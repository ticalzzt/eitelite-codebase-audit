"""persistent_worker.py — Long-running autonomous task executor.

Designed for tical-code. Runs complex tasks as a series of subtasks,
each with git checkpoints, cost tracking, external memory, and crash recovery.

Usage (from unified_worker._autonomous_cycle):
    pw = PersistentWorker(worker=self, task_id=task_id, task_desc=task_desc_str)
    pw.run()
"""

import json
import logging
import os
import re
import time
import traceback
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

logger = logging.getLogger("tical-code.persistent_worker")
TOKEN_COST_PER_M_INPUT = 0.5    # DeepSeek pricing: $0.5/M input tokens
TOKEN_COST_PER_M_OUTPUT = 2.0   # $2/M output tokens


@dataclass
class Subtask:
    id: int
    title: str
    description: str = ""
    files: Optional[list] = None
    depends_on: Optional[list] = None
    status: str = "pending"
    max_steps: int = 50
    max_time_seconds: int = 600
    steps_used: int = 0
    result_summary: str = ""
    git_commit_before: str = ""
    git_commit_after: str = ""


# ---------------------------------------------------------------------------
# External Memory — structured step summaries, indexed by file
# ---------------------------------------------------------------------------

class ExternalMemory:
    """Append-only step summary store with file-based index."""

    def __init__(self, state_dir: Path):
        self.mem_dir = state_dir / "memory"
        self.mem_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.mem_dir / "index.json"
        self._counter = 0
        self._index = self._load_index()

    def _load_index(self) -> dict:
        if self.index_path.exists():
            return json.loads(self.index_path.read_text())
        return {}

    def _save_index(self):
        self.index_path.write_text(json.dumps(self._index, indent=2))

    def add(self, subtask: Subtask):
        """Write one summary record and update the file index."""
        self._counter += 1
        record = {
            "step": self._counter,
            "subtask_id": subtask.id,
            "title": subtask.title,
            "result": subtask.result_summary[:200],
            "files_changed": subtask.files or [],
            "files_read": subtask.files or [],
        }
        path = self.mem_dir / f"{self._counter:04d}_summary.json"
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2))

        # Update file index
        for f in (subtask.files or []):
            self._index.setdefault(f, []).append(self._counter)
        self._save_index()

    def get_context_for_files(self, files: list[str]) -> str:
        """Return a prompt fragment with relevant step summaries."""
        if not files:
            return ""
        relevant_steps = set()
        for f in files:
            relevant_steps.update(self._index.get(f, []))
        if not relevant_steps:
            return ""
        lines = ["", "## External memory (previous steps touching your files)"]
        for step_num in sorted(relevant_steps)[-5:]:  # last 5 only
            p = self.mem_dir / f"{step_num:04d}_summary.json"
            if p.exists():
                r = json.loads(p.read_text())
                lines.append(f"  - Step {r['step']}: {r['title']} → {r['result'][:80]}")
        lines.append("")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# File context loader (AST-based)
# ---------------------------------------------------------------------------

MAX_CONTEXT_TOKENS = 4000
MAX_FILE_BYTES = 1_048_576  # 1MB


