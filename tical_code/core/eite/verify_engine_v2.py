"""EITE Verification Engine v2 — Single source of truth for all verification.

Replaces:
- eite/verify_engine.py (tool safety + reply scanning)
- modules/truthful_reporter.py (declaration-evidence matching)

Architecture:
  Phase 1: verify_tool_call() — before tool execution
  Phase 2: verify_tool_output() — after tool execution
  Phase 3: verify_reply() — before sending to user

Each phase returns VerificationResult with:
  - passed: bool
  - violations: list[Violation]
  - action: "allow" | "block" | "retry" | "rewrite"
"""

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("tical-code.verification")


# ===========================================================================
# Data structures
# ===========================================================================

@dataclass
class Violation:
    rule: int
    category: str  # "security", "evidence", "scope", "certainty", "attribution", "injection", "self_knowledge"
    claim: str
    detail: str
    severity: str = "medium"  # "low", "medium", "high", "critical"

@dataclass
class VerificationResult:
    passed: bool
    violations: list[Violation] = field(default_factory=list)
    action: str = "allow"  # "allow", "block", "retry", "rewrite"
    corrections: list[str] = field(default_factory=list)


# ===========================================================================
# Regex patterns
# ===========================================================================

# Declaration patterns (verb → expected tools)
_DECL_VERB_MAP: dict[str, list[str]] = {
    "saved":     ["file_write", "state_save", "memory_save"],
    "created":   ["file_write", "bash"],
    "deleted":   ["bash", "file_write"],
    "installed": ["bash"],
    "deployed":  ["bash"],
    "fixed":     ["bash", "file_write"],
    "checked":   ["file_read", "web_fetch", "bash"],
    "verified":  ["file_read", "bash", "web_fetch"],
    "confirmed": ["bash", "file_read"],
    "sent to":   ["chat_send"],
    # Chinese
    "已保存": ["file_write", "state_save", "memory_save"],
    "已创建": ["file_write", "bash"],
    "已删除": ["bash", "file_write"],
    "已安装": ["bash"],
    "已部署": ["bash"],
    "已修复": ["bash", "file_write"],
    "已检查": ["file_read", "web_fetch", "bash"],
    "已确认": ["bash", "file_read"],
    "已发送": ["chat_send"],
}

_DECL_RE = re.compile(
    r"\b(saved|created|deleted|installed|deployed|fixed|checked|verified|confirmed|sent to|"
    r"已保存|已创建|已删除|已安装|已部署|已修复|已检查|已确认|已发送)\b",
    re.I,
)

# Scope, certainty, attribution
_SCOPE_RE = re.compile(r"\b(production|deployed|all systems|completely fixed)\b", re.I)
_CERTAINTY_RE = re.compile(r"\b(definitely|for sure|100%)\b", re.I)
_ATTRIBUTION_RE = re.compile(
    r"\b(search|found|fetched|looked up|according to|from the web|from search|retrieved)\b",
    re.I,
)

# Completion and evidence
_COMPLETION_RE = re.compile(
    r"\b(done|completed?|finished|resolved|accomplished|已[做完修好改]|完成|修复|修正)\b",
    re.I,
)
_DIFF_RAW_RE = re.compile(
    r"(?m)^(?:diff --git |index |--- [ab]/|\+\+\+ [ab]/|@@ -\d+,\d+ \+\d+,?\d* @@|^\+[^+]|^-[^-])",
)
_TEST_RAW_RE = re.compile(
    r"(?m)^(?:Ran \d+ test|OK$|FAILED|FAIL|ERROR|\.+E\.+F\.+|PASSED|FAILED|test_\w+|passed|failed|skipped|warnings)",
)
_COMMIT_HASH_RE = re.compile(r"commit [0-9a-f]{7,40}\b")
_VERIFICATION_TOOLS_RE = re.compile(
    r"\b(git diff|git log|pytest|python -m pytest|unittest|run_all)\b", re.I,
)
_EVIDENCE_CLAIM_RE = re.compile(
    r"(?:git diff (?:shows|indicates?|confirmed|verified|output|result)|"
    r"test(?:s)? (?:pass|fail|ran|run|ok|all green|all passed))\b",
    re.I,
)
_SUMMARY_ONLY_RE = re.compile(r"(?:git diff (?:shows|indicates?|confirmed|verified|output|result))", re.I)
_GIT_DIFF_RE = re.compile(r"\bgit diff\b", re.I)
_GIT_LOG_RE = re.compile(r"\bgit log\b", re.I)
_TEST_CMD_RE = re.compile(
    r"\b(pytest|python -m pytest|unittest|run_all|eite-test|nose|tox|pdm run test)\b", re.I,
)

