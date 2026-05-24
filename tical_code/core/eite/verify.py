"""EITE verify layer - verify claims match reality.

Two hooks in worker loop:
  Hook 1: Identity integrity (every loop iteration)
  Hook 2: Post-tool verification (after every tool call)
"""
import hashlib
import json
import time
from pathlib import Path
from .signature import sign, _get_hardware_id

class VerifyLayer:
    def __init__(self, name: str, workspace: str):
        self.name = name
        self.workspace = workspace
        self._identity_hash = self._compute_identity_hash()
        self._session_tools = []  # Track tools executed this turn

    def _compute_identity_hash(self) -> str:
        hw_id = _get_hardware_id()
        raw = f"{self.name}@{hw_id}:{self.workspace}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    # === Hook 1: Identity Integrity ===

    def check_identity(self, system_prompt: str) -> bool:
        """Verify system prompt hasn't been tampered with."""
        return self.name in system_prompt

    def get_identity_marker(self) -> str:
        """Return injectable identity marker for system prompt."""
        ts = time.strftime("%Y-%m-%d %H:%M")
        sig = sign(self.name, f"identity-anchor:{ts}")
        return (
            f"\n## EITE Identity\n"
            f"- Name: {self.name}\n"
            f"- Hash: {self._identity_hash}\n"
            f"- Signature: {sig[:16]}\n"
            f"- Verified: {ts}"
        )

    # === Hook 2: Post-Tool Verification ===

    def verify_tool_result(self, tool_name: str, args: dict, result: dict) -> dict:
        """After tool executes, verify the claim matches reality."""
        verified = False
        detail = ""

        if tool_name == "file_write":
            path = result.get("path", args.get("path", ""))
            verified = Path(path).exists() if path else False
            detail = f"file_exists={verified}"

        elif tool_name == "bash":
            exit_code = result.get("exit_code", -1)
            # Check if this was a safety block vs execution failure
            error_msg = result.get("error", "")
            if "blocked" in error_msg.lower() or "safety" in error_msg.lower():
                verified = False
                detail = f"safety_blocked: {error_msg[:60]}"
            else:
                verified = exit_code == 0
                detail = f"exit_code={exit_code}"

        elif tool_name == "memory_save":
            key = args.get("key", "")
            mem_file = Path(self.workspace) / "memory.json"
            if mem_file.exists() and key:
                try:
                    data = json.loads(mem_file.read_text())
                    verified = key in data.get("entries", {})
                except Exception:
                    verified = False
            detail = f"key_found={verified}"

        elif tool_name == "chat_send":
            verified = result.get("ok", False)
            detail = f"http_ok={verified}"

        else:
            verified = True
            detail = "no_verification_available"

        result["verified"] = verified
        result["verify_detail"] = detail
        self._session_tools.append({
            "name": tool_name, "args": args,
            "verified": verified, "detail": detail
        })
        return result

    def reset_session(self):
        """Call at start of each message processing turn."""
        self._session_tools = []