def _count_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token for Chinese, ~1 per word for English."""
    return len(text) // 4 + text.count(" ")


def _get_docstring(node) -> str:
    """Extract first line of docstring from an AST node."""
    doc = getattr(node, "doc", None)
    if doc:
        return doc.strip().split("\n")[0][:200]
    return ""


def load_file_context(files: list[str], repo_root: str) -> str:
    """Build a compact file-context string using AST extraction.

    For Python files: shows class/function signatures + docstrings.
    For other files: shows first 50 lines.
    Total context capped at MAX_CONTEXT_TOKENS.
    """
    context_parts = []
    total_tokens = 0

    for path in files:
        full_path = os.path.join(repo_root, path) if not os.path.isabs(path) else path
        if not os.path.exists(full_path):
            continue
        if os.path.getsize(full_path) > MAX_FILE_BYTES:
            continue

        source = open(full_path, "r", errors="replace").read()

        try:
            import ast
            tree = ast.parse(source)
            classes = []
            functions = []
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    doc = _get_docstring(node)
                    classes.append(f"  class {node.name}(...):  # line {node.lineno}" + (f"  {doc}" if doc else ""))
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    doc = _get_docstring(node)
                    functions.append(f"  def {node.name}(...):  # line {node.lineno}" + (f"  {doc}" if doc else ""))

            lines = [f"File: {path}"]
            lines.append(f"  Classes: {len(classes)} | Functions: {len(functions)}")
            lines.extend(classes[:10])
            lines.extend(functions[:20])
            block = "\n".join(lines)
        except SyntaxError:
            block = f"File: {path}  (non-Python)\n" + "\n".join(source.split("\n")[:50])

        tokens = _count_tokens(block)
        if total_tokens + tokens > MAX_CONTEXT_TOKENS and context_parts:
            break
        context_parts.append(block)
        total_tokens += tokens

    return "\n\n".join(context_parts)


# ---------------------------------------------------------------------------
# Cost tracker
# ---------------------------------------------------------------------------

def extract_cost(response) -> float:
    """Extract cost from LLM response. Use usage fields if available, else estimate."""
    try:
        usage = response.usage
        if usage:
            in_tokens = getattr(usage, "prompt_tokens", 0) or 0
            out_tokens = getattr(usage, "completion_tokens", 0) or 0
            return (in_tokens / 1_000_000 * TOKEN_COST_PER_M_INPUT +
                    out_tokens / 1_000_000 * TOKEN_COST_PER_M_OUTPUT)
    except Exception:
        pass
    return 0.001  # minimum per-call cost estimate


# ---------------------------------------------------------------------------
# Main PersistentWorker
# ---------------------------------------------------------------------------

class PersistentWorker:
    """Manages the full lifecycle of a persistent autonomous task."""

    def __init__(self, worker, task_id: int, task_desc: str, cost_limit: float = 5.0):
        self.worker = worker
        # Import module-level references (not worker attributes, which don't exist)
        from tical_code.core.tool_executor import execute, TOOL_SCHEMAS_CLEAN
        self.llm = worker.llm
        self.execute = execute
        self.tools = TOOL_SCHEMAS_CLEAN
        self.workspace = worker.workspace
        self.anchor_api = worker._anchor_api
        self.name = worker.name
        self.eite = getattr(worker, "eite", None)
        self.reporter = getattr(worker, "reporter", None)
        self.usage = getattr(worker, "usage", None)

        self.task_id = task_id
        self.task_desc = task_desc
        self.cost_limit = cost_limit
        self.cost_incurred = 0.0
        self._persistent_running = False

        # State directory
        self.state_dir = Path.home() / ".persistent" / str(task_id)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.state_dir / "state.json"
        self.progress_path = self.state_dir / "progress.json"

        # Git manager
        from tical_code.core.persistent_diff import GitCheckpointManager
        self.git = GitCheckpointManager(self.workspace)

        # Subtask state
        self.subtasks: list[Subtask] = []
        self.memory = ExternalMemory(self.state_dir)

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _save_state(self, status: str = "running"):
        """Atomically write state."""
        state = {
            "task_id": self.task_id,
            "description": self.task_desc,
            "status": status,
            "cost_incurred": self.cost_incurred,
            "cost_limit": self.cost_limit,
            "subtasks": [asdict(s) for s in self.subtasks],
            "last_active": time.time(),
        }
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
        os.replace(str(tmp), str(self.state_path))

        # Progress file (human/worker readable)
        running = [s for s in self.subtasks if s.status == "running"]
        done = [s for s in self.subtasks if s.status == "done"]
        progress = {
            "status": status,
            "task_id": self.task_id,
            "description": self.task_desc[:80],
            "current_subtask": running[0].id if running else (done[-1].id if done else 0),
            "total_subtasks": len(self.subtasks),
            "steps_in_subtask": running[0].steps_used if running else 0,
            "current_subtask_title": running[0].title if running else "",
            "cost_usd": round(self.cost_incurred, 4),
            "last_active": time.time(),
        }
        self.progress_path.write_text(json.dumps(progress, indent=2))

        # Also push to anchor
        self._anchor_update(status, running)

    def _anchor_update(self, status: str, running: list):
        try:
            task_str = self.task_desc[:60]
            if running:
                task_str = f"[persistent] sub#{running[0].id}/{len(self.subtasks)} {running[0].title[:40]}"
            self.anchor_api("anchor", "POST", {
                "name": self.name, "status": "online",
                "current_task": task_str,
                "progress": f"{len([s for s in self.subtasks if s.status=='done'])}/{len(self.subtasks)} subtasks",
                "task_type": "persistent",
            })
        except Exception:
            pass

    def _load_state(self) -> bool:
        """Load saved state. Returns True if state exists and is valid."""
        if not self.state_path.exists():
            return False
        try:
            data = json.loads(self.state_path.read_text())
            self.subtasks = [Subtask(**s) for s in data.get("subtasks", [])]
            self.cost_incurred = data.get("cost_incurred", 0.0)
            return True
        except (json.JSONDecodeError, KeyError, TypeError):
            logger.warning(f"[persistent] Corrupt state file, starting fresh")
            self.state_path.unlink(missing_ok=True)
            return False

    # ------------------------------------------------------------------
    # Cancel check
    # ------------------------------------------------------------------

    def _check_cancel(self) -> bool:
        if (self.state_dir / "cancel.flag").exists():
            logger.info(f"[persistent] task #{self.task_id} cancelled by flag")
            self._save_state(status="cancelled")
            self.git.checkout("main")
            self.git.cleanup_branch(f"persistent/{self.task_id}")
            return True
        return False

    # ------------------------------------------------------------------
    # Phase 1: Plan
    # ------------------------------------------------------------------

    def _plan(self) -> bool:
        """Call LLM to decompose task into subtasks. Retries once on failure."""
        repo_head = self.git.get_head()[:8] if self.workspace else "?"
        prompt = (
            f"You are planning a complex coding task into subtasks.\n"
            f"Repository: {os.path.basename(self.workspace)} at {repo_head}\n"
            f"Task: {self.task_desc}\n\n"
            f"Rules:\n"
            f"1. Break into 3-15 subtasks\n"
            f"2. Each subtask MUST fit within 50 tool calls\n"
            f"3. Subtasks MUST have clear completion criteria\n"
            f"4. Dependencies MUST be explicit (B depends on A)\n"
            f"5. Mark which files each subtask will read/modify\n"
            f"6. Output ONLY valid JSON, no other text\n\n"
            f"Format:\n"
            f'[{{"id":1,"title":"...","description":"...","files":["a.py"],"depends_on":[]}}]'
        )

        for attempt in range(2):
            try:
                resp = self.llm.chat([{"role": "user", "content": prompt}])
                match = re.search(r"\[.*\]", resp.content or "", re.DOTALL)
                if not match:
                    raise ValueError("No JSON array in response")
                data = json.loads(match.group(0))
                self.subtasks = [Subtask(**item) for item in data]
                if not self.subtasks:
                    raise ValueError("Empty subtask list")
                # Create git branch
                self.git.create_branch(
                    f"persistent/{self.task_id}",
                    f"[persistent] task #{self.task_id}: start",
                )
                self._save_state()
                return True
            except Exception as e:
                logger.warning(f"[persistent] Plan attempt {attempt+1} failed: {e}")
        return False

    # ------------------------------------------------------------------
    # Phase 2: Execute subtasks
    # ------------------------------------------------------------------

    def _execute_subtasks(self):
        """Run each pending/failed subtask sequentially."""
        for subtask in self.subtasks:
            if subtask.status == "done":
                continue
            if self._check_cancel():
                return

            subtask.status = "running"
            subtask.steps_used = 0

            # Git checkpoint
            self.git.commit_checkpoint(
                f"[persistent] task #{self.task_id} sub#{subtask.id}: before {subtask.title[:40]}"
            )
            subtask.git_commit_before = self.git.get_head()

            # Conflict detection
            if not self.git.try_rebase():
                subtask.status = "failed"
                subtask.result_summary = "git conflict"
                conflict_log = self.state_dir / "conflicts.log"
                conflict_log.write_text(
                    f"Subtask #{subtask.id} {subtask.title}: rebase conflict\n"
                )
                self._save_state()
                continue

            # Load file context
            file_context = load_file_context(subtask.files or [], self.workspace)

            # External memory injection
            ext_mem = self.memory.get_context_for_files(subtask.files)

            # Build system prompt
            sys_prompt = (
                f"You are a persistent autonomous coding agent on {self.name}.\n"
                f"Task: {self.task_desc[:200]}\n"
                f"Current subtask ({subtask.id}): {subtask.title}\n\n"
                f"Files available:\n{file_context}\n"
                f"{ext_mem}\n"
                f"Rules:\n"
                f"1. Complete this subtask, then say [SUBTASK_DONE] followed by a summary\n"
                f"2. If plan is wrong, say [REPLAN] + explanation\n"
                f"3. Each step must make measurable progress\n"
                f"4. Cost: ${self.cost_incurred:.3f} / ${self.cost_limit:.2f}\n"
                f"5. File changes tracked by git — commits are automatic\n"
                f"6. Use bash, file_write, execute_code, and all tools freely\n"
                f"7. Read files to verify state before modifying\n"
            )
            conv = [{"role": "system", "content": sys_prompt}]

            # Inner loop
            subtask_start = time.time()
            step = 0
            plan_again = False

            for step in range(subtask.max_steps):
                if time.time() - subtask_start > subtask.max_time_seconds:
                    subtask.status = "failed"
                    subtask.result_summary = "timeout"
                    break
                if self._check_cancel():
                    return

                try:
                    response = self.llm.chat(conv, tools=self.tools)
                except Exception as e:
                    logger.error(f"[persistent] LLM error: {e}")
                    subtask.status = "failed"
                    subtask.result_summary = f"LLM error: {str(e)[:80]}"
                    break

                subtask.steps_used = step + 1

                # Cost tracking
                cost = extract_cost(response)
                self.cost_incurred += cost
                if self.usage:
                    try:
                        self.usage.log_call(
                            model=getattr(response, "model", "unknown"),
                            prompt_tokens=getattr(getattr(response, "usage", None), "prompt_tokens", 0) or 0,
                            completion_tokens=getattr(getattr(response, "usage", None), "completion_tokens", 0) or 0,
                            cost=cost,
                            caller="persistent_worker",
                        )
                    except Exception:
                        pass

                # Save trajectory
                traj = self.state_dir / "conv_history.jsonl"
                with open(traj, "a") as f:
                    f.write(json.dumps({
                        "role": "assistant",
                        "content": (response.content or "")[:500],
                        "tool_calls": len(response.tool_calls) if response.tool_calls else 0,
                        "cost": cost,
                    }, ensure_ascii=False) + "\n")

                # Check signals
                content = response.content or ""
                if "[SUBTASK_DONE]" in content:
                    subtask.status = "done"
                    subtask.result_summary = content.replace("[SUBTASK_DONE]", "").strip()[:200]
                    break
                if "[REPLAN]" in content:
                    plan_again = True
                    break

                # Execute tools
                if response.tool_calls:
                    for tc in response.tool_calls:
                        try:
                            result = self.execute(tc.name, tc.arguments, base_dir=self.workspace)
                            formatted = json.dumps(result, ensure_ascii=False)[:2000]
                        except Exception as e:
                            formatted = f"[error] {e}"
                        conv.append({"role": "tool", "content": formatted, "tool_call_id": tc.id})

                # Periodic save and progress injection
                if step % 5 == 0 and step > 0:
                    self._save_state()
                    conv.append({
                        "role": "system",
                        "content": f"[progress] {step+1} steps done in this subtask. Keep going."
                    })

            # After inner loop: git checkpoint
            if subtask.status == "running":
                subtask.status = "done"  # fell through max_steps but didn't fail
            subtask.git_commit_after = self.git.commit_checkpoint(
                f"[persistent] task #{self.task_id} sub#{subtask.id}: after {subtask.title[:40]}"
            )

            # Write external memory
            self.memory.add(subtask)

            # Re-plan if requested
            if plan_again:
                logger.info(f"[persistent] re-plan requested at subtask #{subtask.id}")
                self._replan()
                return

            self._save_state()

        self._save_state()

    # ------------------------------------------------------------------
    # Re-plan
    # ------------------------------------------------------------------

    def _replan(self):
        """Re-plan remaining undone subtasks with completed results as context."""
        done = [s for s in self.subtasks if s.status == "done"]
        undone = [s for s in self.subtasks if s.status != "done"]

        context = "\n".join(f"- #{s.id} {s.title}: {s.result_summary[:100]}" for s in done)
        prompt = (
            f"Re-plan the remaining work for this task.\n"
            f"Task: {self.task_desc[:200]}\n\n"
            f"Already completed:\n{context}\n\n"
            f"Previous plan included:\n"
            + "\n".join(f"- #{s.id} {s.title} (status: {s.status})" for s in undone) +
            f"\n\nUpdate the subtask list. Output ONLY JSON array."
        )
        try:
            resp = self.llm.chat([{"role": "user", "content": prompt}])
            match = re.search(r"\[.*\]", resp.content or "", re.DOTALL)
            if match:
                data = json.loads(match.group(0))
                # Replace undone subtasks, keep done ones
                next_id = max(s.id for s in self.subtasks) + 1
                for item in data:
                    item["id"] = next_id
                    next_id += 1
                    item["status"] = "pending"
                self.subtasks = done + [Subtask(**item) for item in data]
                self._save_state()
                logger.info(f"[persistent] re-plan: {len(data)} new subtasks")
        except Exception as e:
            logger.warning(f"[persistent] re-plan failed: {e}")

    # ------------------------------------------------------------------
    # Phase 3: Finalize
    # ------------------------------------------------------------------

    def _finalize(self):
        """Merge branch, report, clean up."""
        branch = f"persistent/{self.task_id}"
        if all(s.status == "done" for s in self.subtasks):
            if self.git.try_merge_to_main(branch):
                self._save_state(status="done")
                report = {
                    "status": "done",
                    "task_id": self.task_id,
                    "total_subtasks": len(self.subtasks),
                    "completed": sum(1 for s in self.subtasks if s.status == "done"),
                    "failed": sum(1 for s in self.subtasks if s.status == "failed"),
                    "skipped": sum(1 for s in self.subtasks if s.status == "skipped"),
                    "total_steps": sum(s.steps_used for s in self.subtasks),
                    "total_cost_usd": round(self.cost_incurred, 4),
                }
                report_path = self.state_dir / "report.json"
                report_path.write_text(json.dumps(report, indent=2))
                self.git.cleanup_branch(branch)
                # Anchor update
                try:
                    self.anchor_api("task/complete", "POST", {
                        "task_id": self.task_id,
                        "result": f"[persistent] {report['completed']}/{report['total_subtasks']} subtasks, ${report['total_cost_usd']:.2f}",
                        "status": "done",
                    })
                except Exception:
                    pass
            else:
                self._save_state(status="failed (merge conflict)")
        else:
            self._save_state(status="failed (incomplete)")

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self):
        """Run the persistent task lifecycle: Plan → Execute → Finalize."""
        logger.info(f"[persistent] task #{self.task_id}: {self.task_desc[:60]}")
        self._persistent_running = True

        # Try resume first
        resumed = self._load_state()

        if not resumed:
            if not self._plan():
                self._save_state(status="failed")
                self._persistent_running = False
                logger.warning(f"[persistent] task #{self.task_id}: plan failed")
                return

        self._execute_subtasks()
        self._finalize()
        self._persistent_running = False
        logger.info(f"[persistent] task #{self.task_id}: done")
