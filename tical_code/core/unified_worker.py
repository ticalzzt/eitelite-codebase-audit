"""tical-code unified worker - main loop.

Replaces ticobot_worker_v0.10.0.py and worker_loop.py.
Single loop: poll channels → LLM call → tool execute → format → reply.
"""
import json
import logging
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

# Add parent dir to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tical_code.core.channel import Message, Response, TelegramChannel, TicalChatChannel
from tical_code.core.llm_interface import DeepSeekProvider
from tical_code.core.tool_executor import execute, TOOL_SCHEMAS, TOOL_SCHEMAS_CLEAN, redact_secrets
from tical_code.core.response_formatter import format_result
from tical_code.core.eite import init as eite_init, get_verify
from tical_code.core.prompt import build_system_prompt
from tical_code.core.config import load_config
from tical_code.core.modules.session_manager import SessionManager
from tical_code.core.modules.context_compactor import ContextCompactor
from tical_code.core.modules.loop_detector import LoopDetector
from tical_code.core.modules.truthful_reporter import TruthfulReporter
from tical_code.core.modules.proposal_gate import ProposalGate
from tical_code.core.usage import UsageTracker
from tical_code.vigil import build_vigil, NewInstruction

# Known AI worker names — used to detect worker-to-worker messages
# Workers must NOT reply to each other (creates A↔B ping-pong loops)
WORKER_IDS = {"seoul", "tico", "ani", "kael", "tico-oracle", "test"}

logger = logging.getLogger("tical-code.worker")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)