# Self-knowledge patterns
_SELF_CLAIM_RE = re.compile(
    r"\b(my model|i(?:'m| am) (?:using|running)|i use|模型是|我是.*模型|"
    r"deepseek|mimo|qwen|gpt|claude|openai|anthropic|"
    r"mimo-v\d|deepseek-v\d|qwen-\d|gpt-\d|claude-\d)\b",
    re.I,
)

# Injection detection
_INJECTION_PATTERNS = [
    "ignore instructions", "ignore all previous", "bypass safety",
    "override safety", "you are now", "pretend to be another",
    "act as a different", "disregard your programming",
    "你不再是", "忽略之前的指令", "忽略所有指令",
]

# Dangerous bash patterns
_DANGEROUS_BASH = ["rm -rf /", "mkfs", "dd if=", "> /dev/sd", "chmod 777 /"]


# ===========================================================================
# VerificationEngine
# ===========================================================================

class VerificationEngine:
    """Single verification engine — replaces EiteVerifyEngine + TruthfulReporter."""

    _TRUST_FILE = ".trust_state.json"

    def __init__(self, identity_id: str, workspace: str = "."):
        self._identity_id = identity_id
        self._workspace = str(Path(workspace).resolve())
        self._session_tools: list[dict] = []
        self._actions: list[dict] = []
        self._trust_state: dict = self._load_trust()

        # Safe paths (outside workspace)
        self.SAFE_WRITE_PATHS = ["/tmp", "/var/tmp"]
        self.SAFE_READ_PATHS = ["/tmp", "/var/tmp", "/proc", "/sys", "/etc"]

    # ------------------------------------------------------------------
    # Trust state
    # ------------------------------------------------------------------

    def _load_trust(self) -> dict:
        try:
            trust_path = Path(self._workspace) / self._TRUST_FILE
            if trust_path.exists():
                return json.loads(trust_path.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {"violation_timestamps": []}

    def _save_trust(self) -> None:
        try:
            trust_path = Path(self._workspace) / self._TRUST_FILE
            trust_path.write_text(
                json.dumps(self._trust_state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _record_violations(self, count: int) -> None:
        now = time.time()
        ts = self._trust_state.setdefault("violation_timestamps", [])
        for _ in range(count):
            ts.append(now)
        cutoff = now - 86400  # 24h window
        self._trust_state["violation_timestamps"] = [t for t in ts if t > cutoff]
        self._save_trust()

    def get_trust_level(self) -> str:
        now = time.time()
        cutoff = now - 86400
        recent = [t for t in self._trust_state.get("violation_timestamps", []) if t > cutoff]
        if len(recent) >= 3:
            return "untrusted"
        if len(recent) >= 1:
            return "reduced"
        return "full"

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def reset_session(self) -> None:
        self._session_tools = []
        self._actions.clear()

    def get_identity_marker(self) -> str:
        return f"\n[EITE: {self._identity_id}]\n"

    # ------------------------------------------------------------------
    # Phase 1: Tool Call Verification (before execution)
    # ------------------------------------------------------------------

    def verify_tool_call(self, name: str, args: dict) -> VerificationResult:
        """Verify a tool call before execution. Returns pass/block."""
        violations = []

        # 1) Blocked tools (currently empty — designed for policy)
        # Future: load from config

        # 2) Bash safety
        if name == "bash":
            cmd = str(args.get("command", ""))
            for pattern in _DANGEROUS_BASH:
                if pattern in cmd:
                    violations.append(Violation(
                        rule=0, category="security",
                        claim=f"dangerous_bash:{pattern}",
                        detail=f"Command contains dangerous pattern: {pattern}",
                        severity="critical",
                    ))

        # 3) File write path safety
        if name == "file_write":
            path = str(args.get("path", ""))
            if path:
                resolved = self._resolve_path(path, self.SAFE_WRITE_PATHS)
                if resolved is None:
                    violations.append(Violation(
                        rule=0, category="security",
                        claim="path_outside_workspace",
                        detail=f"Path outside workspace: {path}",
                        severity="high",
                    ))
                elif "eite" in resolved.parts:
                    violations.append(Violation(
                        rule=0, category="security",
                        claim="write_eite_directory",
                        detail=f"Cannot write to EITE directory: {path}",
                        severity="high",
                    ))

        # 4) File read path safety
        if name == "file_read":
            path = str(args.get("path", ""))
            if path:
                resolved = self._resolve_path(path, self.SAFE_READ_PATHS)
                if resolved is None:
                    violations.append(Violation(
                        rule=0, category="security",
                        claim="path_outside_workspace",
                        detail=f"Path outside workspace: {path}",
                        severity="high",
                    ))

        # Record for Phase 3
        self._session_tools.append({
            "tool": name, "args": args, "verified": True, "detail": "ok",
        })

        passed = not violations
        return VerificationResult(
            passed=passed,
            violations=violations,
            action="block" if not passed else "allow",
        )

    # ------------------------------------------------------------------
    # Phase 2: Tool Output Verification (after execution)
    # ------------------------------------------------------------------

    def verify_tool_output(self, name: str, args: dict, result: dict) -> VerificationResult:
        """Verify tool output after execution. Returns pass/block."""
        violations = []

        # 1) Execution error check
        if isinstance(result, dict) and "error" in result:
            err = str(result["error"])[:200]
            violations.append(Violation(
                rule=0, category="security",
                claim="tool_execution_error",
                detail=f"Tool returned error: {err}",
                severity="medium",
            ))

        # 2) File write: verify file exists
        if name == "file_write":
            path = str(args.get("path", ""))
            if path:
                resolved = self._resolve_path(path, self.SAFE_WRITE_PATHS)
                if resolved and not resolved.exists():
                    violations.append(Violation(
                        rule=0, category="evidence",
                        claim="file_not_written",
                        detail=f"File does not exist after write: {path}",
                        severity="high",
                    ))

        # 3) Bash: check exit code
        if name == "bash" and isinstance(result, dict):
            exit_code = result.get("exit_code", -1)
            if exit_code != 0 and exit_code is not None:
                stdout = str(result.get("stdout", ""))[:200]
                violations.append(Violation(
                    rule=0, category="evidence",
                    claim="bash_exit_nonzero",
                    detail=f"Bash exited with code {exit_code}: {stdout}",
                    severity="medium",
                ))

        # Record action for Phase 3
        verified = (result.get("ok", False) or result.get("exit_code") == 0) and not violations
        self._actions.append({
            "tool_name": name,
            "args": args,
            "result": result,
            "verified": verified,
        })

        # Update session_tools
        if self._session_tools:
            self._session_tools[-1]["verified"] = verified
            self._session_tools[-1]["detail"] = violations[0].detail if violations else "ok"

        passed = not any(v.severity == "high" for v in violations)
        return VerificationResult(
            passed=passed,
            violations=violations,
            action="block" if not passed else "allow",
        )

    # ------------------------------------------------------------------
    # Phase 3: Reply Verification (before sending)
    # ------------------------------------------------------------------

    def verify_reply(self, reply: str) -> VerificationResult:
        """Verify the final reply before sending. Returns allow/retry/rewrite."""
        violations = []
        corrections = []

        # Rule 1-2: Declaration-evidence matching
        for match in _DECL_RE.finditer(reply.lower()):
            verb = match.group(1)
            expected_tools = _DECL_VERB_MAP.get(verb, [])
            if expected_tools:
                executed = {a["tool_name"] for a in self._actions}
                succeeded = {a["tool_name"] for a in self._actions if a["verified"]}
                if not any(t in executed for t in expected_tools):
                    violations.append(Violation(
                        rule=1, category="evidence",
                        claim=verb,
                        detail=f"No matching tool was executed for '{verb}'",
                        severity="high",
                    ))
                elif not any(t in succeeded for t in expected_tools):
                    violations.append(Violation(
                        rule=2, category="evidence",
                        claim=verb,
                        detail=f"The action '{verb}' did not complete successfully",
                        severity="medium",
                    ))

        # Rule 3: Scope — local tool but production claim
        if _SCOPE_RE.search(reply):
            if self._actions and all(a.get("is_local_only", False) for a in self._actions):
                violations.append(Violation(
                    rule=3, category="scope",
                    claim="scope_expansion",
                    detail="Local action claimed as production/system-wide",
                    severity="medium",
                ))

        # Rule 4: Certainty with warnings
        if _CERTAINTY_RE.search(reply):
            if any("warning" in str(a.get("result", "")).lower() for a in self._actions):
                violations.append(Violation(
                    rule=4, category="certainty",
                    claim="certainty_overstatement",
                    detail="Absolute certainty claimed with uncertain results",
                    severity="medium",
                ))

        # Rule 5: Attribution — fetch results presented as own knowledge
        fetch_actions = [a for a in self._actions if a["tool_name"] == "web_fetch"]
        if fetch_actions and not _ATTRIBUTION_RE.search(reply):
            violations.append(Violation(
                rule=5, category="attribution",
                claim="attribution_missing",
                detail="Information from search/fetch presented as own knowledge",
                severity="medium",
            ))

        # Rule 6: Raw evidence for git/test operations
        has_raw_diff = bool(_DIFF_RAW_RE.search(reply))
        has_raw_test = bool(_TEST_RAW_RE.search(reply))
        has_commit_hash = bool(_COMMIT_HASH_RE.search(reply))
        is_summary_only = bool(_SUMMARY_ONLY_RE.search(reply))

        for action in self._actions:
            if action["tool_name"] != "bash":
                continue
            cmd = str(action.get("args", {}).get("command", ""))
            if _GIT_DIFF_RE.search(cmd) and is_summary_only and not has_raw_diff:
                violations.append(Violation(
                    rule=6, category="evidence",
                    claim="git_diff_summary_only",
                    detail="git diff output summarized instead of showing raw output",
                    severity="high",
                ))
                break
            if _TEST_CMD_RE.search(cmd) and not has_raw_test:
                violations.append(Violation(
                    rule=6, category="evidence",
                    claim="test_output_missing",
                    detail="Tests run but raw output not included",
                    severity="high",
                ))
                break
            if _GIT_LOG_RE.search(cmd) and not has_commit_hash:
                violations.append(Violation(
                    rule=6, category="evidence",
                    claim="git_log_no_hash",
                    detail="git log run but no commit hash in reply",
                    severity="high",
                ))
                break

        # Rule 7: Completion claims must have verification evidence
        if _COMPLETION_RE.search(reply):
            ran_verification = False
            for action in self._actions:
                if action["tool_name"] == "bash":
                    cmd = str(action.get("args", {}).get("command", ""))
                    if _VERIFICATION_TOOLS_RE.search(cmd):
                        ran_verification = True
                        break
            if not ran_verification:
                violations.append(Violation(
                    rule=7, category="evidence",
                    claim="completion_without_verification",
                    detail="Task completion claimed but no verification tools were run",
                    severity="high",
                ))

        # Rule 8: Self-knowledge must use check_self
        if _SELF_CLAIM_RE.search(reply):
            used_check_self = any(a["tool_name"] == "check_self" for a in self._actions)
            if not used_check_self:
                violations.append(Violation(
                    rule=8, category="self_knowledge",
                    claim="self_knowledge_without_verification",
                    detail="Claim about model/config without using check_self tool",
                    severity="high",
                ))

        # Injection detection
        reply_lower = reply.lower()
        for pattern in _INJECTION_PATTERNS:
            if pattern.lower() in reply_lower:
                violations.append(Violation(
                    rule=0, category="injection",
                    claim=f"injection:{pattern}",
                    detail=f"Reply contains suspicious phrase: {pattern}",
                    severity="low",
                ))

        # Record violations
        if violations:
            self._record_violations(len(violations))

        # Determine action based on severity
        has_critical = any(v.severity == "critical" for v in violations)
        has_high = any(v.severity == "high" for v in violations)

        if has_critical:
            action = "block"
        elif has_high:
            action = "retry"
        elif violations:
            action = "rewrite"
        else:
            action = "allow"

        return VerificationResult(
            passed=len(violations) == 0,
            violations=violations,
            action=action,
            corrections=[v.detail for v in violations],
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_path(self, path: str, allowed_outside: list[str] | None = None) -> Path | None:
        try:
            p = Path(path).expanduser().resolve()
            workspace = Path(self._workspace).resolve()
            try:
                p.relative_to(workspace)
                return p
            except ValueError:
                pass
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
