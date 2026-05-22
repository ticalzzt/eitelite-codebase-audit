"""EITE verify layer - verify claims match reality.

Three hooks in worker loop:
  Hook 1: Identity integrity (every loop iteration)
  Hook 2: Post-tool verification (after every tool call)
  Hook 3: Pre-reply claim scan (before sending to user)
"""
import hashlib
import json
import os
import re
import time
from pathlib import Path
from .signature import sign, verify as sig_verify, _get_hardware_id

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

    # === Hook 3: Pre-Reply Claim Scan ===

    def scan_reply(self, reply_text: str) -> list:
        """Scan reply for unverified claims before sending to user."""
        warnings = []
        reply_lower = reply_text.lower()
        executed_names = {t["name"] for t in self._session_tools}
        executed_verified = {t["name"] for t in self._session_tools if t["verified"]}

        # Claim patterns to required tools
        claim_map = {
            # English patterns
            r"\bsaved\b": {"file_write", "memory_save", "state_save"},
            r"\bsaved\b": {"file_write", "memory_save", "state_save"},
            r"\bsent to\b": {"chat_send"},
            r"\bcreated\b": {"file_write", "bash"},
            r"\bdeleted\b": {"bash"},
            r"\binstalled\b": {"bash"},
            r"\bwritten to\b": {"file_write"},
            r"\bfixed\b": {"bash", "file_write"},
            r"\bdeployed\b": {"bash"},
            # Chinese patterns
            "": {"file_write", "memory_save", "state_save"},
            "": {"chat_send"},
            "": {"file_write", "bash"},
            "": {"bash"},
            "": {"bash"},
            "": {"bash"},
            "": {"bash", "file_write"},
            "": {"file_write", "memory_save"},
            "": {"file_write", "bash"},
            "": {"bash"},
            "": {"file_write"},
            "": {"file_write"},
            "": {"chat_send"},
            "": {"chat_send"},
        }

        for pattern, required_tools in claim_map.items():
            if re.search(pattern, reply_lower):
                if not (executed_names & required_tools):
                    warnings.append(
                        f"Claim '{pattern}' in reply but no {required_tools} tool executed"
                    )
                elif not (executed_verified & required_tools):
                    warnings.append(
                        f"Claim '{pattern}' in reply but tool verification failed"
                    )

        return warnings

    def reset_session(self):
        """Call at start of each message processing turn."""
        self._session_tools = []
