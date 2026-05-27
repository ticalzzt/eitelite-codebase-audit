"""
EITE Verify Engine v0.4 — real tool verification and reply scanning engine.
Replaces the previous _EiteVerifyWrapper stub implementation.
"""
import json
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger("tical-code.eite.verify")


class EiteVerifyEngine:
    """EITE Verify Engine: security verification for tool calls and replies.

    Used by unified_worker.py as self.eite, interface:
    - get_identity_marker() -> str
    - verify_tool_result(name, args, result) -> {"verified": bool, "verify_detail": str}
    - reset_session()
    - scan_reply(reply) -> list[str]
    - _session_tools: list[dict]
    """

    # 完全禁止的工具
    BLOCKED_TOOLS: set[str] = set()

    # 回复中可疑关键词 (仅做第一层过滤)
    SUSPICIOUS_PHRASES: list[str] = [
        "ignore instructions",
        "ignore all previous",
        "bypass safety",
        "override safety",
        "you are now",
        "pretend to be another",
        "act as a different",
        "disregard your programming",
        "你不再是",
        "忽略之前的指令",
        "忽略所有指令",
    ]

    # Paths outside workspace that are always allowed
    SAFE_WRITE_PATHS: list[str] = [
        "/tmp",
        "/var/tmp",
    ]
    SAFE_READ_PATHS: list[str] = [
        "/tmp",
        "/var/tmp",
        "/proc",
        "/sys",
        "/etc",
    ]

    def __init__(self, identity_id: str, workspace: str = "."):
        self._identity_id = identity_id
        self._workspace = str(Path(workspace).resolve())
        self._session_tools: list[dict] = []

    # ── Public interface ──────────────────────────────────────────

    def get_identity_marker(self) -> str:
        return f"\n[EITE: {self._identity_id}]\n"

    def reset_session(self) -> None:
        self._session_tools = []

    def verify_tool_result(self, name: str, args: dict, result: dict) -> dict:
        """Verify tool call result. Returns {"verified": bool, "verify_detail": str}."""
        entry = {
            "tool": name,
            "args": self._sanitize(args),
            "verified": True,
            "detail": "ok",
        }
        self._session_tools.append(entry)

        # 1) Block forbidden tools
        if name in self.BLOCKED_TOOLS:
            return self._reject(entry, f"Tool '{name}' is blocked by EITE policy")

        # 2) bash safety check
        if name == "bash":
            result = self._verify_bash(args, entry)
            if not result["verified"]:
                return result

        # 3) file_write safety check
        if name == "file_write":
            result = self._verify_file_write(args, entry)
            if not result["verified"]:
                return result

        # 4) file_read safety check
        if name == "file_read":
            result = self._verify_file_read(args, entry)
            if not result["verified"]:
                return result

        # 5) Check if tool execution returned error
        if isinstance(result, dict) and "error" in result:
            err = str(result["error"])[:200]
            return self._reject(entry, f"Tool returned error: {err}")

        # 6) EITE check module rule matching
        try:
            from .check import check as _eite_check
            check_result = _eite_check(json.dumps({"tool": name, "args": args}))
            if check_result.get("action") == "block":
                return self._reject(entry,
                    f"Blocked by EITE rule: {check_result.get('reason', '?')}")
        except Exception as e:
            logger.debug(f"EITE check module error: {e}")

        return {"verified": True, "verify_detail": "ok"}

    def check_identity(self, prompt: str = "") -> bool:
        """Check if the prompt correctly identifies this worker by name.
        Returns True if identity matches or prompt is empty."""
        if not prompt:
            return True
        return self._identity_id in prompt

    def scan_reply(self, reply: str) -> list[str]:
        """扫描回复中的可疑内容。返回警告列表。"""
        if not reply:
            return []
        warnings: list[str] = []
        reply_lower = reply.lower()
        for phrase in self.SUSPICIOUS_PHRASES:
            if phrase.lower() in reply_lower:
                warnings.append(f"Reply contains suspicious phrase: {phrase[:50]}")
        return warnings

    # ── Internal verification methods ─────────────────────────────

    def _verify_bash(self, args: dict, entry: dict) -> dict:
        cmd = str(args.get("command", ""))
        if not cmd:
            return {"verified": True, "verify_detail": "empty command"}

        # Reuse tool_executor's safety check
        try:
            from tical_code.core.tool_executor import _bash_safety_check
            block_reason = _bash_safety_check(cmd)
            if block_reason is not None:
                return self._reject(entry, block_reason)
        except ImportError:
            # tool_executor unavailable, falling back to basic regex check
            pass

        # 基础检查：黑名单关键词
        dangerous = ["rm -rf /", "mkfs", "dd if=", "> /dev/sd", "chmod 777 /"]
        for d in dangerous:
            if d in cmd:
                return self._reject(entry, f"Command contains dangerous pattern: {d}")

        return {"verified": True, "verify_detail": "ok"}

    def _verify_file_write(self, args: dict, entry: dict) -> dict:
        path = str(args.get("path", ""))
        if not path:
            return {"verified": True, "verify_detail": "no path"}

        resolved = self._resolve_path(path, allowed_outside=self.SAFE_WRITE_PATHS)
        if resolved is None:
            return self._reject(entry, f"Path outside workspace: {path}")

        # Forbid writing to EITE's own files
        if "eite" in resolved.parts:
            return self._reject(entry, f"Cannot write to EITE directory: {path}")

        # Verify the file was actually written (if result claims success)
        if not resolved.exists():
            return self._reject(entry, f"File does not exist after write: {path}")

        return {"verified": True, "verify_detail": "ok"}

    def _verify_file_read(self, args: dict, entry: dict) -> dict:
        path = str(args.get("path", ""))
        if not path:
            return {"verified": True, "verify_detail": "no path"}

        resolved = self._resolve_path(path, allowed_outside=self.SAFE_READ_PATHS)
        if resolved is None:
            return self._reject(entry, f"Path outside workspace: {path}")

        return {"verified": True, "verify_detail": "ok"}

    # ── Helpers ───────────────────────────────────────────────────

    def _resolve_path(self, path: str, allowed_outside: list[str] | None = None) -> Path | None:
        """Resolve path and check if it's in workspace or allowed directories."""
        try:
            p = Path(path).expanduser().resolve()
            workspace = Path(self._workspace).resolve()
            # Allow if inside workspace
            try:
                p.relative_to(workspace)
                return p
            except ValueError:
                pass
            # Allow if inside any whitelisted directory
            if allowed_outside:
                for safe in allowed_outside:
                    try:
                        p.relative_to(Path(safe))
                        return p
                    except ValueError:
                        continue
            return None
        except (ValueError, OSError, RuntimeError):
            return None

    def _reject(self, entry: dict, reason: str) -> dict:
        entry["verified"] = False
        entry["detail"] = reason
        return {"verified": False, "verify_detail": reason}

    @staticmethod
    def _sanitize(args: dict) -> dict:
        """Sanitize: keep only type and first 100 chars of value."""
        safe = {}
        for k, v in args.items():
            vs = str(v)
            if len(vs) > 100:
                safe[k] = vs[:100] + "..."
            else:
                safe[k] = vs
        return safe
