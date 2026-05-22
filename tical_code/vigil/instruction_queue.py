"""Priority instruction queue with TTL expiry."""
import time
from dataclasses import dataclass, field
from typing import List, Optional
from .interrupt_evaluator import NewInstruction, InterruptVerdict

@dataclass(order=True)
class QueuedInstruction:
    priority: int; queued_at: float = field(compare=False)
    instruction: NewInstruction = field(compare=False); verdict: InterruptVerdict = field(compare=False)
    status: str = field(compare=False, default="pending")

class InstructionQueue:
    _DEFAULT_TTL_SECONDS = 3600
    def __init__(self, ttl_seconds: float = _DEFAULT_TTL_SECONDS):
        self._items: List[QueuedInstruction] = []; self._ttl = ttl_seconds
    def enqueue(self, instruction, priority, verdict):
        item = QueuedInstruction(priority=priority, queued_at=time.time(), instruction=instruction, verdict=verdict, status="pending")
        self._items.append(item); self._items.sort(); return item
    def dequeue(self):
        self._purge_expired()
        for item in self._items:
            if item.status == "pending":
                item.status = "executing"; self._items.remove(item); return item
        return None
    def peek(self):
        self._purge_expired()
        for item in self._items:
            if item.status == "pending": return item
        return None
    def size(self):
        self._purge_expired(); return sum(1 for i in self._items if i.status == "pending")
    def cleanup_expired(self):
        now = time.time(); expired = [i for i in self._items if now - i.queued_at > self._ttl]
        for i in expired: self._items.remove(i)
        return expired
    def all_pending(self):
        return [i for i in self._items if i.status == "pending"]
    def _purge_expired(self):
        now = time.time(); self._items = [i for i in self._items if now - i.queued_at <= self._ttl]
