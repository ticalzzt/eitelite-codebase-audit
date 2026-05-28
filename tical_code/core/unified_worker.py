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
from tical_code.core.llm_backend import create_llm_backend
from tical_code.core.tool_executor import execute, TOOL_SCHEMAS
from tical_code.core.response_formatter import format_result, format_error, format_progress
from tical_code.core.eite.verify_engine_v2 import VerificationEngine
from tical_code.core.prompt import build_system_prompt
from tical_code.core.config import load_config
from tical_code.core.modules.session_manager import SessionManager
from tical_code.core.modules.context_compactor import ContextCompactor
from tical_code.core.modules.loop_detector import LoopDetector
from tical_code.core.trace_recorder import TraceRecorder
from tical_code.core.trace.verification_recorder import VerificationEventRecorder
from tical_code.core.config import get_data_collection_config
from tical_code.core.modules.proposal_gate import ProposalGate

# Known AI worker names — used for CMD protocol identity detection
WORKER_IDS = {"seoul", "tico", "ani", "kael", "tico-oracle", "test"}

# === [CMD] Protocol — AI Management Layer ===
CMD_LEVEL_MASTER = 0  # 主人 — full access
CMD_LEVEL_ADMIN  = 1  # AI admin (seoul)
CMD_LEVEL_WORKER = 2  # Worker — self-manage only

MASTER_IDS = {"tical", "tiCal", "zizetu"}

