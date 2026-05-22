"""Human-side signal collection."""
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, List, Optional

@dataclass
class InteractionSignal:
    last_input_time: float = 0.0
    input_interval_avg: float = 0.0
    input_interval_variance: float = 0.0
    session_duration: float = 0.0
    consecutive_work_hours: float = 0.0
    input_error_rate: float = 0.0
    response_length_trend: str = "stable"
    task_switch_frequency: float = 0.0

@dataclass
class PhysioSignal:
    heart_rate: float = 0.0
    hrv: float = 0.0
    spo2: float = 0.0
    eda: float = 0.0
    temperature: float = 0.0
    source: str = "none"
    timestamp: float = field(default_factory=time.time)

@dataclass
class CombinedSignal:
    interaction: InteractionSignal = field(default_factory=InteractionSignal)
    physio: Optional[PhysioSignal] = None
    collected_at: float = field(default_factory=time.time)

@dataclass
class _InputEvent:
    timestamp: float; char_count: int; had_error: bool

@dataclass
class _ResponseEvent:
    timestamp: float; length: int

@dataclass
class _TaskSwitchEvent:
    timestamp: float

class SignalCollector:
    _MAX_EVENTS = 500
    def __init__(self, physio_source=None):
        self._session_start = time.time()
        self._work_start = time.time()
        self._inputs: Deque[_InputEvent] = deque(maxlen=self._MAX_EVENTS)
        self._responses: Deque[_ResponseEvent] = deque(maxlen=200)
        self._switches: Deque[_TaskSwitchEvent] = deque(maxlen=200)
        self._physio_source = physio_source
    def record_input(self, char_count=1, had_error=False):
        self._inputs.append(_InputEvent(time.time(), char_count, had_error))
    def record_response(self, length):
        self._responses.append(_ResponseEvent(time.time(), length))
    def record_task_switch(self):
        self._switches.append(_TaskSwitchEvent(time.time()))
    def reset_work_clock(self):
        self._work_start = time.time()
    def collect(self):
        return CombinedSignal(interaction=self.get_interaction_signal(), physio=self.get_physio_signal(), collected_at=time.time())
    def get_interaction_signal(self):
        now = time.time(); sig = InteractionSignal()
        sig.last_input_time = self._inputs[-1].timestamp if self._inputs else 0.0
        sig.session_duration = (now - self._session_start) / 60.0
        sig.consecutive_work_hours = (now - self._work_start) / 3600.0
        window_start = now - 30 * 60
        recent = [e for e in self._inputs if e.timestamp >= window_start]
        if len(recent) >= 2:
            intervals = [recent[i].timestamp - recent[i-1].timestamp for i in range(1, len(recent))]
            avg = sum(intervals) / len(intervals)
            sig.input_interval_avg = avg
            sig.input_interval_variance = sum((x-avg)**2 for x in intervals) / len(intervals)
        else:
            sig.input_interval_avg = (now - sig.last_input_time) if sig.last_input_time else 9999.0
        if recent:
            sig.input_error_rate = sum(1 for e in recent if e.had_error) / len(recent)
        sig.response_length_trend = self._compute_length_trend()
        hour_ago = now - 3600
        sig.task_switch_frequency = float(sum(1 for s in self._switches if s.timestamp >= hour_ago))
        return sig
    def get_physio_signal(self):
        if self._physio_source is None: return None
        try: return self._physio_source.fetch()
        except: return None
    def _compute_length_trend(self):
        recent = list(self._responses)[-10:]
        if len(recent) < 6: return "stable"
        half = len(recent) // 2
        old_avg = sum(r.length for r in recent[:half]) / half
        new_avg = sum(r.length for r in recent[half:]) / (len(recent) - half)
        ratio = new_avg / old_avg if old_avg > 0 else 1.0
        if ratio > 1.2: return "increasing"
        if ratio < 0.8: return "decreasing"
        return "stable"

class PhysioAdapter:
    def fetch(self): raise NotImplementedError
