"""persistent_worker.py — Persistent autonomous coding agent for tical-code.

Entry point:
    PersistentWorker(worker, task_id, task_desc).run()

Architecture:
    Phase 0 — _autonomous_cycle() detects large task, delegates here
    Phase 1 — _plan() → LLM produces JSON subtask list
    Phase 2 — _execute_subtasks() → LLM loop per subtask, git checkpoints, memory
    Phase 3 — _finalize() → merge to main, report, anchor update

State:   ~/.persistent/<task_id>/
Git:     branch persistent/<task_id>
Resume:  detects state.json → _resume() → git reset + clean → rerun failed subtask
"""

import ast
import json
import logging
import os
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

from tical_code.core.persistent_diff import GitCheckpointManager

logger = logging.getLogger("tical-code.persistent_worker")

# ── Constants ──────────────────────────────────────────────────────────────────

LARGE_KEYWORDS = [
    "重构", "实现", "多个", "全部", "整个", "每一个",
    "migrate", "refactor", "implement", "multiple", "all",
    "create", "build",
]
MAX_CONV_MESSAGES = 24          # rolling window size
MAX_TOOL_OUTPUT_CHARS = 8000    # truncate tool results before append
MAX_REPLANS = 3                 # prevent infinite replan loop
AUTOSAVE_INTERVAL = 5           # git + state save every N steps
STATE_DIR = Path.home() / ".persistent"


# ── Public helper ──────────────────────────────────────────────────────────────

def task_is_large(desc: str) -> bool:
    """Return True when the task should run in persistent mode."""
    return len(desc) > 200 or any(kw in desc for kw in LARGE_KEYWORDS)


# ── Data structures ────────────────────────────────────────────────────────────

@dataclass
class Subtask:
    id: int
    title: str
    description: str = ""
    files: Optional[list] = None
    depends_on: Optional[list] = None
    status: str = "pending"          # pending | running | done | failed | skipped
    max_steps: int = 50
    max_time_seconds: int = 600
    steps_used: int = 0
    result_summary: str = ""
    git_commit_before: str = ""
    git_commit_after: str = ""


# ── Cost tracking ──────────────────────────────────────────────────────────────

class CostTracker:
    """Tracks LLM cost. Integrates with worker.usage (record_tokens/record_api_call)."""

    INPUT_PRICE = 0.5 / 1_000_000    # $0.50/M input
    OUTPUT_PRICE = 2.0 / 1_000_000   # $2.00/M output

    def __init__(self, usage_tracker=None, cost_limit: float = 5.0):
        self.usage = usage_tracker
        self.cost_limit = cost_limit
        self.total_cost = 0.0
        self._call_count = 0

    def add_call(self, response) -> float:
        """Account for one LLM response. Updates usage tracker if available."""
        cost = self._extract(response)
        self.total_cost += cost
        self._call_count += 1
        if self.usage:
            usage = getattr(response, "usage", None)
            try:
                self.usage.record_tokens(
                    prompt_tokens=getattr(usage, "prompt_tokens", 0) if usage else 0,
                    completion_tokens=getattr(usage, "completion_tokens", 0) if usage else 0,
                    model=getattr(response, "model", "deepseek-chat"),
                )
                self.usage.record_api_call(
                    provider="deepseek",
                    model=getattr(response, "model", "deepseek-chat"),
                    success=True,
                )
            except Exception:
                pass
        return cost

    def check_limit(self) -> str:
        """Return 'ok' | 'warning' (80%) | 'exceeded' (100%)."""
        if self.total_cost >= self.cost_limit:
            return "exceeded"
        if self.total_cost >= self.cost_limit * 0.8:
            return "warning"
        return "ok"

    def _extract(self, response) -> float:
        u = getattr(response, "usage", None)
        if u:
            pt = getattr(u, "prompt_tokens", 0) or 0
            ct = getattr(u, "completion_tokens", 0) or 0
            return pt * self.INPUT_PRICE + ct * self.OUTPUT_PRICE
        return 0.001  # fallback minimum


# ── File context (AST extraction) ──────────────────────────────────────────────

def _approx_tokens(text: str) -> int:
    return len(text) // 4


