"""Module 5: Proactive Proposal Gate - confirm before write operations."""

import hashlib
import json
import logging
import re
import time
from typing import Optional

logger = logging.getLogger("tical-code.proposal_gate")

# ---------------------------------------------------------------------------
# Bash classification helpers
# ---------------------------------------------------------------------------

_READ_TOOLS = {"file_read", "memory_load", "conv_search", "web_fetch", "chat_send"}
_WRITE_TOOLS = {"file_write", "state_save"}

_BASH_READ_PREFIXES = re.compile(
    r"^\s*(cat|head|tail|ls|find|grep|which|echo|pwd|env|ps|df|du|stat|file|wc|diff"
    r"|git\s+(status|log|diff|branch)|uname|hostname|uptime|free|top|ip|ping|curl|wget)\b",
    re.I,
)

_BASH_WRITE_INDICATORS = re.compile(
    r"(>>?|pip\s+install|apt\b|yum\b|dnf\b|snap\b"
    r"|\bcp\b|\bmv\b|\brm\b|\bmkdir\b|\brmdir\b"
    r"|chmod|chown|systemctl\s+(start|stop|restart)"
    r"|git\s+(push|commit))",
)

# Safe pipe targets: piping to these doesn't make a read command a write
_SAFE_PIPE_RE = re.compile(r"\|\s*(grep|head|tail|wc)\b")

# ---------------------------------------------------------------------------
# Multilingual confirmation/rejection — compiled regexes
# ---------------------------------------------------------------------------

_CONFIRM_RES = [
    re.compile(r"\b(yes|ok|sure|go ahead|do it|proceed|confirm|go|done)\b", re.I),
    re.compile(r"(|||||||||)"),
    re.compile(r"\b(ja|mach|los)\b", re.I),
    re.compile(r"\b(oui|fait|allez)\b", re.I),
    re.compile(r"\b(sí|haz|hacer)\b", re.I),
]

_REJECT_RES = [
    re.compile(r"\b(no|don't|cancel|stop|wait|hold on|not)\b", re.I),
    re.compile(r"(||||||)"),
    re.compile(r"\b(nein|warte|nicht)\b", re.I),
    re.compile(r"\b(non|attendez|pas)\b", re.I),
    re.compile(r"\b(espera)\b", re.I),
]

class ProposalGate:
    """Gate write operations behind user confirmation."""

    def __init__(self, timeout_seconds: int = 300):
        self.timeout = timeout_seconds
        self._pending: Optional[dict] = None

    # ------------------------------------------------------------------
    # Bash read/write classification
    # ------------------------------------------------------------------

    @staticmethod
    def _bash_is_write(command: str) -> bool:
        # Strip safe pipes before checking write indicators
        stripped = _SAFE_PIPE_RE.sub("", command)
        if _BASH_WRITE_INDICATORS.search(stripped):
            return True
        # Unknown command prefix → conservative: treat as write
        if not _BASH_READ_PREFIXES.match(command):
            return True
        return False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def should_confirm(self, tool_name: str, args: dict, source: str = "") -> bool:
        try:
            # Seoul/controller messages bypass gate automatically
            if source == "tical-chat":
                return False
            # Telegram messages bypass gate — user expects direct replies, not proposals
            if source == "telegram":
                return False
            if tool_name in _READ_TOOLS:
                return False
            if tool_name in _WRITE_TOOLS:
                return True
            if tool_name == "bash":
                command = args.get("command", args.get("cmd", ""))
                return self._bash_is_write(command)
            # Unknown tool → conservative: confirm
            return True
        except Exception:
            logger.exception("should_confirm failed")
            return True

    def create_proposal(self, tool_name: str, args: dict) -> dict:
        proposal_id = hashlib.sha256(
            f"{tool_name}:{json.dumps(args, sort_keys=True)}:{time.time()}".encode()
        ).hexdigest()[:16]

        # Natural-language description
        if tool_name == "bash":
            cmd = args.get("command", args.get("cmd", ""))
            desc = f"run shell command: `{cmd}`"
        elif tool_name == "file_write":
            path = args.get("path", args.get("file", ""))
            desc = f"write to file: `{path}`"
        elif tool_name == "state_save":
            key = args.get("key", "")
            desc = f"save state key: `{key}`"
        else:
            desc = f"execute `{tool_name}` with args: {json.dumps(args)}"

        message = (
            f"I'd like to {desc}.\n"
            f"Expected result: persistent state will be modified.\n"
            f"Please confirm (yes/ok/proceed) or cancel (no/cancel).\n"
            f"[proposal: {proposal_id}]"
        )
        self._pending = {
            "proposal_id": proposal_id,
            "tool_name": tool_name,
            "args": args,
            "created_at": time.time(),
        }
        logger.info("Proposal created id=%s tool=%s", proposal_id, tool_name)
        return {"proposal_id": proposal_id, "message": message, "pending_action": self._pending}

    def check_user_response(self, user_message: str) -> str:
        if self._pending is None:
            return "unclear"
        if time.time() - self._pending["created_at"] > self.timeout:
            logger.info("Proposal timed out")
            self._pending = None
            return "unclear"

        has_confirm = any(p.search(user_message) for p in _CONFIRM_RES)
        has_reject = any(p.search(user_message) for p in _REJECT_RES)

        if has_confirm and not has_reject:
            return "confirmed"
        if has_reject:
            return "rejected"
        return "unclear"

    def get_pending_action(self) -> Optional[dict]:
        if self._pending is None:
            return None
        if time.time() - self._pending["created_at"] > self.timeout:
            self._pending = None
            return None
        return self._pending

    def clear_pending(self) -> None:
        self._pending = None

    def reset(self) -> None:
        self.clear_pending()
