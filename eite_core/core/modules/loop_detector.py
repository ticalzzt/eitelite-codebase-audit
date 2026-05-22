"""Module 3: Loop Detection - detect repetitive tool-call patterns."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from typing import Optional

logger = logging.getLogger("tical-code.loop_detector")

class LoopDetector:
    """4-pattern loop detector: stagnation → exact_repeat → ping_pong → arg_drift.

    Detection order matters: stagnation is checked FIRST because a session
    where all calls fail should be identified as stagnation, not mis-classified
    as argument drift.
    """

    def __init__(self, window_size: int = 30):
        self.window_size = window_size
        self._records: list[dict] = []
        self._injected: set[str] = set()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _hash(obj) -> str:
        raw = json.dumps(obj, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()

    @staticmethod
    def _has_cjk(text: str) -> bool:
        return any("\u4e00" <= c <= "\u9fff" for c in text)

    @staticmethod
    def _jaccard(a: str, b: str, cjk: bool = False) -> float:
        if cjk:
            def bigrams(s):
                return set(s[i:i + 2] for i in range(len(s) - 1)) if len(s) > 1 else set(s)
            sa, sb = bigrams(a), bigrams(b)
        else:
            sa = set(re.findall(r"\w+", a.lower()))
            sb = set(re.findall(r"\w+", b.lower()))
        if not sa and not sb:
            return 1.0
        if not sa or not sb:
            return 0.0
        return len(sa & sb) / len(sa | sb)

    @staticmethod
    def _is_success(result: dict) -> bool:
        if result.get("ok") is True:
            return True
        if result.get("exit_code") == 0:
            return True
        return False

    # ------------------------------------------------------------------
    # Record & Detect
    # ------------------------------------------------------------------

    def record(self, tool_name: str, args: dict, result: dict) -> None:
        try:
            self._records.append({
                "tool_name": tool_name,
                "args_hash": self._hash(args),
                "result_hash": self._hash(result),
                "result_text": json.dumps(result, ensure_ascii=False)[:200],
                "success": self._is_success(result),
                "ts": time.time(),
            })
            if len(self._records) > self.window_size:
                self._records.pop(0)
        except Exception:
            logger.exception("record failed")

    def detect(self) -> Optional[dict]:
        try:
            rec = self._records
            if len(rec) < 2:
                return None

            # ORDER MATTERS: stagnation first, then exact, ping-pong, drift
            result = self._check_stagnation(rec)
            if result:
                return result

            result = self._check_exact_repeat(rec)
            if result:
                return result

            result = self._check_ping_pong(rec)
            if result:
                return result

            result = self._check_arg_drift(rec)
            if result:
                return result

            return None
        except Exception:
            logger.exception("detect failed")
            return None

    # ------------------------------------------------------------------
    # Pattern checkers
    # ------------------------------------------------------------------

    def _check_exact_repeat(self, rec: list) -> Optional[dict]:
        if len(rec) < 2:
            return None
        last = rec[-1]
        count = 1
        for i in range(len(rec) - 2, -1, -1):
            if rec[i]["tool_name"] == last["tool_name"] and rec[i]["args_hash"] == last["args_hash"]:
                count += 1
            else:
                break
        if count >= 3:
            return self._emit("exact_repeat", "critical", count)
        if count >= 2:
            return self._emit("exact_repeat", "warning", count)
        return None

    def _check_arg_drift(self, rec: list) -> Optional[dict]:
        by_tool: dict[str, list] = {}
        for r in rec:
            by_tool.setdefault(r["tool_name"], []).append(r)
        for tool, entries in by_tool.items():
            if len(entries) < 3:
                continue
            similar = 0
            for i in range(len(entries) - 1):
                if entries[i]["args_hash"] == entries[i + 1]["args_hash"]:
                    continue
                a, b = entries[i]["result_text"], entries[i + 1]["result_text"]
                cjk = self._has_cjk(a) or self._has_cjk(b)
                if self._jaccard(a, b, cjk) > 0.85:
                    similar += 1
            if similar >= 5:
                return self._emit("arg_drift", "critical", similar)
            if similar >= 3:
                return self._emit("arg_drift", "warning", similar)
        return None

    def _check_ping_pong(self, rec: list) -> Optional[dict]:
        if len(rec) < 4:
            return None
        names = [r["tool_name"] for r in rec[-8:]]
        cycles = 0
        for i in range(0, len(names) - 3, 2):
            if names[i] == names[i + 2] and names[i + 1] == names[i + 3] and names[i] != names[i + 1]:
                cycles += 1
        if cycles >= 3:
            return self._emit("ping_pong", "critical", cycles)
        if cycles >= 2:
            return self._emit("ping_pong", "warning", cycles)
        return None

    def _check_stagnation(self, rec: list) -> Optional[dict]:
        if len(rec) < 5:
            return None
        successes = sum(1 for r in rec if r["success"])
        ratio = successes / len(rec)
        if ratio < 0.2:
            return self._emit("stagnation", "warning", len(rec))
        return None

    def _emit(self, loop_type: str, level: str, count: int) -> dict:
        msg = (
            f"Loop detected ({loop_type}). "
            f"Break out by trying a different approach or reply with what you have."
        )
        logger.warning("Loop detected type=%s level=%s count=%d", loop_type, level, count)
        return {"type": loop_type, "level": level, "count": count, "message": msg}

    def reset(self) -> None:
        self._records.clear()
        self._injected.clear()
