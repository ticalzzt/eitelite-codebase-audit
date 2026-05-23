"""tical-code unified worker - main loop.

Replaces ticobot_worker_v0.10.0.py and worker_loop.py.
Single loop: poll channels → LLM call → tool execute → format → reply.
"""
import json
import logging
import subprocess
import sys
import time
import traceback
from pathlib import Path

# Add parent dir to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tical_code.core.channel import Message, Response, TelegramChannel, TicalChatChannel
from tical_code.core.llm_backend import create_llm_backend
from tical_code.core.tool_executor import execute, TOOL_SCHEMAS
from tical_code.core.response_formatter import format_result, format_error, format_progress
from tical_code.core.eite import init as eite_init, get_verify
from tical_code.core.prompt import build_system_prompt
from tical_code.core.config import load_config
from tical_code.core.modules.session_manager import SessionManager
from tical_code.core.modules.context_compactor import ContextCompactor
from tical_code.core.modules.loop_detector import LoopDetector
from tical_code.core.modules.truthful_reporter import TruthfulReporter
from tical_code.core.modules.proposal_gate import ProposalGate
from tical_code.vigil import build_vigil, NewInstruction

# Known AI worker names — used to detect worker-to-worker messages
# Workers must NOT reply to each other (creates A↔B ping-pong loops)
WORKER_IDS = {"seoul", "tico", "ani", "kael", "tico-oracle", "test"}

logger = logging.getLogger("tical-code.worker")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)

# Clean TOOL_SCHEMAS: remove bash_execute if present
TOOL_SCHEMAS_CLEAN = [
    s for s in TOOL_SCHEMAS if s["function"]["name"] != "bash_execute"
]

# Tool call limits
MAX_TOOL_ITERATIONS = 8
SOFT_HINT_AT = 5   # gentle nudge to wrap up
HARD_STOP_AT = 8   # force stop

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
        self.llm = create_llm_backend(
            model=cfg.get("ai_model", "deepseek-v4-flash"),
            api_key=cfg.get("ai_key", ""),
            base_url=cfg.get("ai_endpoint", ""),
        )

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

        # Vigil — AI safety runtime
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

    def run(self):
        """Main loop: poll channels → handle messages → sleep."""
        logger.info(f"Worker {self.name} entering main loop")
        while True:
            try:
                for channel in self.channels:
                    messages = channel.poll()
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

            # Check for pending task continuation
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

        conv = [
            {"role": "system", "content": self.system_prompt},
        ]
        # Module 1: Load previous session context
        session_id = self.sessions.get_session_id(msg.source, str(msg.chat_id))
        history = self.sessions.load_session(session_id)
        conv.extend(history)
        conv.append({"role": "user", "content": msg.content})

        max_iterations = 60
        for iteration in range(max_iterations):
            # Module 2: Context compaction - trim if over token limit
            if self.compactor.needs_compaction(conv):
                conv = self.compactor.compact(conv, lambda msgs: {"content": ""})
                logger.info(f"[worker] context compacted: {len(conv)} messages")
            response = self.llm.call(conv, tools=TOOL_SCHEMAS_CLEAN)
            content = response.get("content", "")
            tool_calls = response.get("tool_calls", [])

            if tool_calls:
                # Track which tcs got a tool response
                responded = set()

                # Add assistant response with tool_calls to conversation
                formatted_tcs = [
                    {"id": tc["id"], "type": "function",
                     "function": {"name": tc["name"],
                                  "arguments": json.dumps(tc.get("args", {}))}}
                    for tc in tool_calls
                ]
                conv.append({
                    "role": "assistant",
                    "content": response.get("content"),
                    "reasoning_content": response.get("reasoning_content", ""),
                    "tool_calls": formatted_tcs,
                })
                loop_messages = []
                for tc in tool_calls:
                    name = tc.get("name", "?")
                    args = tc.get("args", {})
                    tc_id = tc.get("id", "")
                    logger.info(f"  tool call: {name}")

                    # Gate write operations
                    if self.gate.should_confirm(name, args, msg.source):
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
                            conv.append({
                                "role": "tool",
                                "tool_call_id": tc_id,
                                "content": f"[BLOCKED] {name}: {result.get('verify_detail', '?')}. Stop - try a different approach or reply directly.",
                            })
                            responded.add(tc_id)
                            continue
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

                    # Module 3: Record & detect loop
                    self.loop_detector.record(name, args, result)
                    loop_result = self.loop_detector.detect()
                    if loop_result:
                        loop_messages.append(loop_result["message"])
                        if loop_result["level"] == "critical":
                            break

                # Fill missing tool responses FIRST (must be adjacent to tool_calls)
                for tc in tool_calls:
                    tc_id = tc.get("id", "")
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

                # EITE: scan reply
                if hasattr(self, "eite") and self.eite:
                    warnings = self.eite.scan_reply(reply)
                    if warnings:
                        logger.warning(f"EITE unverified claims: {warnings}")

                # Module 4: Scan for violations
                violations = self.reporter.scan_reply(reply)
                if violations:
                    reply += "\n" + self.reporter.format_corrections(violations)

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