class Worker:
    """Unified worker - polls channels, calls LLM, executes tools, replies."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.name = cfg['name']
        self.workspace = cfg["workspace"]

        # Channels
        self.channels = []
        if cfg.get("tg_token"):
            self.channels.append(TelegramChannel(cfg["tg_token"]))
            logger.info("Telegram channel ready")
        if cfg.get("chat_url"):
            chat_key = cfg.get("chat_key", "") or ""
            if not chat_key:
                logger.warning("TICAL_CHAT_KEY not set — tical-chat channel will fail")
            self.channels.append(TicalChatChannel(
                base_url=cfg["chat_url"],
                identity=cfg['name'],
                shared_key=chat_key,
            ))
            logger.info(f"tical-chat channel ready ({cfg['chat_url']})")

        # LLM backend
        self.llm = DeepSeekProvider(
            model=cfg.get("ai_model", "deepseek-chat"),
            api_key=cfg.get("ai_key", ""),
            base_url=cfg.get("ai_endpoint", "https://api.deepseek.com/v1"),
        )

        # Expose LLM to tool_executor for switch_model
        from tical_code.core import tool_executor as _te
        _te._executor_llm = self.llm

        # System prompt
        # Pending task file for cross-poll continuation
        self._pending_task_file = Path(cfg.get("workspace", ".")) / ".pending_task.json"
        self._pending_task = self._load_pending()

        # Kael's modules
        w = cfg.get("workspace", ".")
        self.sessions = SessionManager(db_path=str(Path(w) / "sessions.db"))
        self.compactor = ContextCompactor(max_tokens=6000, keep_recent=6)
        self.loop_detector = LoopDetector(window_size=30)
        self.reporter = TruthfulReporter(workspace=w)
        self.gate = ProposalGate(timeout_seconds=300)

        # Usage tracking
        self.usage = UsageTracker(db_path=str(Path(w) / "usage.db"))
        
        # CDP browser config - set env for tool_executor to pick up
        cdp_url = cfg.get("cdp_url", "")
        if cdp_url:
            os.environ["CDP_URL"] = cdp_url
            logger.info(f"CDP browser: {cdp_url}")
        if not cfg.get("cdp_headless", True):
            os.environ["CDP_HEADLESS"] = "0"
        cdp_proxy = cfg.get("cdp_proxy", "")
        if cdp_proxy:
            os.environ["CDP_PROXY"] = cdp_proxy
            logger.info(f"CDP proxy: {cdp_proxy}")

        # Set env for tools that depend on WORKER_NAME
        os.environ["WORKER_NAME"] = cfg["name"]

        # Vigil — AI safety runtime (v1: pure software, no hardware)
        try:
            self.vigil = build_vigil()
            self._vigil_enabled = True
            logger.info("Vigil safety runtime active")
        except Exception:
            self.vigil = None
            self._vigil_enabled = False
            logger.warning("Vigil not available")

        self.system_prompt = build_system_prompt(
            name=cfg['name'],
            hostname=self._get_hostname(),
            deploy_path=cfg.get("workspace", ""),
            target_model=cfg.get("ai_model", ""),
        )

        # Live Anchor: register on startup
        self._anchor_ping(cfg)

        # EITE identity layer
        eite_init(identity_id=cfg['name'], workspace=cfg.get("workspace", ""))
        eite_verify = get_verify()
        if eite_verify:
            self.system_prompt += eite_verify.get_identity_marker()
            self.eite = eite_verify
            logger.info(f"EITE identity bound: {cfg['name']}")
        else:
            self.eite = None

        logger.info(
            f"Worker initialized: name={self.name} "
            f"model={cfg.get('ai_model', '?')} "
            f"channels={len(self.channels)} "
            f"prompt_len={len(self.system_prompt)}"
        )

    def _load_pending(self) -> dict | None:
        try:
            if self._pending_task_file.exists():
                data = json.loads(self._pending_task_file.read_text())
                self._pending_task_file.unlink(missing_ok=True)
                return data
        except Exception:
            pass
        return None

    def _save_pending(self, task: str, iteration: int = 0):
        try:
            self._pending_task_file.parent.mkdir(parents=True, exist_ok=True)
            self._pending_task_file.write_text(json.dumps({
                "task": task, "iteration": iteration, "source": "continuation"
            }))
        except Exception as e:
            logger.warning(f"Failed to save pending task: {e}")

    def _get_hostname(self) -> str:
        import socket
        try:
            return socket.gethostname()
        except Exception:
            return "unknown"

    def _anchor_ping(self, cfg: dict):
        """Ping the live anchor server on startup."""
        import json, urllib.request, threading
        name = cfg.get("name", "unknown")
        anchor_url = os.environ.get("ANCHOR_URL", "https://bench.ticalasi.com/anchor")
        payload = json.dumps({
            "name": name, "hostname": self._get_hostname(),
            "status": "online", "version": f"EITElite {cfg.get('ai_model','?')}",
        }).encode()
        def _do_ping():
            try:
                req = urllib.request.Request(
                    anchor_url, data=payload,
                    headers={"Content-Type": "application/json"}, method="POST",
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    logger.info(f"Anchor ping OK ({name})")
            except Exception as e:
                logger.warning(f"Anchor ping failed: {e}")
        threading.Thread(target=_do_ping, daemon=True).start()

    # ------------------------------------------------------------------
    # Anchor API helper
    # ------------------------------------------------------------------

    def _anchor_api(self, path: str, method: str = "GET", data: dict | None = None) -> dict | None:
        """Make an HTTP call to the live anchor server. Returns parsed JSON or None."""
        import json, urllib.request, urllib.error
        anchor_url = os.environ.get("ANCHOR_URL", "https://bench.ticalasi.com/anchor")
        # Root anchor path uses the URL as-is; other paths append
        if path.strip("/") in ("", "anchor"):
            url = anchor_url
        else:
            url = f"{anchor_url.rstrip('/')}/{path.lstrip('/')}"
        try:
            payload = json.dumps(data).encode() if data else None
            req = urllib.request.Request(
                url, data=payload,
                headers={"Content-Type": "application/json"}, method=method,
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None  # no workers / no tasks — fine
            logger.warning(f"anchor_api HTTP {e.code}: {path}")
            return None
        except Exception as e:
            logger.debug(f"anchor_api {method} {path}: {e}")
            return None

    # ------------------------------------------------------------------
    # Rescue check — find orphaned tasks from dead workers
    # ------------------------------------------------------------------

    def _rescue_check(self) -> list[dict]:
        """Check anchor for dead workers with incomplete tasks."""
        data = self._anchor_api("anchor")
        if not data or not isinstance(data, dict):
            return []
        vps = data.get("vps", {})
        orphans = []
        for name, info in vps.items():
            if name == self.name:
                continue  # skip self
            alive = info.get("alive", False)
            current = info.get("current_task", "")
            result = info.get("result", "")
            if not alive and current and not result:
                orphans.append({
                    "name": name, "task": current,
                    "progress": info.get("progress", ""),
                })
        return orphans

    # ------------------------------------------------------------------
    # Autonomous cycle — execute queued tasks without human input
    # ------------------------------------------------------------------

    def _autonomous_cycle(self) -> bool:
        """Run one autonomous cycle: rescue → dequeue → execute → report.
        
        Returns True if a task was processed, False if queue was empty.
        This blocks during LLM processing (uses _handle_message).
        """
        # 1. Rescue — claim orphaned tasks
        orphans = self._rescue_check()
        if orphans:
            for o in orphans:
                logger.warning(
                    f"[autonomous] orphaned task from {o['name']}: "
                    f"{o['task'][:60]} ({o.get('progress','')})"
                )
                # Mark this worker as having taken over
                self._anchor_api("anchor", "POST", {
                    "name": self.name, "status": "online",
                    "current_task": f"[rescue:{o['name']}] {o['task']}",
                    "progress": o.get("progress", "0%"), "task_type": "rescue",
                })

        # 2. Dequeue — pick up next task assigned to this worker
        data = self._anchor_api("task/dequeue", "POST", {"worker": self.name})
        if not data or not isinstance(data, dict):
            return False
        task_obj = data.get("task", {}) if isinstance(data.get("task"), dict) else {}
        task_desc = task_obj.get("task", data.get("task", "")) if task_obj else data.get("task", "")
        task_id = task_obj.get("id", data.get("task_id", 0))
        if not task_desc:
            return False

        if isinstance(task_desc, dict):
            task_desc = str(task_desc.get("task", task_desc))
        task_desc_str = str(task_desc)[:100]

        logger.info(f"[autonomous] dequeued task #{task_id}: {task_desc_str}")

        # 3. Report start to anchor
        self._anchor_api("anchor", "POST", {
            "name": self.name, "status": "online",
            "current_task": task_desc_str, "progress": "0%",
            "task_type": "autonomous",
        })

        # 4. Execute via synthetic message (reuses full LLM + tool loop)
        msg = Message(
            sender="system",
            content=f"[autonomous] {task_desc_str}",
            source="system",
            chat_id="autonomous",
        )
        self._handle_message(None, msg)

        # 5. Mark complete on anchor
        self._anchor_api("task/complete", "POST", {
            "task_id": task_id, "result": "done via autonomous cycle",
            "status": "done",
        })
        self._anchor_api("anchor", "POST", {
            "name": self.name, "status": "online",
            "current_task": "", "progress": "", "task_type": "",
            "result": f"done: {task_desc_str}",
        })
        logger.info(f"[autonomous] completed task #{task_id}")
        return True

    def run(self):
        """Main loop: poll channels → handle messages → autonomous → sleep."""
        logger.info(f"Worker {self.name} entering main loop")
        while True:
            had_messages = False
            try:
                for channel in self.channels:
                    messages = channel.poll()
                    if messages:
                        had_messages = True
                    for msg in messages:
                        try:
                            self._handle_message(channel, msg)
                        except Exception as e:
                            logger.error(
                                f"handle error: {e}\n{traceback.format_exc()}"
                            )
                            if isinstance(msg, str):
                                logger.warning(f"msg is string: {msg[:100]}")
                            elif channel:
                                channel.send(Response(
                                    content=f"[worker] error: {e}",
                                    target=msg.sender,
                                    source=msg.source,
                                    chat_id=msg.chat_id,
                                ))
            except Exception as e:
                logger.error(f"poll error: {e}\n{traceback.format_exc()}")

            # Check for pending task continuation (always, even if had messages)
            if self._pending_task:
                task = self._pending_task
                self._pending_task = None
                msg = Message(
                    sender="system",
                    content=f"[continue] {task['task']}",
                    source="system",
                )
                try:
                    self._handle_message(None, msg)
                except Exception as e:
                    logger.error(f"pending task error: {e}\n{traceback.format_exc()}")

            # Autonomous cycle when idle (no messages, no pending task)
            if not had_messages and not self._pending_task:
                try:
                    self._autonomous_cycle()
                except Exception as e:
                    logger.error(f"autonomous cycle error: {e}\n{traceback.format_exc()}")

            # Periodic anchor ping every 60s to keep alive
            now = time.time()
            if not hasattr(self, '_last_ping_time') or now - self._last_ping_time > 60:
                try:
                    self._last_ping_time = now
                    self._anchor_api("anchor", "POST", {
                        "name": self.name,
                        "hostname": self._get_hostname(),
                        "status": "online",
                    })
                except Exception:
                    pass

            # Vigil patrol every 5 min
            if self._vigil_enabled and hasattr(self, 'vigil'):
                import asyncio
                try:
                    asyncio.run(self.vigil.patrol())
                except Exception:
                    pass
            time.sleep(1)

    def _execute_direct(self, msg, channel):
        """Execute a seoul command directly (no LLM). Bypasses proposal gate."""
        cmd = msg.content.strip()
        if not cmd:
            result = "[error] empty command"
        else:
            logger.info(f"[direct] execute: {cmd[:100]}")
            try:
                proc = subprocess.run(
                    cmd, shell=True, capture_output=True, text=True, timeout=120
                )
                output = proc.stdout
                if proc.stderr:
                    output += f"\n[stderr]\n{proc.stderr}"
                result = output.strip() if output.strip() else f"[exit {proc.returncode}] (no output)"
            except subprocess.TimeoutExpired:
                result = "[error] command timed out (120s)"
            except Exception as e:
                result = f"[error] {e}"
        logger.info(f"[direct] result: {result[:100]}")
        channel.send(Response(
            content=result,
            target=msg.sender,
            source=msg.source,
            chat_id=msg.chat_id,
        ))

    def _handle_message(self, channel, msg: Message):
        # Guard: convert string to Message
        if isinstance(msg, str):
            msg = Message(sender="system", content=msg, source="tical-chat")
        """Process a single message through LLM + tools.