def load_file_context(files: list, workspace: str) -> dict:
    """AST-based file summary. Returns dict[path, str] clipped to ~4000 tokens."""
    ctx: dict[str, str] = {}
    for path in (files or []):
        full = path if os.path.isabs(path) else os.path.join(workspace, path)
        if not os.path.exists(full) or os.path.getsize(full) > 1_048_576:
            continue
        try:
            source = open(full, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        try:
            tree = ast.parse(source)
            classes = [
                f"class {n.name}(...):  # L{n.lineno}\n  {(ast.get_docstring(n) or '')[:200]}"
                for n in ast.walk(tree) if isinstance(n, ast.ClassDef)
            ]
            functions = [
                f"def {n.name}(...):  # L{n.lineno}\n  {(ast.get_docstring(n) or '')[:200]}"
                for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            summary = (
                f"File: {path}\nClasses: {len(classes)} | Functions: {len(functions)}\n"
                + ("\n".join(classes[:10]) + "\n" if classes else "")
                + ("\n".join(functions[:20]) + "\n" if functions else "")
            )
            ctx[path] = summary
        except SyntaxError:
            ctx[path] = source[:2000]
    while _approx_tokens(str(ctx)) > 4000 and len(ctx) > 1:
        del ctx[max(ctx, key=lambda k: _approx_tokens(ctx[k]))]
    return ctx


# ── PersistentWorker ───────────────────────────────────────────────────────────

class PersistentWorker:
    """Persistent autonomous coding agent. Owns the subtask loop, git, memory, cost."""

    def __init__(self, worker, task_id: int, task_desc: str, cost_limit: float = 5.0):
        from tical_code.core import tool_executor as _te

        self.task_id = task_id
        self.task_desc = task_desc
        self.cost_limit = cost_limit
        self.name = worker.name
        self.workspace = worker.workspace
        self.llm = worker.llm
        self._anchor_api = worker._anchor_api  # (path, method, data) → dict|None
        self.eite = getattr(worker, "eite", None)
        self.reporter = getattr(worker, "reporter", None)

        # Tool execution (module-level, not worker attribute)
        self._execute = _te.execute
        self._tools = _te.TOOL_SCHEMAS_CLEAN

        # State directory
        self.state_dir = STATE_DIR / str(task_id)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "memory").mkdir(exist_ok=True)
        (self.state_dir / "subtask_logs").mkdir(exist_ok=True)

        # Components
        self.git = GitCheckpointManager(self.workspace, task_id)
        self.cost = CostTracker(getattr(worker, "usage", None), cost_limit)

        # Subtasks
        self.subtasks: list[Subtask] = []
        self._replan_count = 0
        self._abort = False
        self._started_at = time.time()

    # ═════════════════════════════════════════════════════════════════════════
    # Public entry point
    # ═════════════════════════════════════════════════════════════════════════

    def run(self):
        """Main entry. If state.json exists, resume; otherwise start fresh."""
        logger.info(f"[persistent] task #{self.task_id}: {self.task_desc[:60]}")
        if (self.state_dir / "state.json").exists():
            self._resume()
            return

        self._write_meta()
        self._anchor_report("planning")
        plan = self._plan()
        if not plan:
            self._finish("failed", "planning failed after 2 attempts")
            return

        self.subtasks = plan
        self.git.create_branch()
        self._save_state("running")
        self._update_progress()

        self._execute_subtasks()

        if not self._abort:
            self._finalize()

    # ═════════════════════════════════════════════════════════════════════════
    # Phase 0 — Resume
    # ═════════════════════════════════════════════════════════════════════════

    def _resume(self):
        state = self._load_state()
        if state.get("status") in ("done", "failed", "cancelled"):
            logger.info(f"[persistent:{self.task_id}] already terminal: {state['status']}")
            return

        self.subtasks = [Subtask(**s) for s in state.get("subtasks", [])]
        self.cost.total_cost = state.get("cost_incurred", 0.0)

        try:
            self.git.checkout_branch()
        except Exception as e:
            logger.error(f"[persistent:{self.task_id}] resume checkout failed: {e}")
            self._finish("failed", f"resume checkout: {e}")
            return

        # Guarantee clean workspace before re-running
        self.git.reset_to_head()

        # Mark interrupted subtasks as failed (will be re-run)
        for st in self.subtasks:
            if st.status == "running":
                st.status = "failed"

        from_idx = next(
            (i for i, s in enumerate(self.subtasks) if s.status in ("pending", "failed")),
            len(self.subtasks),
        )
        self._anchor_report(f"resumed sub={from_idx+1}/{len(self.subtasks)}")

        self._execute_subtasks(from_idx=from_idx)
        if not self._abort:
            self._finalize()

    # ═════════════════════════════════════════════════════════════════════════
    # Phase 1 — Plan
    # ═════════════════════════════════════════════════════════════════════════

    def _plan(self, completed_summaries: Optional[list] = None) -> Optional[list]:
        """Ask LLM to decompose task into subtasks. Retries once on failure."""
        completed = ""
        if completed_summaries:
            completed = "\nAlready completed:\n" + json.dumps(completed_summaries, indent=2)

        prompt = (
            "You are planning a complex coding task into subtasks.\n\n"
            f"Repository: {os.path.basename(self.workspace)}\n"
            f"Task: {self.task_desc}\n{completed}\n\n"
            "Rules:\n"
            "1. Break into 3-15 subtasks. Each must fit within 50 tool calls.\n"
            "2. Each subtask must have clear completion criteria.\n"
            "3. Dependencies MUST be explicit (B depends on A).\n"
            "4. Mark which files each subtask will read/modify.\n"
            "5. Output ONLY a valid JSON array. No markdown, no preamble.\n\n"
            '[{"id":1,"title":"...","description":"...","files":["a.py"],"depends_on":[]}]'
        )
        for attempt in range(2):
            try:
                resp = self.llm.chat([{"role": "user", "content": prompt}])
                raw = (resp.content or "").strip()
                # Strip markdown fences if LLM wrapped output
                if "```" in raw:
                    raw = raw.split("```")[1] if raw.count("```") > 1 else raw
                    if raw.startswith("json"):
                        raw = raw[4:]
                raw = raw.strip()
                data = json.loads(raw)
                subtasks = []
                for item in data:
                    item.setdefault("files", [])
                    item.setdefault("depends_on", [])
                    item.setdefault("description", "")
                    subtasks.append(Subtask(**item))
                if not subtasks:
                    raise ValueError("empty subtask list")
                return subtasks
            except Exception as e:
                logger.warning(f"[persistent:{self.task_id}] plan attempt {attempt+1}/2: {e}")
        return None

    # ═════════════════════════════════════════════════════════════════════════
    # Phase 2 — Execute subtasks
    # ═════════════════════════════════════════════════════════════════════════

    def _execute_subtasks(self, from_idx: int = 0):
        """Iterate subtasks[from_idx:] in order, running each with its own LLM loop."""
        for i in range(from_idx, len(self.subtasks)):
            st = self.subtasks[i]
            if st.status == "done":
                continue
            if not self._deps_met(st):
                st.status = "skipped"
                self._save_state()
                continue
            if self._cancel_requested():
                self._cancel_cleanup()
                return

            # Git: commit dirty state, rebase, then before-checkpoint
            self.git.commit_all(f"[persistent] autosave before sub#{st.id}")
            ok, err = self.git.rebase_main()
            if not ok:
                st.status = "failed"
                st.result_summary = f"rebase conflict: {err[:100]}"
                self._log_conflict(f"sub#{st.id}: {err}")
                self._save_state()
                continue

            st.git_commit_before = self.git.autosave(self.task_id, st.id, 0)
            self._anchor_report(f"sub={i+1}/{len(self.subtasks)}: {st.title[:40]}")

            # Build initial conversation
            file_ctx = load_file_context(st.files, self.workspace)
            mem_inject = self._build_memory_inject(st.files)
            conv = [{"role": "system", "content": self._build_prompt(st, file_ctx, mem_inject)}]

            # Inner LLM loop
            st.status = "running"
            st.steps_used = 0
            subtask_start = time.time()
            replan = False

            for step in range(st.max_steps):
                if time.time() - subtask_start > st.max_time_seconds:
                    st.status = "failed"
                    st.result_summary = "timeout"
                    break
                if self._cancel_requested():
                    self._cancel_cleanup()
                    return

                # Periodic autosave + progress inject
                if step > 0 and step % AUTOSAVE_INTERVAL == 0:
                    self.git.autosave(self.task_id, st.id, step)
                    self._save_state()
                    self._update_progress(st, step)
                    self._inject_progress(conv, step, st)

                # Roll conv to prevent context explosion
                conv = self._roll_conv(conv, step)

                # LLM call with retry on transient errors
                response = self._llm_call(conv)
                st.steps_used += 1
                self.cost.add_call(response)
                self._append_trajectory(response)

                content = self._extract_content(response) or ""
                tool_calls = getattr(response, "tool_calls", None) or []

                # Append assistant turn
                conv.append({"role": "assistant", "content": content})

                # Signal detection
                if "[SUBTASK_DONE]" in content:
                    st.result_summary = content.split("[SUBTASK_DONE]", 1)[-1].strip()[:200]
                    break
                if "[REPLAN]" in content:
                    replan = True
                    break

                # Execute tools
                if tool_calls:
                    for tc in tool_calls:
                        name = tc.function.name if hasattr(tc, "function") else tc.name
                        raw_args = tc.function.arguments if hasattr(tc, "function") else tc.arguments
                        try:
                            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                        except (json.JSONDecodeError, TypeError):
                            args = {}
                        try:
                            result = self._execute(name, args, base_dir=self.workspace)
                            output = json.dumps(result, ensure_ascii=False)[:MAX_TOOL_OUTPUT_CHARS]
                        except Exception as e:
                            output = f"[error] {e}"
                        conv.append({"role": "tool", "content": output})

                # Cost limit check
                if self.cost.check_limit() == "exceeded":
                    self._save_state("paused", "cost_limit")
                    self._abort = True
                    return

            # Post-subtask: checkpoint + memory
            st.git_commit_after = self.git.autosave(self.task_id, st.id, st.steps_used)
            if st.status == "running":
                st.status = "done"
            self._write_external_memory(st, i + 1)
            self._write_subtask_log(st, i + 1)
            self._update_progress(st)
            self._save_state()

            # Re-plan?
            if replan:
                self._replan_count += 1
                if self._replan_count > MAX_REPLANS:
                    logger.warning(f"[persistent:{self.task_id}] max replans ({MAX_REPLANS}) reached")
                    break
                done = [s for s in self.subtasks if s.status == "done"]
                new_plan = self._plan(completed_summaries=[
                    {"id": s.id, "title": s.title, "result": s.result_summary[:100]} for s in done
                ])
                if new_plan:
                    self.subtasks = done + new_plan
                    self._save_state()
                    self._execute_subtasks(from_idx=len(done))
                    return
                else:
                    st.status = "failed"
                    st.result_summary = "replan failed"

    # ═════════════════════════════════════════════════════════════════════════
    # Phase 3 — Finalize
    # ═════════════════════════════════════════════════════════════════════════

    def _finalize(self):
        ok, err = self.git.merge_to_main()
        status = "done" if ok else "failed"
        report = {
            "status": status,
            "task_id": self.task_id,
            "total_subtasks": len(self.subtasks),
            "completed": sum(1 for s in self.subtasks if s.status == "done"),
            "failed": sum(1 for s in self.subtasks if s.status == "failed"),
            "skipped": sum(1 for s in self.subtasks if s.status == "skipped"),
            "total_steps": sum(s.steps_used for s in self.subtasks),
            "cost_usd": round(self.cost.total_cost, 4),
            "error": err or "",
        }
        self._save_state(status)
        self._write_report(report)
        self.git.cleanup_branch()
        if err:
            self._log_conflict(err)

        summary = f"[persistent] {report['completed']}/{report['total_subtasks']} done, ${report['cost_usd']:.2f}"
        self._anchor_report(summary)
        self._anchor_complete(summary)
        logger.info(f"[persistent:{self.task_id}] {summary}")

    def _finish(self, status: str, result: str):
        """Quick exit without full merge (planning failure, etc.)."""
        self._save_state(status)
        self._update_progress(status=status)
        self._anchor_report(f"{status}: {result[:60]}")
        logger.info(f"[persistent:{self.task_id}] {status}: {result}")

    def _cancel_cleanup(self):
        self._save_state("cancelled")
        self.git.cleanup_branch()
        self._abort = True
        logger.info(f"[persistent:{self.task_id}] cancelled")

    # ═════════════════════════════════════════════════════════════════════════
    # Conv management
    # ═════════════════════════════════════════════════════════════════════════

    def _roll_conv(self, conv: list, step: int) -> list:
        """Keep conv within MAX_CONV_MESSAGES by dropping old tool results."""
        if len(conv) <= MAX_CONV_MESSAGES:
            return conv
        # Keep system prompt (index 0) + recent messages
        return [conv[0]] + conv[-(MAX_CONV_MESSAGES - 1):]

    def _inject_progress(self, conv: list, step: int, st: Subtask):
        conv.append({
            "role": "system",
            "content": f"[progress] Step {step}/{st.max_steps} | Cost ${self.cost.total_cost:.3f} / ${self.cost_limit:.2f} | Keep working toward [SUBTASK_DONE].",
        })

    def _llm_call(self, conv: list) -> object:
        """Call LLM with retry on transient errors (429, 500). HTTP 400 not retried."""
        last_err = None
        for attempt in range(3):
            try:
                return self.llm.chat(conv, tools=self._tools)
            except Exception as e:
                if "400" in str(e):
                    raise  # not retryable
                last_err = e
                wait = 30 * (attempt + 1)
                logger.warning(f"[persistent:{self.task_id}] LLM transient (attempt {attempt+1}): {e}, retry in {wait}s")
                time.sleep(wait)
        raise last_err  # exhausted retries

    def _extract_content(self, response) -> str:
        c = getattr(response, "content", None)
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return " ".join(
                item.get("text", "") if isinstance(item, dict) else str(item) for item in c
            )
        return str(c) if c else ""

    # ═════════════════════════════════════════════════════════════════════════
    # State persistence
    # ═════════════════════════════════════════════════════════════════════════

    def _save_state(self, status: str = "running", reason: str = ""):
        """Atomic write: .tmp → os.replace. Guarantees state.json is never half-written."""
        existing = self._load_state()
        state = {
            "description": self.task_desc,
            "subtasks": [asdict(s) for s in self.subtasks],
            "cost_incurred": self.cost.total_cost,
            "cost_limit": self.cost_limit,
            "status": status,
            "reason": reason,
            "created_at": existing.get("created_at", self._started_at),
            "git_head_before": existing.get("git_head_before", ""),
            "git_head_current": self._safe_git_head(),
            "updated_at": time.time(),
        }
        tmp = self.state_dir / "state.json.tmp"
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
        os.replace(str(tmp), str(self.state_dir / "state.json"))

    def _load_state(self) -> dict:
        p = self.state_dir / "state.json"
        if p.exists():
            try:
                return json.loads(p.read_text())
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def _update_progress(self, st: Optional[Subtask] = None, step: int = 0, status: str = "running"):
        idx = next(
            (i for i, s in enumerate(self.subtasks) if s.status in ("running", "pending")), 0
        )
        progress = {
            "status": status,
            "task_id": self.task_id,
            "description": self.task_desc[:100],
            "current_subtask": idx + 1,
            "total_subtasks": len(self.subtasks),
            "current_subtask_title": st.title if st else "",
            "steps_in_subtask": step,
            "cost_usd": round(self.cost.total_cost, 4),
            "started_at": self._started_at,
            "last_active": time.time(),
        }
        self.state_dir.joinpath("progress.json").write_text(json.dumps(progress, indent=2))

    def _write_meta(self):
        self.state_dir.joinpath("meta.json").write_text(json.dumps({
            "task_id": self.task_id, "description": self.task_desc,
            "worker": self.name, "created_at": self._started_at,
        }, indent=2))

    def _write_report(self, report: dict):
        self.state_dir.joinpath("report.json").write_text(json.dumps(report, indent=2))

    # ═════════════════════════════════════════════════════════════════════════
    # External memory (cross-subtask summaries)
    # ═════════════════════════════════════════════════════════════════════════

    def _write_external_memory(self, st: Subtask, step_num: int):
        mem_dir = self.state_dir / "memory"
        index_p = mem_dir / "index.json"
        index: dict = json.loads(index_p.read_text()) if index_p.exists() else {}

        (mem_dir / f"{step_num:04d}_summary.json").write_text(json.dumps({
            "step": step_num, "subtask_id": st.id, "action": st.title,
            "result": st.result_summary, "files_changed": st.files or [],
            "git_before": st.git_commit_before, "git_after": st.git_commit_after,
        }, indent=2))

        for f in (st.files or []):
            index.setdefault(f, []).append(step_num)
            # Keep only recent 20 entries per file
            index[f] = index[f][-20:]
        index_p.write_text(json.dumps(index, indent=2))

    def _build_memory_inject(self, files: list) -> str:
        """Build '## External memory' block filtered by file relevance."""
        index_p = self.state_dir / "memory" / "index.json"
        if not index_p.exists():
            return ""
        try:
            index = json.loads(index_p.read_text())
        except (json.JSONDecodeError, OSError):
            return ""
        relevant: set[int] = set()
        for f in (files or []):
            relevant.update(index.get(f, []))
        if not relevant:
            return ""

        lines = ["## External memory (files relevant to this subtask)"]
        for sn in sorted(relevant)[-5:]:
            sf = self.state_dir / "memory" / f"{sn:04d}_summary.json"
            if sf.exists():
                try:
                    s = json.loads(sf.read_text())
                    changed = ", ".join(s.get("files_changed", []))
                    lines.append(f"  - Step {s['step']}: {s['action']} → {s['result'][:80]}" + (f" ({changed})" if changed else ""))
                except Exception:
                    pass
        return "\n".join(lines)

    def _write_subtask_log(self, st: Subtask, step_num: int):
        safe = st.title[:40].replace("/", "_")
        (self.state_dir / "subtask_logs" / f"{step_num:02d}_{safe}.txt").write_text(
            f"Subtask {st.id}: {st.title}\nStatus: {st.status}\nSteps: {st.steps_used}\nResult: {st.result_summary}\n"
        )

    # ═════════════════════════════════════════════════════════════════════════
    # Trajectory
    # ═════════════════════════════════════════════════════════════════════════

    def _append_trajectory(self, response):
        entry = {
            "role": "assistant",
            "content": self._extract_content(response)[:2000],
            "cost": self.cost._extract(response),
            "ts": time.time(),
        }
        with self.state_dir.joinpath("conv_history.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # ═════════════════════════════════════════════════════════════════════════
    # Prompt
    # ═════════════════════════════════════════════════════════════════════════

    def _build_prompt(self, st: Subtask, file_ctx: dict, mem_inject: str) -> str:
        cost_status = self.cost.check_limit()
        warn = (
            "APPROACHING COST LIMIT" if cost_status == "warning"
            else "COST EXCEEDED" if cost_status == "exceeded"
            else ""
        )
        ctx_str = "\n\n".join(f"### {p}\n{s}" for p, s in file_ctx.items()) if file_ctx else "(no files)"
        return (
            f"You are a persistent autonomous coding agent on {self.name}.\n"
            f"Task: {self.task_desc[:300]}\n"
            f"Current subtask ({st.id}/{len(self.subtasks)}): {st.title}\n"
            f"Description: {st.description}\n\n"
            f"Files:\n{ctx_str}\n\n{mem_inject}\n\n"
            "Rules:\n"
            "1. Complete current subtask, then output [SUBTASK_DONE] + summary.\n"
            "2. If plan is wrong, output [REPLAN] + explanation.\n"
            "3. Each step must make measurable progress.\n"
            f"4. Cost: ${self.cost.total_cost:.2f} / ${self.cost_limit:.2f} {warn}\n"
            "5. All file changes are tracked by git.\n"
            "6. Use bash, file_write, execute_code, and all tools freely.\n"
            "7. Read files before modifying.\n"
        )

    # ═════════════════════════════════════════════════════════════════════════
    # Anchor integration
    # ═════════════════════════════════════════════════════════════════════════

    def _anchor_report(self, status_text: str):
        """Update anchor with current persistent task status."""
        if not self._anchor_api:
            return
        try:
            self._anchor_api("anchor", "POST", {
                "name": self.name, "status": "online",
                "current_task": f"[persistent] task_id={self.task_id} {status_text[:80]}",
                "task_type": "persistent",
            })
        except Exception:
            pass

    def _anchor_complete(self, summary: str):
        """Mark task complete on anchor."""
        if not self._anchor_api:
            return
        try:
            self._anchor_api("task/complete", "POST", {
                "task_id": self.task_id, "result": summary, "status": "done",
            })
        except Exception:
            pass

    # ═════════════════════════════════════════════════════════════════════════
    # Utilities
    # ═════════════════════════════════════════════════════════════════════════

    def _deps_met(self, st: Subtask) -> bool:
        done = {s.id for s in self.subtasks if s.status == "done"}
        return all(d in done for d in (st.depends_on or []))

    def _cancel_requested(self) -> bool:
        return (self.state_dir / "cancel.flag").exists()

    def _safe_git_head(self) -> str:
        try:
            return self.git.get_head()
        except Exception:
            return "unknown"

    def _log_conflict(self, msg: str):
        with self.state_dir.joinpath("conflicts.log").open("a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
