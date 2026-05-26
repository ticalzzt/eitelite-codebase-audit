"""
EITE验证引擎 v0.4 — 真实的工具验证和回复扫描引擎。
替换之前 _EiteVerifyWrapper 的空壳实现。
"""
import json
import os
import re
from pathlib import Path


class EiteVerifyEngine:
    """EITE 验证引擎：对工具调用和回复进行安全验证。

    提供给 unified_worker.py 的 self.eite 使用，接口：
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
        """验证工具调用结果。返回 {"verified": bool, "verify_detail": str}。"""
        entry = {
            "tool": name,
            "args": self._sanitize(args),
            "verified": True,
            "detail": "ok",
        }
        self._session_tools.append(entry)

        # 1) 拦截禁止工具
        if name in self.BLOCKED_TOOLS:
            return self._reject(entry, f"Tool '{name}' is blocked by EITE policy")

        # 2) bash 安全检查
        if name == "bash":
            result = self._verify_bash(args, entry)
            if not result["verified"]:
                return result

        # 3) file_write 安全检查
        if name == "file_write":
            result = self._verify_file_write(args, entry)
            if not result["verified"]:
                return result

        # 4) file_read 安全检查
        if name == "file_read":
            result = self._verify_file_read(args, entry)
            if not result["verified"]:
                return result

        # 5) 工具执行本身是否返回错误
        if isinstance(result, dict) and "error" in result:
            err = str(result["error"])[:200]
            return self._reject(entry, f"Tool returned error: {err}")

        # 6) EITE check 模块规则匹配
        try:
            from .check import check as _eite_check
            check_result = _eite_check(json.dumps({"tool": name, "args": args}))
            if check_result.get("action") == "block":
                return self._reject(entry,
                    f"Blocked by EITE rule: {check_result.get('reason', '?')}")
        except Exception:
            pass

        return {"verified": True, "verify_detail": "ok"}

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

        # 复用 tool_executor 的安全检查
        try:
            from tical_code.core.tool_executor import _bash_safety_check
            block_reason = _bash_safety_check(cmd)
            if block_reason is not None:
                return self._reject(entry, block_reason)
        except ImportError:
            # tool_executor 不可用，退回到基础正则检查
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

        resolved = self._resolve_path(path)
        if resolved is None:
            return self._reject(entry, f"Path outside workspace: {path}")

        # 禁止写 EITE 自身文件
        if "eite" in resolved.parts:
            return self._reject(entry, f"Cannot write to EITE directory: {path}")

        return {"verified": True, "verify_detail": "ok"}

    def _verify_file_read(self, args: dict, entry: dict) -> dict:
        path = str(args.get("path", ""))
        if not path:
            return {"verified": True, "verify_detail": "no path"}

        resolved = self._resolve_path(path)
        if resolved is None:
            return self._reject(entry, f"Path outside workspace: {path}")

        return {"verified": True, "verify_detail": "ok"}

    # ── Helpers ───────────────────────────────────────────────────

    def _resolve_path(self, path: str) -> Path | None:
        """解析路径并检查是否在工作区内。"""
        try:
            p = Path(path).expanduser().resolve()
            workspace = Path(self._workspace).resolve()
            p.relative_to(workspace)
            return p
        except (ValueError, OSError, RuntimeError):
            return None

    def _reject(self, entry: dict, reason: str) -> dict:
        entry["verified"] = False
        entry["detail"] = reason
        return {"verified": False, "verify_detail": reason}

    @staticmethod
    def _sanitize(args: dict) -> dict:
        """脱敏：仅保留类型和前 100 字符的值。"""
        safe = {}
        for k, v in args.items():
            vs = str(v)
            if len(vs) > 100:
                safe[k] = vs[:100] + "..."
            else:
                safe[k] = vs
        return safe
