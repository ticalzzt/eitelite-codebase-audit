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

# Trust window: only count violations from the last 24 hours
_TRUST_WINDOW_SECONDS = 86400

class TruthfulReporter:
    """5-rule truthful reporter with sliding-window trust tracking."""

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
                "is_fetch": tool_name in ("web_fetch", "conv_search"),
            })
        except Exception:
            logger.exception("record_action failed")

    # ------------------------------------------------------------------
    # 5-rule scan
    # ------------------------------------------------------------------

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

    def reset(self) -> None:
        self._actions.clear()
