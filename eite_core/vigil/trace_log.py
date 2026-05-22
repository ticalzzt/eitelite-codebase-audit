"""Decision trace - immutable audit log."""
import json, os, time, uuid
from dataclasses import asdict, dataclass, field
from collections import deque
from typing import Deque, List, Optional
from .state_classifier import StateResult
from .vigil_judge import VigilVerdict, InterventionRequest

@dataclass
class VigilTrace:
    trace_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    timestamp: float = field(default_factory=time.time)
    state: Optional[dict] = None; intervention_request: Optional[dict] = None
    physio_available: bool = False; verdict: Optional[dict] = None
    human_response: Optional[str] = None; outcome: Optional[str] = None

class VigilTraceStore:
    _RING_SIZE = 500
    def __init__(self, log_path=None):
        self._ring: Deque[VigilTrace] = deque(maxlen=self._RING_SIZE)
        self._index = {}
        if log_path is None: log_path = os.path.join(os.path.dirname(__file__), "guardian_trace.jsonl")
        self._log_path = log_path
    def record(self, state, verdict, request=None, physio_available=False):
        trace = VigilTrace(state=asdict(state), verdict=asdict(verdict),
            intervention_request=asdict(request) if request else None, physio_available=physio_available)
        self._ring.append(trace); self._index[trace.trace_id] = trace
        self._append_jsonl(trace); return trace.trace_id
    def update_outcome(self, trace_id, human_response=None, outcome=None):
        trace = self._index.get(trace_id)
        if trace is None: return False
        if human_response is not None: trace.human_response = human_response
        if outcome is not None: trace.outcome = outcome
        self._append_jsonl(trace, tag="UPDATE"); return True
    def recent(self, n=20): return list(self._ring)[-n:]
    def get(self, trace_id): return self._index.get(trace_id)
    def summary(self, last_n=100):
        items = self.recent(last_n)
        if not items: return {}
        actions, states = {}, {}
        for t in items:
            if t.verdict: a = t.verdict.get("action", "unknown"); actions[a] = actions.get(a, 0) + 1
            if t.state: s = t.state.get("state", "unknown"); states[s] = states.get(s, 0) + 1
        return {"total": len(items), "actions": actions, "states": states}
    def _append_jsonl(self, trace, tag="NEW"):
        try:
            line = json.dumps({"_tag": tag, **asdict(trace)}, ensure_ascii=False)
            with open(self._log_path, "a", encoding="utf-8") as f: f.write(line + "\n")
        except OSError: pass
