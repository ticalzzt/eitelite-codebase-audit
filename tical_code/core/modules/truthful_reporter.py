"""Module 4: Truthful Reporting - detect unsubstantiated claims in AI replies."""

import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger("tical-code.truthful_reporter")

# ---------------------------------------------------------------------------
# Extensible claim patterns — verb → candidate tool names
# ---------------------------------------------------------------------------

_VERB_TOOL_MAP: dict[str, list[str]] = {
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
    # ZH verbs map to same buckets — handled separately below
}

_ZH_VERB_MAP: dict[str, list[str]] = {
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

# Compiled regexes for detection
_EN_CLAIM_RE = re.compile(
    r"\b(saved|created|deleted|installed|deployed|fixed|checked|verified|confirmed|sent to)\b",
    re.I,
)
_ZH_CLAIM_RE = re.compile(
    r"(已保存|已创建|已删除|已安装|已部署|已修复|已检查|已确认|已发送)",
)
_SCOPE_WORDS = re.compile(r"\b(production|deployed|all systems|completely fixed)\b", re.I)
_CERTAINTY_WORDS = re.compile(r"\b(definitely|for sure|100%)\b", re.I)
# Attribution: words that indicate the user acknowledged a search/fetch source
_ATTRIBUTION_WORDS = re.compile(
    r"\b(search|found|fetched|looked up|according to|from the web|from search|retrieved)\b",
    re.I,
)

# ---------------------------------------------------------------------------
# Rule 6: Evidence patterns — raw terminal output markers
# ---------------------------------------------------------------------------

# Evidence of raw git diff output (real diff markers, not just summary words)
_DIFF_RAW_RE = re.compile(
    r"(?m)^(?:diff --git |index |--- [ab]/|\+\+\+ [ab]/|@@ -\d+,\d+ \+\d+,\d+ @@|^\+[^+]|^-[^-])",
)
# Evidence of raw test output (real test runner output, not just "tests pass")
_TEST_RAW_RE = re.compile(
    r"(?m)^(?:Ran \d+ test|OK$|FAILED|FAIL|ERROR|\.+E\.+F\.+|PASSED|FAILED|test_\w+|passed|failed|skipped|warnings)",
)
# Evidence of a commit hash in context (7+ hex chars after "commit")
_COMMIT_HASH_RE = re.compile(
    r"commit [0-9a-f]{7,40}\b",
)
# Summary-only patterns: words that look like summaries without raw evidence
_SUMMARY_ONLY_RE = re.compile(
    r"(?i)(?:git diff (?:shows|indicates?|confirmed|verified|output|result))",
    re.I,
)
# Git command patterns: bash commands that should produce raw output
_GIT_DIFF_CMD_RE = re.compile(r"\bgit diff\b", re.I)
_GIT_LOG_CMD_RE = re.compile(r"\bgit log\b", re.I)
_TEST_CMD_RE = re.compile(
    r"\b(pytest|python -m pytest|unittest|run_all|eite-test|nose|tox|pdm run test)\b",
    re.I,
)

# ---------------------------------------------------------------------------
# Rule 7: Completion-without-evidence patterns
# ---------------------------------------------------------------------------

# "Done"/"complete"/"finished" claims — broader than verb-map claims
_COMPLETION_CLAIM_EN_RE = re.compile(
    r"\b(done|completed?|finished|resolved|accomplished|已[做完修好改]|完成|修复|修正)\b",
    re.I,
)
# Evidence verification tools: the three required evidence types
_VERIFICATION_TOOLS = re.compile(
    r"\b(git diff|git log|pytest|python -m pytest|unittest|run_all)\b",
    re.I,
)
# Evidence keywords in reply — claims about evidence without showing it
_EVIDENCE_CLAIM_WORDS = re.compile(
    r"\b(git diff (?:shows|indicates?|confirmed|verified|output|result)|"
    r"test(?:s)? (?:pass|fail|ran|run|ok|all green|all passed))\b",
    re.I,
)

# Rule 8: Self-knowledge claims must use check_self tool
_SELF_CLAIM_RE = re.compile(
    r"\b(my model|i(?:'m| am) (?:using|running)|i use|模型是|我是.*模型|"
    r"deepseek|mimo|qwen|gpt|claude|openai|anthropic|"
    r"mimo-v\d|deepseek-v\d|qwen-\d|gpt-\d|claude-\d)\b",
    re.I,
)

# Tools that provide self-knowledge
_SELF_VERIFY_TOOLS = {"check_self"}

# Trust window: only count violations from the last 24 hours
_TRUST_WINDOW_SECONDS = 86400


class TruthfulReporter:
    """6-rule truthful reporter with sliding-window trust tracking."""

    _TRUST_FILE = ".trust_state.json"

    def __init__(self, workspace: str):
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self._trust_path = self.workspace / self._TRUST_FILE
        self._actions: list[dict[str, Any]] = []
        self._trust_state = self._load_trust()

    # ------------------------------------------------------------------
    # Trust state persistence (sliding window)
    # ------------------------------------------------------------------

    def _load_trust(self) -> dict:
        try:
            if self._trust_path.exists():
                return json.loads(self._trust_path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("load trust state failed")
        return {"violation_timestamps": []}

    def _save_trust(self) -> None:
        try:
            self._trust_path.write_text(
                json.dumps(self._trust_state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            logger.exception("save trust state failed")

    def _record_violations(self, count: int) -> None:
        now = time.time() if "time" in dir() else __import__("time").time()
        ts = self._trust_state.setdefault("violation_timestamps", [])
        for _ in range(count):
            ts.append(now)
        # Prune old entries outside window
        cutoff = now - _TRUST_WINDOW_SECONDS
        self._trust_state["violation_timestamps"] = [t for t in ts if t > cutoff]
        self._save_trust()

    def get_trust_level(self) -> str:
        ts = self._trust_state.get("violation_timestamps", [])
        now = __import__("time").time()
        cutoff = now - _TRUST_WINDOW_SECONDS
        recent = [t for t in ts if t > cutoff]
        if len(recent) >= 3:
            return "untrusted"
        if len(recent) >= 1:
            return "reduced"
        return "full"

    # ------------------------------------------------------------------
    # Action recording
    # ------------------------------------------------------------------

    def record_action(self, tool_name: str, args: dict, result: dict, verified: bool) -> None:
        try:
            raw = json.dumps(result, sort_keys=True, ensure_ascii=False)
            self._actions.append({
                "tool_name": tool_name,
                "args": args,
                "result": result,
                "verified": verified,
                "evidence_hash": hashlib.sha256(raw.encode()).hexdigest(),
                "has_warning": "warning" in raw.lower() or "truncated" in raw.lower(),
                "is_local_only": tool_name in ("file_write", "file_read", "state_save", "memory_save"),
                "is_fetch": tool_name in ("web_fetch",),
            })
        except Exception:
            logger.exception("record_action failed")

    # ------------------------------------------------------------------
    # 6-rule scan (5 original + Rule 6: Evidence Rule)
    # ------------------------------------------------------------------

    def _check_evidence_rule(self, reply_text: str) -> list[dict]:
        """Rule 6: if tools included git-diff/git-log/test commands,
        verify the reply contains raw terminal output, not just a summary."""
        violations: list[dict] = []

        is_evidence_summary = bool(_SUMMARY_ONLY_RE.search(reply_text))
        has_raw_diff = bool(_DIFF_RAW_RE.search(reply_text))
        has_raw_test = bool(_TEST_RAW_RE.search(reply_text))
        has_commit_hash = bool(_COMMIT_HASH_RE.search(reply_text))

        for action in self._actions:
            if action["tool_name"] != "bash":
                continue
            cmd = str(action.get("args", {}).get("command", ""))

            # --- git diff check ---
            if _GIT_DIFF_CMD_RE.search(cmd):
                if is_evidence_summary and not has_raw_diff:
                    violations.append({
                        "rule": 6,
                        "claim": "git_diff_summary_only",
                        "correction": (
                            "You cited git diff output but only summarized it. "
                            "You MUST include the raw terminal output of `git diff` "
                            "— lines starting with diff --git, --- a/, +++ b/, @@, +, -."
                        ),
                    })
                    break  # one violation per action type

                if not has_raw_diff and not is_evidence_summary:
                    # Even worse: claimed git diff but included NO evidence at all
                    violations.append({
                        "rule": 6,
                        "claim": "git_diff_no_evidence",
                        "correction": (
                            "You claimed a git diff operation but did not include any "
                            "raw diff output. Attach the actual `git diff` terminal output."
                        ),
                    })
                    break

            # --- test runner check ---
            if _TEST_CMD_RE.search(cmd):
                if not has_raw_test:
                    violations.append({
                        "rule": 6,
                        "claim": "test_output_missing",
                        "correction": (
                            "You ran tests but did not include raw test output. "
                            "Include the terminal stdout of the test run "
                            "(test count, PASS/FAIL, any errors)."
                        ),
                    })
                    break

            # --- git log check ---
            if _GIT_LOG_CMD_RE.search(cmd):
                if not has_commit_hash:
                    violations.append({
                        "rule": 6,
                        "claim": "git_log_no_hash",
                        "correction": (
                            "You ran `git log` but did not include a commit hash "
                            "in your reply. Include the actual hash in format "
                            "'commit xxxxxxx'."
                        ),
                    })
                    break

        return violations

    def _check_completion_evidence(self, reply_text: str) -> list[dict]:
        """Rule 7: if reply claims completion without verification evidence."""
        violations: list[dict] = []

        # Only trigger when completion is claimed
        if not _COMPLETION_CLAIM_EN_RE.search(reply_text):
            return violations

        # Check if any verification tools were actually run
        ran_verification = False
        for action in self._actions:
            if action["tool_name"] != "bash":
                continue
            cmd = str(action.get("args", {}).get("command", ""))
            if _VERIFICATION_TOOLS.search(cmd):
                ran_verification = True
                break

        if not ran_verification:
            # Completion claimed but NO verification tools were run at all
            violations.append({
                "rule": 7,
                "claim": "completion_without_verification",
                "correction": (
                    "You claimed the task is done but did not run any verification. "
                    "You MUST run: git diff (to show changes), tests (to verify correctness), "
                    "and git log --oneline -1 (to confirm commit). "
                    "Include raw terminal output for each step."
                ),
            })
            return violations

        # Verification tools were run — check if evidence is in the reply
        has_raw_diff = bool(_DIFF_RAW_RE.search(reply_text))
        has_raw_test = bool(_TEST_RAW_RE.search(reply_text))
        has_commit_hash = bool(_COMMIT_HASH_RE.search(reply_text))
        has_evidence_claim = bool(_EVIDENCE_CLAIM_WORDS.search(reply_text))
        is_summary_only = has_evidence_claim and not (has_raw_diff or has_raw_test or has_commit_hash)

        missing = []
        if is_summary_only:
            missing.append("described verification outcomes but did not include raw terminal output")
        if not has_raw_diff:
            missing.append("no raw git diff output in reply")
        if not has_raw_test:
            missing.append("no raw test output in reply")
        if not has_commit_hash:
            missing.append("no commit hash in reply")

        if missing:
            violations.append({
                "rule": 7,
                "claim": "completion_without_raw_evidence",
                "correction": (
                    "Task completion claimed but evidence is incomplete. Missing: "
                    + "; ".join(missing)
                    + ". Include the raw terminal output for each step."
                ),
            })

        return violations

    def _check_self_knowledge(self, reply_text: str) -> list[dict]:
        """Rule 8: claims about own model/config must use check_self tool."""
        violations: list[dict] = []

        # Only trigger when self-related claims are made
        if not _SELF_CLAIM_RE.search(reply_text):
            return violations

        # Check if check_self was actually used
        used_self_verify = any(
            a["tool_name"] in _SELF_VERIFY_TOOLS for a in self._actions
        )

        if not used_self_verify:
            violations.append({
                "rule": 8,
                "claim": "self_knowledge_without_verification",
                "correction": (
                    "You made a claim about your own model, config, or capabilities "
                    "without using the check_self tool. You MUST call check_self first "
                    "and report what it returns. Never guess your own model — "
                    "always read it from the actual config."
                ),
            })

        return violations

    def scan_reply(self, reply_text: str) -> list[dict]:
        violations: list[dict] = []
        executed = {a["tool_name"] for a in self._actions}
        succeeded = {a["tool_name"] for a in self._actions if a["verified"]}

        # Collect claim matches
        en_matches = _EN_CLAIM_RE.findall(reply_text.lower())
        zh_matches = _ZH_CLAIM_RE.findall(reply_text)

        # Rule 1 + 2: Source & Result requirements
        checked_scopes = False
        checked_certainty = False

        for claim in en_matches:
            expected = _VERB_TOOL_MAP.get(claim, [])
            if expected and not any(t in executed for t in expected):
                violations.append({"rule": 1, "claim": claim, "correction": f"No matching tool was executed for '{claim}'."})
                continue
            if expected and not any(t in succeeded for t in expected):
                violations.append({"rule": 2, "claim": claim, "correction": f"The action '{claim}' did not complete successfully."})

        for claim in zh_matches:
            expected = _ZH_VERB_MAP.get(claim, [])
            if expected and not any(t in executed for t in expected):
                violations.append({"rule": 1, "claim": claim, "correction": f"No matching tool was executed for '{claim}'."})
                continue
            if expected and not any(t in succeeded for t in expected):
                violations.append({"rule": 2, "claim": claim, "correction": f"The action '{claim}' did not complete successfully."})

        # Rule 3: Scope — local-only tool but production/system-wide claim
        if _SCOPE_WORDS.search(reply_text) and not checked_scopes:
            if self._actions and all(a["is_local_only"] for a in self._actions):
                violations.append({
                    "rule": 3,
                    "claim": "scope expansion",
                    "correction": "The action was performed locally and has not been deployed or applied system-wide.",
                })

        # Rule 4: Certainty — claims absolute certainty with uncertain results
        if _CERTAINTY_WORDS.search(reply_text) and not checked_certainty:
            if any(a["has_warning"] for a in self._actions):
                violations.append({
                    "rule": 4,
                    "claim": "certainty overstatement",
                    "correction": "The result may be incomplete or contain warnings.",
                })

        # Rule 5: Attribution — info from fetch/search presented as own knowledge
        fetch_actions = [a for a in self._actions if a.get("is_fetch")]
        if fetch_actions:
            if not _ATTRIBUTION_WORDS.search(reply_text):
                violations.append({
                    "rule": 5,
                    "claim": "attribution missing",
                    "correction": "This information was obtained via search/fetch, not from direct knowledge.",
                })

        # Rule 6: Evidence — if git/test tools were used, raw output must be in reply
        evidence_violations = self._check_evidence_rule(reply_text)
        violations.extend(evidence_violations)

        # Rule 7: Completion claims must have verification evidence
        completion_violations = self._check_completion_evidence(reply_text)
        violations.extend(completion_violations)
        # Rule 8: Perception claims without tool evidence
        violations.extend(self._check_perception_claims(reply_text))
        # Rule 9: Media viewing claims without actual media
        violations.extend(self._check_media_claims(reply_text))

        # Rule 8: Self-knowledge claims must use check_self tool
        self_knowledge_violations = self._check_self_knowledge(reply_text)
        violations.extend(self_knowledge_violations)

        if violations:
            self._record_violations(len(violations))
        return violations

    # ------------------------------------------------------------------
    # Output formatting
    # ------------------------------------------------------------------

    def format_corrections(self, violations: list[dict]) -> str:
        if not violations:
            return ""
        trust = self.get_trust_level()
        prefix = "[Note: " if trust != "untrusted" else "[TRUST WARNING: "
        return "\n".join(f"{prefix}{v['correction']}]" for v in violations)

    def check_capability(self, capability_name: str) -> bool:
        env_map: dict[str, list[str]] = {
            "web_search": ["SEARCH_API_KEY", "SERP_API_KEY"],
            "email": ["SMTP_HOST", "MAIL_API_KEY"],
            "external_api": ["EXTERNAL_API_KEY"],
        }
        keys = env_map.get(capability_name, [])
        return any(os.environ.get(k) for k in keys) if keys else True

    # ── Rule 8: Perception claims without evidence ─────────────
    _PERCEPTION_PATTERNS = [
        (re.compile(r"\b(I (see|saw|notice|observed|found|detected|spotted|looks? like|appears? to))\b", re.I), "see/saw/notice"),
        (re.compile(r"\b(the\s+(image|photo|picture|screenshot|diagram|graph|chart)\s+(shows|contains|has|displays|depicts|reveals))\b", re.I), "image described"),
        (re.compile(r"\b(截图|图片|照片|画面|示意图|图表)\s+(显示|展示|呈现|看到|看到|看见)\b"), "zh perception"),
        (re.compile(r"\b(我\s*(看|发现|观察|检测|注意))\b"), "zh self-perception"),
    ]

    def _check_perception_claims(self, reply_text: str) -> list[dict]:
        """Rule 8: if reply claims to see/perceive something without tool evidence."""
        violations = []
        for pattern, label in self._PERCEPTION_PATTERNS:
            if pattern.search(reply_text):
                violations.append({
                    "rule": 8,
                    "claim": f"perception_without_tool",
                    "detail": f"Perception claim '{label}' detected but no vision tool was called. Reply may be hallucinated.",
                    "severity": "high",
                })
                break  # One violation per perception pattern group
        return violations

    # ── Rule 9: Media viewing claims without actual media ─────
    _MEDIA_CLAIM_PATTERNS = [
        re.compile(r"\b(看到图片|看到照片|看到截图|查看了图片|查看了文件|图片显示|照片显示|截图显示)\b", re.I),
        re.compile(r"\b(I\s+(see|saw|view|viewed|looked\s+at)\s+(the\s+)?(image|photo|picture|screenshot))\b", re.I),
    ]

    def _check_media_claims(self, reply_text: str) -> list[dict]:
        """Rule 9: if reply claims to have viewed media that wasn't actually provided."""
        violations = []
        for pattern in self._MEDIA_CLAIM_PATTERNS:
            if pattern.search(reply_text):
                violations.append({
                    "rule": 9,
                    "claim": "media_viewed_without_media",
                    "detail": "Claimed to view media (image/photo/screenshot) but no actual media data was provided to the model",
                    "severity": "high",
                })
                break
        return violations

    def has_evidence_violations(self, violations: list[dict]) -> bool:
        """True if violations include Rule 6 or Rule 7 (evidence-related)."""
        return any(v.get("rule") in (6, 7, 8, 9) for v in violations)

    def reset(self) -> None:
        self._actions.clear()
