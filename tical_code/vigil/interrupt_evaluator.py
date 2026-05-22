"""AI interrupt evaluator - intercept new instructions."""
import re, time
from dataclasses import dataclass, field
from .ai_state_classifier import AIStateResult

@dataclass
class NewInstruction:
    content: str; source: str = "human"; urgency_hint: float = 0.0; timestamp: float = field(default_factory=time.time)

@dataclass
class InterruptVerdict:
    action: str; reason: str; estimated_context_loss: float; queue_priority: int; cooldown_minutes: float; notify_message: str = ""

_URGENT = re.compile(r"紧急|着火|出事了|emergency|urgent|critical|fire|asap|immediately", re.I)
_HURRY = re.compile(r"快点|催|搞快|速度|faster|hurry[^,]*|speed up|come on", re.I)
_REDIRECT = re.compile(r"方向错了|停止|停下|stop|cancel|abort|重新来|start over", re.I)
_PARALLEL = re.compile(r"顺便|顺便帮|另外|还有个事|by the way|also|quick question|one more thing", re.I)

_KEEP_GOING = re.compile(r"继续|没事|不用管我|keep going|im fine|dont stop|继续干", re.I)

class AIInterruptEvaluator:
    _CONTEXT_LOSS_MAP = {"DEEP_WORK": 0.8, "REASONING": 0.7, "GENERATING": 0.6, "WAITING": 0.0, "STUCK": 0.0}
    def evaluate_new_instruction(self, instruction, ai_state):
        state = ai_state.state; base_loss = self._CONTEXT_LOSS_MAP.get(state, 0.5)
        if state == "WAITING":
            return InterruptVerdict("execute_now", "AI idle", 0.0, 1, 0)
        if state == "STUCK":
            return InterruptVerdict("interrupt_current", "AI stuck", 0.0, 1, 0)
        category = self._categorise(instruction)
        # FATIGUE override: user says "keep going" — respect it
        if _KEEP_GOING.search(instruction.content):
            return InterruptVerdict("execute_now", "User overrides fatigue guard: keep going", 0.0, 1, 0)
        if category == "urgent":
            return InterruptVerdict("execute_now", "Urgent instruction", base_loss, 1, 0)
        if category == "hurry":
            return InterruptVerdict("reject", "Hurry with no content", 0.0, 5, 5, "AI is executing. Please wait.")
        if category == "redirect":
            estimated_loss = self._adjusted_loss(base_loss, ai_state)
            if estimated_loss < 0.5:
                return InterruptVerdict("interrupt_current", "Direction change", estimated_loss, 1, 0)
            else:
                return InterruptVerdict("queue", "Direction change but near done", estimated_loss, 1, 0,
                    notify_message="Current task near completion. Direction change queued.")
        if category == "parallel":
            return InterruptVerdict("queue", "Non-urgent parallel request", 0.0, 3, 0, "Request received, will process after current task.")
        if instruction.urgency_hint >= 0.8:
            return InterruptVerdict("execute_now", "High urgency hint", base_loss, 1, 0)
        return InterruptVerdict("queue", "General instruction while busy", 0.0, 3, 0, "Instruction queued.")
    def _categorise(self, instruction):
        text = instruction.content
        if instruction.urgency_hint >= 0.9 or _URGENT.search(text): return "urgent"
        if _HURRY.search(text.strip()): return "hurry"
        if _REDIRECT.search(text): return "redirect"
        if _PARALLEL.search(text): return "parallel"
        return "general"
    @staticmethod
    def _adjusted_loss(base_loss, state):
        progress_factor = min(1.0, state.duration_seconds / 120)
        return base_loss * (1 - progress_factor * 0.5)
