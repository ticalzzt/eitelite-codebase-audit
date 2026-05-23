"""Usage tracking stub — EITElite lite version."""

import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger("tical-code.usage")

class UsageTracker:
    """Minimal usage tracker — no-op stub for EITElite."""
    
    def __init__(self, db_path: str = "~/.tical-code/usage.db"):
        self._start_time = time.time()
        self._call_count = 0
        self._token_count = 0
    
    def record_tokens(self, prompt_tokens: int = 0, completion_tokens: int = 0, 
                      model: str = "", session_id: str = None, **kwargs):
        self._token_count += prompt_tokens + completion_tokens
    
    def record_api_call(self, provider: str = "", model: str = "", 
                        latency_ms: float = 0, success: bool = True, **kwargs):
        self._call_count += 1
    
    def record_storage(self, session_id: str = None, bytes_delta: int = 0):
        pass
    
    def get_session_summary(self, session_id: str) -> Dict[str, Any]:
        return {"session_id": session_id, "calls": 0}
    
    def get_summary(self, since: float = 0) -> Dict[str, Any]:
        return {
            "uptime_sec": time.time() - self._start_time,
            "calls": self._call_count,
            "tokens": self._token_count,
        }


def get_tracker() -> UsageTracker:
    """Get or create the global tracker."""
    global _tracker
    if _tracker is None:
        _tracker = UsageTracker()
    return _tracker

def record_tokens(**kwargs):
    tracker = get_tracker()
    tracker.record_tokens(**kwargs)

def record_api_call(**kwargs):
    tracker = get_tracker()
    tracker.record_api_call(**kwargs)

_tracker = None