Seoul messages via tical-chat use direct execution (no LLM).
All other messages enter the LLM conversation loop.
"""
        logger.info(
            f"[{msg.source}] {msg.sender}: {msg.content[:100]}"
        )

        # === Direct execution: seoul commands via tical-chat ===
        # Commands starting with typical bash prefixes go direct
        # All other messages (research, analysis) go through LLM
        if msg.sender == "seoul" and msg.source == "tical-chat":
            import re
            cmd = msg.content.strip().split(chr(10))[0].strip()
            if re.match(r'^(echo |ls |cat |grep |cd |head |tail |wc |find |ps |df |free |uname |date |cp |mv |rm |mkdir |sudo |curl |wget |pip |apt |systemctl |git |python3 |bash |chmod |chown |ln |tar |scp |ssh |screen |kill |pkill )', cmd):
                self._execute_direct(msg, channel)
                return

        # Vigil: evaluate instruction before LLM
        if self._vigil_enabled and msg.sender != "system":
            try:
                verdict = self.vigil.evaluate_instruction(NewInstruction(content=msg.content))
                if verdict.action == "reject":
                    logger.info(f"[vigil] rejected: {msg.content[:50]}")
                    if channel:
                        channel.send(Response(
                            content="(filtered)", target=msg.sender,
                            source=msg.source, chat_id=msg.chat_id,
                        ))
                    return
                if verdict.action == "queue":
                    logger.info(f"[vigil] queued: {msg.content[:50]}")
                    if channel:
                        channel.send(Response(
                            content=verdict.notify_message or "(queued)",
                            target=msg.sender, source=msg.source,
                            chat_id=msg.chat_id,
                        ))
                    return
            except Exception as e:
                logger.warning(f"[vigil] error: {e}")

        # Lock reply target — workers ALWAYS reply to seoul, never to other workers
        # This prevents A↔B ping-pong loops between workers
        import tical_code.core.tool_executor as _te
        if msg.sender in WORKER_IDS and msg.sender != "seoul":
            _te._reply_target = "seoul"
            logger.info(f"[worker] worker msg from {msg.sender}, reply→seoul")
        else:
            _te._reply_target = msg.sender

        # Reset EITE session tracking for this turn
        if hasattr(self, "eite") and self.eite:
            self.eite.reset_session()

        # Process through LLM + tools
        self._process(channel, msg)

    def _process(self, channel, msg: Message) -> None:
        """Process a single incoming message — guaranteed to produce a reply."""
        # Deadlock busters: reset all guards for each incoming message
        self.gate.clear_pending()
        self.loop_detector.reset()
        self._consecutive_blocks = 0
        
        # Init conversation with system prompt + memory injection
        from tical_code.core.tool_executor import get_memory_injection as _mem_inject
        _mem_text = _mem_inject()
        _sys = self.system_prompt
        if _mem_text:
            _sys += "\n\n═══════════════════════════\nPERSISTENT MEMORY (loaded fresh each turn):\n" + _mem_text
        conv = [{"role": "system", "content": _sys}]
        # Load session history
        session_id = self.sessions.get_session_id(msg.source, str(msg.chat_id))
        history = self.sessions.load_session(session_id)
        if history:
            conv.extend(history)
        conv.append({"role": "user", "content": msg.content})

        max_iterations = 60
        for iteration in range(max_iterations):
            # Module 2: Context compaction - trim if over token limit
            if self.compactor.needs_compaction(conv):
                conv = self.compactor.compact(conv, lambda msgs: {"content": ""})
                logger.info(f"[worker] context compacted: {len(conv)} messages")
            try:
                response = self.llm.chat(conv, tools=TOOL_SCHEMAS_CLEAN)
            except Exception as e:
                import traceback as _tb
                _tb.print_exc()
                error_str = str(e)
                hint = ""
                if "401" in error_str or "402" in error_str or "403" in error_str:
                    hint = " API key error — use switch_model to set a valid key."
                elif "400" in error_str:
                    hint = " Model may be unavailable — use list_models + switch_model to change."
                elif "429" in error_str or "rate" in error_str.lower():
                    hint = " Rate limited — retry or switch_model to a different model."
                elif "timeout" in error_str.lower() or "timed out" in error_str.lower():
                    hint = " API timed out — retry or switch_model to a faster model."
                from tical_code.core.llm_interface import ChatResponse
                response = ChatResponse(content=f"[API error: {error_str[:80]}.{hint}]")
                logger.warning(f"  LLM call failed: {error_str[:100]}")
            content = response.content
            tool_calls = response.tool_calls

            if tool_calls:
                # Track which tcs got a tool response
                responded = set()

                # Add assistant response with tool_calls to conversation
                formatted_tcs = [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.name,
                                  "arguments": json.dumps(tc.arguments)}}
                    for tc in tool_calls
                ]
                conv.append({
                    "role": "assistant",
                    "content": response.content,
                    "reasoning_content": response.reasoning_content,
                    "tool_calls": formatted_tcs,
                })
                loop_messages = []
                for tc in tool_calls:
                    name = tc.name
                    args = tc.arguments
                    tc_id = tc.id
                    logger.info(f"  tool call: {name}")

                    # Gate write operations — bypass for non-interactive sources
                    if self.gate.should_confirm(name, args, msg.source) and msg.source in ("telegram",):
                        # Check if LLM just confirmed this exact proposal
                        pending = self.gate.get_pending_action()
                        if pending and pending["tool_name"] == name and pending["args"] == args:
                            # Already confirmed — execute and clear
                            self.gate.clear_pending()
                        else:
                            proposal = self.gate.create_proposal(name, args)
                            conv.append({
                                "role": "tool",
                                "tool_call_id": tc_id,
                                "content": proposal["message"],
                            })
                            responded.add(tc_id)
                            continue

                    result = execute(name, args, base_dir=self.workspace)
                    formatted = format_result(name, result)
                    # EITE: verify tool result
                    if hasattr(self, "eite") and self.eite:
                        result = self.eite.verify_tool_result(name, args, result)
                        logger.info(f"  verify {name}: {result.get('verified')} ({result.get('verify_detail', '')})")
                        if not result.get("verified"):
                            self._consecutive_blocks += 1
                            if self._consecutive_blocks >= 2:
                                conv.append({
                                    "role": "system",
                                    "content": f"[DEADLOCK] {name} keeps getting blocked. STOP trying tools and reply directly to the user with what you have."
                                })
                                logger.warning(f"  deadlock break: {self._consecutive_blocks} consecutive blocks")
                                break  # exit tool loop, force reply
                            conv.append({
                                "role": "tool",
                                "tool_call_id": tc_id,
                                "content": f"[BLOCKED] {name}: {result.get('verify_detail', '?')}. Stop - try a different approach or reply directly.",
                            })
                            responded.add(tc_id)
                            continue
                        else:
                            self._consecutive_blocks = 0
                    if not formatted:
                        formatted = json.dumps(result, ensure_ascii=False)[:500]

                    conv.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": formatted,
                    })
                    responded.add(tc_id)

                    # Module 4: Record action
                    verified = result.get("ok", False) or result.get("exit_code") == 0
                    self.reporter.record_action(name, args, result, verified=verified)
                    
                    # Auto-save: save_important results as memory
                    if verified and name in ("execute_code", "web_search", "web_fetch", "memory_fts_search"):
                        try:
                            from tical_code.core.tool_executor import exec_memory as _exec_mem
                            result_text = str(result.get("output", result.get("content", "")))[:200]
                            if result_text.strip():
                                _exec_mem({
                                    "action": "add",
                                    "target": "memory",
                                    "content": f"[{name}] {args.get('code', args.get('query', ''))[:80]}: {result_text[:80]}",
                                })
                        except Exception:
                            pass

                    # Module 3: Record & detect loop
                    self.loop_detector.record(name, args, result)
                    loop_result = self.loop_detector.detect()
                    if loop_result:
                        loop_messages.append(loop_result["message"])
                        if loop_result["level"] == "critical":
                            self.loop_detector.reset()
                            logger.info("  loop critical — detector reset")
                            break

                # Fill missing tool responses FIRST (must be adjacent to tool_calls)
                for tc in tool_calls:
                    tc_id = tc.id
                    if tc_id not in responded:
                        conv.append({
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": "[interrupted]",
                        })

                # Now safe to add system messages (all tool_call_ids have responses)
                # EITE: per-iteration all-blocked check - only for SAFETY blocks, not execution failures
                if hasattr(self, "eite") and self.eite:
                    session = getattr(self.eite, "_session_tools", [])
                    it_tools = session[-len(tool_calls):] if len(tool_calls) > 0 else []
                    # Only count as "blocked" if result had a safety policy error
                    safety_blocked = [
                        t for t in it_tools
                        if t.get("verified") == False
                        and ("blocked" in str(t.get("detail", "")).lower()
                            or "safety" in str(t.get("detail", "")).lower())
                    ]
                    if len(safety_blocked) == len(tool_calls) and len(tool_calls) > 0:
                        conv.append({
                            "role": "system",
                            "content": "Some tool calls were blocked by safety policy (write-restricted). You CAN write inside " + self.workspace + " using bash or file_write. Try: cd to workspace, use echo/cat/heredoc inside the workspace. Do NOT give up — retry with the correct path."
                        })
                        logger.info("  all blocked - injected system hint")

                # Append accumulated loop detector messages
                for lm in loop_messages:
                    conv.append({"role": "system", "content": lm})

                # Iteration guard — scale with max_iterations
                if iteration >= min(60, max_iterations - 20):
                    conv.append({
                        "role": "system",
                        "content": f"You have used {iteration + 1} rounds. Finish your task and reply to the user.",
                    })
                if iteration >= min(60, max_iterations - 10):
                    conv.append({
                        "role": "system",
                        "content": "STOP calling tools. Reply now.",
                    })
            else:
                # Text response
                reply = content or "[worker] no response"

                # Check if this confirms a pending proposal
                pending_action = self.gate.get_pending_action()
                if pending_action:
                    confirmation = self.gate.check_user_response(reply)
                    if confirmation == "confirmed":
                        name = pending_action["tool_name"]
                        args = pending_action["args"]
                        self.gate.clear_pending()
                        logger.info(f"  confirmed proposal → execute {name}")
                        result = execute(name, args, base_dir=self.workspace)
                        formatted = format_result(name, result)
                        # Add tool result to conv and continue loop
                        conv.append({
                            "role": "tool",
                            "tool_call_id": "confirmed",
                            "content": formatted,
                        })
                        continue
                    elif confirmation == "rejected":
                        self.gate.clear_pending()
                        reply = f"Cancelled: {pending_action['tool_name']}"
                    else:
                        # unclear - keep waiting
                        conv.append({
                            "role": "system",
                            "content": "Please clearly confirm or cancel the pending proposal."
                        })
                        continue

                # Module 4: Scan for violations — append correction to reply
                violations = self.reporter.scan_reply(reply)
                if violations:
                    corrections = self.reporter.format_corrections(violations)
                    logger.warning(f"trust violations: {violations}")
                    reply += f"\n\n{corrections}"

                # Check for continuation hint — only if explicit "I still need to"
                if "I still need to" in reply:
                    next_task = reply.split("I still need to", 1)[-1].strip()
                    self._save_pending(next_task, iteration)
                    reply += f"\n\n[task queued: {next_task[:60]}]"

                if channel:
                    channel.send(Response(
                        content=reply,
                        target=msg.sender,
                        source=msg.source,
                        chat_id=msg.chat_id,
                    ))
                logger.info(f"  reply: {reply[:80]}")

                # Post-reply verification: check if LLM claimed files that don't exist
                import re as _re
                claimed_paths = _re.findall(r'(?<!\w)(/[\w\-./]+\.\w{2,4})', reply)
                missing = []
                for _p in claimed_paths[:10]:
                    _resolved = Path(_p).expanduser()
                    if _resolved.exists() or not _resolved.parent.exists():
                        continue
                    if any(kw in reply.lower() for kw in ["created", "wrote", "writes", "saved", "set up"]):
                        missing.append(_p)
                if missing:
                    logger.warning(f"[worker] hallucination detected: claimed files: {missing}")
                    if channel:
                        channel.send(Response(
                            content=f"⚠ Verification: {missing} do not exist. Do not claim files created unless verified.",
                            target=msg.sender,
                            source=msg.source,
                            chat_id=msg.chat_id,
                        ))

                # Module 1: Save conversation
                new_messages = [{"role": "assistant", "content": reply}]
                session_id = self.sessions.get_session_id(msg.source, str(msg.chat_id))
                self.sessions.save_messages(session_id, new_messages)
                return

        # Exceeded max iterations — log only, don't reply (avoids chat noise)
        logger.warning(f"[worker] {msg.sender}: exceeded max tool iterations")
        # Silently save continuation if any context remains
        if conv and len(conv) > 2:
            last_assistant = next(
                (m["content"] for m in reversed(conv) if m.get("role") == "assistant"),
                None
            )
            if last_assistant and "I still need to" in last_assistant:
                self._save_pending(last_assistant, max_iterations)

def main():
    logger.info("tical-code worker starting")
    cfg = load_config()
    worker = Worker(cfg)
    worker.run()

if __name__ == "__main__":
    main()