CMD_PERMISSIONS = {
    "deploy":   CMD_LEVEL_ADMIN,
    "status":   CMD_LEVEL_ADMIN,
    "restart":  CMD_LEVEL_WORKER,
    "exec":     CMD_LEVEL_MASTER,
    "report":   CMD_LEVEL_ADMIN,
    "escalate": CMD_LEVEL_WORKER,
    "ping":     CMD_LEVEL_WORKER,
    "help":     CMD_LEVEL_WORKER,
    "log":      CMD_LEVEL_WORKER,  # read-only: list/search/export conversations
}

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
        self.compactor = ContextCompactor(max_tokens=200000, keep_recent=20)
        self.loop_detector = LoopDetector(window_size=30)
        # VerificationEngine — single source of truth for all verification
        self.verification = VerificationEngine(
            identity_id=cfg['name'],
            workspace=cfg.get("workspace", ""),
        )
        # TraceRecorder - 0号模型训练数据采集
        dc = get_data_collection_config(cfg)
        self.tracer = TraceRecorder(system_name=cfg.get('name', 'eitelite'), enabled=dc['enabled'])
        # VerificationEventRecorder — captures verification signals for training
        self.verif_recorder = VerificationEventRecorder()
        if dc['enabled']:
            logger.info('TraceRecorder active -> %s' % dc['target_url'])
        self.gate = ProposalGate(timeout_seconds=300)
        self._evidence_retry_count = 0

        self.system_prompt = build_system_prompt(
            name=cfg['name'],
            hostname=self._get_hostname(),
            deploy_path=cfg.get("workspace", ""),
            target_model=cfg.get("ai_model", ""),
        )

        # EITE identity layer — now integrated into VerificationEngine
        self.system_prompt += self.verification.get_identity_marker()
        logger.info(f"EITE identity bound: {cfg['name']}")

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
        except Exception as e:
            logger.debug(f"[{filepath.stem}] swallowed: {e}")
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

    # ------------------------------------------------------------------
    # [CMD] Protocol — direct command execution (no LLM)
    # ------------------------------------------------------------------

    def _cmd_get_level(self, sender: str, msg: Message) -> int:
        """Determine authority level for a [CMD] sender."""
        if sender.lower() in {m.lower() for m in MASTER_IDS}:
            return CMD_LEVEL_MASTER
        if sender in WORKER_IDS:
            if sender == "seoul":
                return CMD_LEVEL_ADMIN
            return CMD_LEVEL_WORKER
        if msg.source in ("telegram", "weixin"):
            return CMD_LEVEL_MASTER
        return CMD_LEVEL_WORKER

    def _send_cmd_reply(self, channel, msg: Message, text: str) -> None:
        logger.info(f"[CMD] reply to {msg.sender}: {text[:80]}")
        if channel:
            channel.send(Response(
                content=text, target=msg.sender,
                source=msg.source, chat_id=msg.chat_id,
            ))

    def _exec_cmd(self, cmd_name: str, cmd_args: list[str],
                  msg: Message, channel) -> str:
        """Execute a single CMD command locally."""
        if cmd_name == "ping":
            import socket
            return f"pong from {self.name}@{socket.gethostname()}"

        if cmd_name == "help":
            lines = [
                "[CMD] Protocol — available commands:", "",
            ]
            for c, lvl in sorted(CMD_PERMISSIONS.items()):
                level_name = ["MASTER", "ADMIN", "WORKER"][lvl]
                lines.append(f"  {c:12s} [{level_name}]")
            lines.append("")
            lines.append("Usage: [CMD] <command> [target:worker] [args...]")
            lines.append("  target:worker — forward to another worker")
            lines.append("  Level 2 workers can only target themselves")
            return "\n".join(lines)

        if cmd_name == "status":
            import socket
            lines = [
                f"Worker: {self.name}",
                f"Host:   {socket.gethostname()}",
                f"Model:  {self.cfg.get('ai_model', '?')}",
                f"Venv:   {sys.prefix}",
            ]
            try:
                import subprocess as _sp
                _r = _sp.run(["git", "log", "--oneline", "-1"],
                             capture_output=True, text=True, timeout=5)
                if _r.returncode == 0:
                    lines.append(f"Git:    {_r.stdout.strip()[:60]}")
            except Exception as e:
                logger.debug(f"[{filepath.stem}] swallowed: {e}")
            return "\n".join(lines)

        if cmd_name == "report":
            import socket
            import subprocess as _sp
            lines = [
                f"==============================",
                f"  {self.name.upper()} System Report",
                f"==============================",
                f"Worker:   {self.name}",
                f"Host:     {socket.gethostname()}",
                f"Model:    {self.cfg.get('ai_model', '?')}",
            ]
            try:
                _r = _sp.run(["uptime"], capture_output=True, text=True, timeout=5)
                lines.append(f"Uptime:   {_r.stdout.strip()}")
            except Exception as e:
                logger.debug(f"[{filepath.stem}] swallowed: {e}")
            try:
                _r = _sp.run(["git", "log", "--oneline", "-3"],
                             capture_output=True, text=True, timeout=5)
                if _r.returncode == 0:
                    lines.append(f"Recent:   {_r.stdout.strip()}")
            except Exception as e:
                logger.debug(f"[{filepath.stem}] swallowed: {e}")
            return "\n".join(lines)

        if cmd_name == "deploy":
            import subprocess as _sp
            _result_parts = []
            try:
                _r = _sp.run(["git", "pull"], capture_output=True, text=True, timeout=60)
                _result_parts.append(f"git pull: {_r.stdout.strip()[:200]}")
                if _r.stderr:
                    _result_parts.append(f"  stderr: {_r.stderr.strip()[:200]}")
            except Exception as e:
                _result_parts.append(f"git pull error: {e}")
            try:
                from tical_code.core.tool_executor import execute as _exec
                r = _exec("restart_self", {}, base_dir=self.workspace)
                _result_parts.append(f"restart: {str(r)[:100]}")
            except Exception as e:
                _result_parts.append(f"restart error: {e}")
            return "\n".join(_result_parts)

        if cmd_name == "restart":
            try:
                from tical_code.core.tool_executor import execute as _exec
                r = _exec("restart_self", {}, base_dir=self.workspace)
                return f"[CMD] restart: {str(r)[:100]}"
            except Exception as e:
                return f"[CMD] restart error: {e}"

        if cmd_name == "escalate":
            _reason = " ".join(cmd_args) or "no details"
            try:
                from tical_code.core.tool_executor import execute as _exec
                _exec("chat_send", {
                    "target": "seoul",
                    "content": f"[ESCALATION from {self.name}] {_reason}",
                }, base_dir=self.workspace)
                return f"[CMD] escalated to seoul: {_reason[:100]}"
            except Exception as e:
                return f"[CMD] escalate error: {e}"

        if cmd_name == "exec":
            payload = " ".join(cmd_args)
            if not payload:
                return "[CMD] exec: empty command"
            import subprocess as _sp
            try:
                _r = _sp.run(payload, shell=True, capture_output=True,
                             text=True, timeout=120)
                _out = _r.stdout.strip() or f"(exit {_r.returncode})"
                if _r.stderr:
                    _out += f"\n[stderr]\n{_r.stderr.strip()[:500]}"
                return _out[:2000]
            except _sp.TimeoutExpired:
                return "[CMD] exec timeout (120s)"
            except Exception as e:
                return f"[CMD] exec error: {e}"

        if cmd_name == "log":
            """Query tical-chat conversation archive via API."""
            import urllib.request, urllib.error, json as _json
            _chat_url = self.cfg.get("chat_url", "").rstrip("/")
            _key = self.cfg.get("chat_key", "") or os.environ.get("TICAL_CHAT_KEY", "")
            if not cmd_args:
                _url = f"{_chat_url}/v1/conversations"
            elif cmd_args[0] == "search" and len(cmd_args) >= 2:
                _q = " ".join(cmd_args[1:])
                _url = f"{_chat_url}/v1/messages/search?q={urllib.parse.quote(_q)}"
            elif cmd_args[0] == "export" and len(cmd_args) >= 3:
                _s, _t = cmd_args[1], cmd_args[2]
                _url = f"{_chat_url}/v1/export?sender={_s}&target={_t}&format=markdown"
                try:
                    _req = urllib.request.Request(_url)
                    _req.add_header("X-AI-Key", _key)
                    with urllib.request.urlopen(_req, timeout=15) as _resp:
                        return _resp.read().decode("utf-8")[:3000]
                except Exception as e:
                    return f"[CMD] log export error: {e}"
            elif len(cmd_args) == 1 and cmd_args[0] == "tags":
                _url = f"{_chat_url}/v1/tags"
            elif len(cmd_args) >= 2 and cmd_args[0] == "classify":
                _limit = int(cmd_args[1]) if len(cmd_args) >= 2 and cmd_args[1].isdigit() else 10
                try:
                    _fetch_url = f"{_chat_url}/v1/messages/unclassified?limit={_limit}"
                    _req = urllib.request.Request(_fetch_url)
                    _req.add_header("X-AI-Key", _key)
                    with urllib.request.urlopen(_req, timeout=15) as _resp:
                        _unclassified = _json.loads(_resp.read())
                except Exception as e:
                    return f"[CMD] log classify fetch error: {e}"
                if not _unclassified.get("messages"):
                    return "[CMD] log classify: no unclassified messages found"
                _classified = 0
                _results = []
                for _m in _unclassified["messages"]:
                    _mid = _m["id"]
                    _content = _m["content"][:500]
                    try:
                        _prompt = (
                            "Classify this message from an AI management conversation.\n"
                            "Pick relevant categories from: 问题, 修复, 决策, 任务, 技术方案, 配置, 部署, 查询, 通知, 审计\n"
                            f"Message: {_content}\n\n"
                            "Respond with valid JSON ONLY: "
                            '{"tags": ["问题"], "summary": "one line summary in Chinese (max 60 chars)"}'
                        )
                        # tical-code uses self.llm.call()
                        _resp = self.llm.call([{"role": "user", "content": _prompt}])
                        _text = _resp.get("content", "").strip()
                        import re as _re
                        _json_match = _re.search(r'\{.*\}', _text, _re.DOTALL)
                        if _json_match:
                            _parsed = _json.loads(_json_match.group())
                            _tag_list = _parsed.get("tags", [])
                            _summary = _parsed.get("summary", "")
                            _tag_req = urllib.request.Request(
                                f"{_chat_url}/v1/messages/tag",
                                data=_json.dumps({"id": _mid, "tags": _tag_list, "summary": _summary}).encode(),
                                headers={"Content-Type": "application/json", "X-AI-Key": _key},
                                method="POST",
                            )
                            with urllib.request.urlopen(_tag_req, timeout=10):
                                _classified += 1
                                _results.append(f"  #{_mid}: {', '.join(_tag_list)} — {_summary[:40]}")
                    except Exception as _e:
                        _results.append(f"  #{_mid}: error - {str(_e)[:50]}")
                if not _results:
                    return "[CMD] log classify: classification failed for all messages"
                return f"[CMD] Classified {_classified}/{len(_unclassified['messages'])} messages:\n" + "\n".join(_results)
            elif len(cmd_args) == 1:
                _other = cmd_args[0]
                _url = f"{_chat_url}/v1/conversation?sender={self.name}&target={_other}&limit=20"
            elif len(cmd_args) >= 2:
                _url = f"{_chat_url}/v1/conversation?sender={cmd_args[0]}&target={cmd_args[1]}&limit=20"
            else:
                return "[CMD] log: unknown subcommand"
            try:
                _req = urllib.request.Request(_url)
                _req.add_header("X-AI-Key", _key)
                with urllib.request.urlopen(_req, timeout=15) as _resp:
                    _data = _json.loads(_resp.read())
                if "conversations" in _data:
                    _lines = ["[CMD] Conversations:", ""]
                    for c in _data["conversations"]:
                        _p = " ↔ ".join(c["participants"])
                        _lines.append(f"  {_p:40s} {c['message_count']:3d} msgs")
                    return "\n".join(_lines)
                elif "tags" in _data:
                    _lines = ["[CMD] Tags:", ""]
                    for t in _data["tags"]:
                        _lines.append(f"  {t['tag']:12s}  {t['count']:3d} messages")
                    return "\n".join(_lines)
                elif "results" in _data:
                    _lines = [f"[CMD] Search: '{_data.get('query','')}' ({_data['count']} results)", ""]
                    for m in _data["results"][:15]:
                        _lines.append(f"  {m['sender']:12s} → {m['target']:12s}  {m['content'][:80]}")
                    return "\n".join(_lines)
                elif "messages" in _data:
                    import datetime as _dt
                    _lines = [f"[CMD] Conversation: {_data.get('sender','?')} ↔ {_data.get('target','?')} ({_data['count']} msgs)", ""]
                    for m in _data["messages"][-15:]:
                        _ts = _dt.datetime.fromtimestamp(m["timestamp"]).strftime("%H:%M:%S")
                        _lines.append(f"  [{_ts}] {m['from']:12s} → {m['to']:12s}  {m['content'][:100]}")
                    return "\n".join(_lines)
                return f"[CMD] log: {_json.dumps(_data, ensure_ascii=False)[:300]}"
            except Exception as e:
                return f"[CMD] log error: {e}"

        return f"[CMD] unknown: {cmd_name}"

    def _handle_cmd(self, msg: Message, channel) -> None:
        """Handle a [CMD] protocol message — direct execution, no LLM."""
        content = msg.content.strip()
        after_prefix = content[len("[CMD]"):].strip()
        parts = after_prefix.split()
        if not parts:
            self._send_cmd_reply(channel, msg, "[CMD] error: empty command")
            return

        cmd_name = parts[0].lower()

        min_level = CMD_PERMISSIONS.get(cmd_name, CMD_LEVEL_MASTER)
        sender_level = self._cmd_get_level(msg.sender, msg)
        if sender_level > min_level:
            self._send_cmd_reply(
                channel, msg,
                f"[CMD] denied: {cmd_name} requires level {min_level}, "
                f"sender has level {sender_level}"
            )
            return

        target = None
        cmd_args = []
        for p in parts[1:]:
            if p.startswith("target:") or p.startswith("to:"):
                target = p.split(":", 1)[1]
            else:
                cmd_args.append(p)

        # Workers can only target themselves
        if target and sender_level == CMD_LEVEL_WORKER:
            if target != self.name:
                self._send_cmd_reply(
                    channel, msg,
                    f"[CMD] denied: workers can only target themselves"
                )
                return

        if target and target != self.name:
            try:
                from tical_code.core.tool_executor import execute as _exec
                _exec("chat_send", {"target": target, "content": content},
                      base_dir=self.workspace)
                self._send_cmd_reply(channel, msg, f"[CMD] forwarded {cmd_name} to {target}")
            except Exception as e:
                self._send_cmd_reply(channel, msg, f"[CMD] forward error: {e}")
            return

        result = self._exec_cmd(cmd_name, cmd_args, msg, channel)
        self._send_cmd_reply(channel, msg, result)

    def _handle_message(self, channel, msg: Message):
        """Process a single message through LLM + tools.

Seoul messages via tical-chat use direct execution (no LLM).
All other messages enter the LLM conversation loop.
"""
        # Guard: convert string to Message to prevent 'str' has no attribute 'source' bugs
        self._evidence_retry_count = 0
        if isinstance(msg, str):
            msg = Message(sender="system", content=msg, source="tical-chat")
        logger.info(
            f"[{msg.source}] {msg.sender}: {msg.content[:100]}"
        )

        # === [CMD] Protocol — direct execution (no LLM) ===
        # Supports both half-width [CMD] and full-width ［CMD］
        _content_stripped = msg.content.strip()
        if _content_stripped.startswith("[CMD]") or _content_stripped.startswith("［CMD］"):
            # Normalize full-width brackets to half-width for parsing
            if _content_stripped.startswith("［CMD］"):
                msg.content = _content_stripped.replace("［", "[").replace("］", "]")
            self._handle_cmd(msg, channel)
            return

        # === Direct execution: seoul commands via tical-chat ===
        # Commands starting with typical bash prefixes go direct
        # All other messages (research, analysis) go through LLM
        if msg.sender == "seoul" and msg.source == "tical-chat":
            import re
            cmd = msg.content.strip().split(chr(10))[0].strip()
            if re.match(r'^(echo |ls |cat |grep |cd |head |tail |wc |find |ps |df |free |uname |date |cp |mv |rm |mkdir |sudo |curl |wget |pip |apt |systemctl |git |python3 |bash |chmod |chown |ln |tar |scp |ssh |screen |kill |pkill )', cmd):
                self._execute_direct(msg, channel)
                return

        # Lock reply target — workers may only chat_send to the message sender
        import tical_code.core.tool_executor as _te

        # Reset verification session tracking for this turn
        self.verification.reset_session()
        # Start recording verification events for this turn
        self.verif_recorder.start_turn(msg.content)

        # TraceRecorder: task start
        if hasattr(self, 'tracer'):
            self.tracer.on_task_start(
                '%s_%s_%d' % (msg.source, msg.sender, int(__import__('time').time())),
                msg.content[:200],
            )

        conv = [
            {"role": "system", "content": self.system_prompt},
        ]
        # Load session history for context persistence
        session_id = self.sessions.get_session_id(msg.source, str(msg.chat_id))
        history = self.sessions.load_session(session_id)
        if history:
            conv.extend(history)
        # Build user message content - include media if available
        if hasattr(msg, 'media_data') and msg.media_data:
            content_parts = [{"type": "text", "text": msg.content}]
            for md in msg.media_data:
                if md["type"] == "image":
                    content_parts.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:{md['mime']};base64,{md['data']}"}
                    })
                elif md["type"] == "transcript":
                    content_parts.append({
                        "type": "text",
                        "text": f"[语音转写: {md['text']}]"
                    })
                elif md["type"] == "document_text":
                    content_parts.append({
                        "type": "text",
                        "text": f"[文件 {md.get('filename','?')} 内容: {md['text']}]"
                    })
            conv.append({"role": "user", "content": content_parts})
        else:
            conv.append({"role": "user", "content": msg.content})
        _new_start = len(conv) - 1  # track where new messages begin

        max_iterations = 60
        for iteration in range(max_iterations):
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

                    # Phase 1: Verify tool call (before execution)
                    phase1 = self.verification.verify_tool_call(name, args)
                    if not phase1.passed:
                        conv.append({
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": f"[BLOCKED] {name}: {phase1.violations[0].detail}. Stop - try a different approach or reply directly.",
                        })
                        responded.add(tc_id)
                        continue

                    result = execute(name, args, base_dir=self.workspace)
                    formatted = format_result(name, result)

                    # Phase 2: Verify tool output (after execution)
                    phase2 = self.verification.verify_tool_output(name, args, result)
                    # Record verification event for training data
                    self.verif_recorder.record_tool_call(name, args, result, phase2.passed)
                    if not phase2.passed:
                        for v in phase2.violations:
                            self.verif_recorder.record_violation(v.rule, v.category, v.claim, v.detail, v.severity)
                    logger.info(f"  verify {name}: passed={phase2.passed} ({phase2.violations[0].detail if phase2.violations else 'ok'})")
                    if not phase2.passed:
                        # High severity output issues → block
                        if any(v.severity == "high" for v in phase2.violations):
                            conv.append({
                                "role": "tool",
                                "tool_call_id": tc_id,
                                "content": f"[BLOCKED] {name}: {phase2.violations[0].detail}. Stop - try a different approach or reply directly.",
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

                    # Record action for verification (already done in verify_tool_output)
                    # TraceRecorder: record tool
                    if hasattr(self, 'tracer'):
                        verified = result.get("ok", False) or result.get("exit_code") == 0
                        self.tracer.on_tool_result(name, args, result, verified)

                    # Module 3: Record & detect loop
                    self.loop_detector.record(name, args, result)
                    loop_result = self.loop_detector.detect()
                    if loop_result:
                        loop_messages.append(loop_result["message"])
                        if loop_result["level"] == "critical":
                            break

                # Per-iteration: all tools blocked check
                if self.verification._session_tools:
                    it_tools = self.verification._session_tools[-len(tool_calls):] if len(tool_calls) > 0 else []
                    if len(it_tools) == len(tool_calls) and all(t.get("verified") == False for t in it_tools):
                        conv.append({
                            "role": "system",
                            "content": "All tool calls were blocked by safety policy. Reply directly to the user explaining what you cannot do."
                        })
                        logger.info("  all blocked - injected system hint")

                # Append accumulated loop detector messages after all tool responses
                for ld_msg in loop_messages:
                    conv.append({"role": "system", "content": ld_msg})

                # Fill missing tool responses to satisfy API requirement
                for tc in tool_calls:
                    tc_id = tc.get("id", "")
                    if tc_id not in responded:
                        conv.append({
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": "[interrupted]",
                        })

                # Iteration guard — use system role (no tool_call_id needed)
                if iteration >= 10:
                    conv.append({
                        "role": "system",
                        "content": f"You have used {iteration + 1} rounds. Finish your task and reply to the user.",
                    })
                if iteration >= 20:
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

                # Phase 3: Verify reply before sending
                self.verif_recorder._turn_buffer["initial_reply"] = reply
                phase3 = self.verification.verify_reply(reply)
                if not phase3.passed:
                    # Record violations for training data
                    for v in phase3.violations:
                        self.verif_recorder.record_violation(v.rule, v.category, v.claim, v.detail, v.severity)
                    if phase3.action == "block":
                        # Critical violations — force retry
                        retry_msg = "[VERIFICATION BLOCKED] " + "; ".join(phase3.corrections) + ". You MUST fix these issues and try again."
                        conv.append({"role": "system", "content": retry_msg})
                        self.verif_recorder.record_retry_instruction(retry_msg)
                        logger.warning(f"Reply blocked: {phase3.corrections}")
                    elif phase3.action == "retry":
                        # Annotate reply with verification notes instead of retrying
                        logger.info(f"Reply annotated: {phase3.corrections}")
                        reply += "\n" + "; ".join(f"[{c}]" for c in phase3.corrections)
                        break
                    elif phase3.action == "rewrite":
                        reply += "\n" + "; ".join(f"[{c}]" for c in phase3.corrections)

                # Check for continuation hint — only if explicit "I still need to"
                if "I still need to" in reply:
                    next_task = reply.split("I still need to", 1)[-1].strip()
                    self._save_pending(next_task, iteration)
                    reply += f"\n\n[task queued: {next_task[:60]}]"

                # TraceRecorder: task end
                if hasattr(self, 'tracer'):
                    self.tracer.on_task_end(True)

                if channel:
                    channel.send(Response(
                        content=reply,
                        target=msg.sender,
                        source=msg.source,
                        chat_id=msg.chat_id,
                    ))
                logger.info(f"  reply: {reply[:80]}")

                # Module 1: Save conversation — full turn (user + tool chain + assistant)
                session_id = self.sessions.get_session_id(msg.source, str(msg.chat_id))
                new_msgs = []
                for m in conv[_new_start:]:
                    entry = {"role": m["role"], "content": m.get("content", "")}
                    if m.get("tool_calls"):
                        entry["tool_calls"] = m["tool_calls"]
                    if m.get("tool_call_id"):
                        entry["tool_call_id"] = m["tool_call_id"]
                    new_msgs.append(entry)
                self.sessions.save_messages(session_id, new_msgs)
                # End verification recording — save training data if violations occurred
                self.verif_recorder.end_turn(reply)
                return

        # Exceeded max iterations — send last reply to sender, save partial conversation
        logger.warning(f"[worker] {msg.sender}: exceeded max tool iterations")
        # End verification recording — save training data even on timeout
        self.verif_recorder.end_turn("[timeout]")
        try:
            session_id = self.sessions.get_session_id(msg.source, str(msg.chat_id))
            new_msgs = []
            for m in conv[_new_start:]:
                entry = {"role": m["role"], "content": m.get("content", "")}
                if m.get("tool_calls"):
                    entry["tool_calls"] = m["tool_calls"]
                if m.get("tool_call_id"):
                    entry["tool_call_id"] = m["tool_call_id"]
                new_msgs.append(entry)
            self.sessions.save_messages(session_id, new_msgs)
        except Exception as e:
            logger.debug(f"[worker] session save error: {e}")
        # Send last assistant reply back to sender (not system messages)
        last_reply = None
        if conv and len(conv) > 2:
            for m in reversed(conv):
                if m.get("role") == "assistant" and m.get("content", "").strip():
                    last_reply = m["content"]
                    break
        if channel and msg.sender not in ("system", None):
            timeout_msg = "[worker timeout after reaching max tool iterations]"
            if last_reply:
                timeout_msg += f"\n\n{last_reply[:1500]}"
            else:
                timeout_msg += "\nNo assistant reply was produced."
            channel.send(Response(
                content=timeout_msg,
                target=msg.sender,
                source=msg.source,
                chat_id=msg.chat_id,
            ))
        # Save continuation hint if explicit
        if conv and len(conv) > 2:
            last_assistant = next(
                (m["content"] for m in reversed(conv) if m.get("role") == "assistant"),
                None
            )
            if last_assistant and "I still need to" in last_assistant:
                self._save_pending(last_assistant, max_iterations)

def main():
    logger.info("tical-code worker starting")

    # PID lock — prevent duplicate instances
    PID_FILE = Path("/tmp/unified-worker.pid")
    try:
        existing = int(PID_FILE.read_text().strip())
        if os.path.exists(f"/proc/{existing}"):
            logger.error(f"Another worker is already running (PID={existing}) — exiting")
            sys.exit(1)
        else:
            logger.warning(f"Stale PID file ({existing}) — overwriting")
    except (FileNotFoundError, ValueError):
        pass
    PID_FILE.write_text(str(os.getpid()))

    try:
        cfg = load_config()
        worker = Worker(cfg)
        worker.run()
    finally:
        PID_FILE.unlink(missing_ok=True)

if __name__ == "__main__":
    main()