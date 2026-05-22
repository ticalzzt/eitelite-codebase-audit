"""Clarification - ask user for missing information."""

import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# =============================================================================
# 
# =============================================================================

class ClarifyStatus(Enum):
    """ """
    CLEAR = "clear"                           # Target,Exec
    NEEDS_CLARIFICATION = "needs_clarification"  # Target,Confirm
    REJECT = "reject"                         # ornotDone,Reject

class ClarifyStrategy(Enum):
    """ """
    PASS_THROUGH = "pass_through"       # +Target → Exec
    SOFT_CLARIFY = "soft_clarify"       # or → Exec,not
    HARD_CLARIFY = "hard_clarify"       # or → UserConfirmExec
    CONSTITUTION_GATE = "constitution_gate"  #  → 

class AmbiguityType(Enum):
    """ """
    MISSING_INFO = "missing_info"       # Info
    AMBIGUOUS_INTENT = "ambiguous_intent"  # (Target)
    INSUFFICIENT_CONTEXT = "insufficient_context"  # not
    HIGH_RISK_OPERATION = "high_risk_operation"  # Op

# =============================================================================
# 
# =============================================================================

@dataclass
class ClarifyQuestion:
    """ """
    question: str
    options: List[str] = field(default_factory=list)
    reason: str = ""
    default: Optional[str] = None
    required: bool = True
    ambiguity_type: AmbiguityType = AmbiguityType.MISSING_INFO

    def to_dict(self) -> Dict[str, Any]:
        return {
            'question': self.question,
            'options': self.options,
            'reason': self.reason,
            'default': self.default,
            'required': self.required,
            'ambiguity_type': self.ambiguity_type.value,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ClarifyQuestion':
        return cls(
            question=data.get('question', ''),
            options=data.get('options', []),
            reason=data.get('reason', ''),
            default=data.get('default'),
            required=data.get('required', True),
            ambiguity_type=AmbiguityType(data.get('ambiguity_type', 'missing_info')),
        )

@dataclass
class ClarifyResult:
    """ """
    status: ClarifyStatus = ClarifyStatus.CLEAR
    strategy: ClarifyStrategy = ClarifyStrategy.PASS_THROUGH
    questions: List[ClarifyQuestion] = field(default_factory=list)
    confidence: float = 1.0
    ambiguities: List[AmbiguityType] = field(default_factory=list)
    rejection_reason: str = ""
    clarify_id: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'status': self.status.value,
            'strategy': self.strategy.value,
            'questions': [q.to_dict() for q in self.questions],
            'confidence': self.confidence,
            'ambiguities': [a.value for a in self.ambiguities],
            'rejection_reason': self.rejection_reason,
            'clarify_id': self.clarify_id,
            'timestamp': self.timestamp,
        }

@dataclass
class ClarifyAnswer:
    """ """
    clarify_id: str = ""
    answers: Dict[int, str] = field(default_factory=dict)
    all_required_answered: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            'clarify_id': self.clarify_id,
            'answers': self.answers,
            'all_required_answered': self.all_required_answered,
        }

# =============================================================================
#  - and
# =============================================================================

# Op( HARD_CLARIFY or CONSTITUTION_GATE)
_HIGH_RISK_PATTERNS = [
    # Delete/删除
    (r'(?:删除|清除|drop|delete|remove|truncate|purge|erase)', "删除/高风险"),
    # /转账
    (r'(?:转账|汇款|付款|打钱|transfer|pay|purchase|buy|sell|trade)', "转账/财务"),
    # Send/发布
    (r'(?:Send|发布|发送|send|publish|deploy|submit)', "Send/发布操作"),  # v0.5.7: push发布/推送(not git操作)
    # System系统
    (r'(?:修改配置|改配置|修改设置|modify config|change setting|system config)', "系统配置"),
    # Perm权限
    (r'(?:授权|赋权|提权|grant|permission|role|chmod|chown|sudo)', "权限操作"),
]

# Info(Target目标,什么,哪些Param)
_MISSING_INFO_INDICATORS = {
    # Target目标
    "target_missing": [
        r'(?:这个|那个|它|它们|它们|do it|handle it|fix it|take care of it)',
        r'(?:do it|handle it|fix it|take care of it)',
    ],
    # 范围/数量
    "scope_missing": [
        r'(?:一些|几个|一些|有些|some|few|many|a lot)',
        r'(?:全部|所有|一批|batch|all|everything)',
    ],
    # 条件
    "condition_missing": [
        r'(?:合适的时候|需要时|有空时|when appropriate|when needed|if necessary)',
    ],
    # Time时间
    "time_missing": [
        r'(?:尽快|马上|立刻|立即|asap|as soon as|right away)',
    ],
}

# (Target)
_AMBIGUITY_INDICATORS = [
    # 这个那个
    (r'(?:(?:这个)?|(?:那个)?|(?:这个)?|(?:那个)?|这个|那个|这个那个)\s*(?:东西|事情|问题|任务)?(?:什么|啥|是什么|怎么做)', "目标不明确"),
    # 处理修复
    (r'(?:处理|修复|搞定|解决|fix|handle|deal with|process)\s*(?:这个|那个)?(?:东西|事情|问题)', "操作不具体"),
    # 相关
    (r'(?:相关|有关|关联|associated|related|relevant)', "相关性不明确"),
]

# not(Info)
_CONTEXT_INSUFFICIENT_INDICATORS = [
    r'(?:之前|以前|原来的|previous|last|original)',
    r'(?:其他|别的|另一个|other|another)',
    r'(?:某处|某个地方|somewhere|someplace)',
]

# =============================================================================
# ClarifyPhase - 
# =============================================================================

class ClarifyPhase:
    """ """

    # Count()
    MAX_QUESTIONS = 3

    # Value(v0.5.9: Value)
    CONFIDENCE_CLEAR = 0.5       # >= 0.5: Target(Path/,Value0.8→0.5)
    CONFIDENCE_SOFT = 0.4        # 0.4-0.8: (0.50.4)
    CONFIDENCE_HARD = 0.2        # 0.2-0.4: (0.30.2)
    # < 0.2: or

    # Value: N  orphan
    DEFAULT_ORPHAN_THRESHOLD = 2

    def __init__(
        self,
        constitution_enforcer: Optional[Any] = None,
        session_data: Optional[Dict[str, Any]] = None,
        orphan_threshold: int = DEFAULT_ORPHAN_THRESHOLD,
        auto_confirm: bool = False,
    ):
        """ """
        self._enforcer = constitution_enforcer
        self._session_data = session_data or {}
        # GenerateSession ID 
        self._clarify_counter = 0
        # v0.13: 
        self._orphan_count: int = 0
        self._orphan_threshold: int = max(1, orphan_threshold)
        # v0.5.9: Confirm
        self._auto_confirm: bool = auto_confirm

    def analyze_goal(
        self,
        goal: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> ClarifyResult:
        """ """
        if not goal or not isinstance(goal, str):
            return ClarifyResult(
                status=ClarifyStatus.REJECT,
                strategy=ClarifyStrategy.PASS_THROUGH,
                confidence=0.0,
                rejection_reason="TargetorFormatInvalid",
                clarify_id=self._next_clarify_id(),
            )

        context = context or {}

        # 1: Type
        ambiguities = []
        confidence = 1.0  # 

        # 1a. Info
        missing_info = self._detect_missing_info(goal)
        if missing_info:
            ambiguities.append(AmbiguityType.MISSING_INFO)
            # Type: Target
            for mtype in missing_info:
                if mtype == "target_missing":
                    confidence -= 0.35  # OpisInfo
                elif mtype == "scope_missing":
                    confidence -= 0.20  # 
                else:
                    confidence -= 0.15  # 

        # 1b. 
        ambiguous = self._detect_ambiguity(goal)
        if ambiguous:
            ambiguities.append(AmbiguityType.AMBIGUOUS_INTENT)
            confidence -= 0.25 * len(ambiguous)

        # 1c. not
        insufficient = self._detect_insufficient_context(goal)
        if insufficient:
            ambiguities.append(AmbiguityType.INSUFFICIENT_CONTEXT)
            confidence -= 0.15 * len(insufficient)

        # 2: Op
        high_risk = self._detect_high_risk(goal)
        if high_risk:
            ambiguities.append(AmbiguityType.HIGH_RISK_OPERATION)
            confidence -= 0.2

        # 3: Check
        constitution_violation = False
        constitution_reason = ""
        if self._enforcer:
            check_result = self._enforcer.check_action(goal, context)
            if not check_result.allowed:
                constitution_violation = True
                constitution_reason = check_result.reason
                confidence = 0.0

        #  confidence  [0.0, 1.0]
        confidence = max(0.0, min(1.0, confidence))

        # 4: Statusand
        if constitution_violation:
            status = ClarifyStatus.REJECT
            strategy = ClarifyStrategy.CONSTITUTION_GATE
        elif confidence >= self.CONFIDENCE_CLEAR and not high_risk:
            status = ClarifyStatus.CLEAR
            strategy = ClarifyStrategy.PASS_THROUGH
        elif confidence >= self.CONFIDENCE_CLEAR and high_risk:
            # Target → Confirm
            status = ClarifyStatus.NEEDS_CLARIFICATION
            strategy = ClarifyStrategy.HARD_CLARIFY
        elif confidence >= self.CONFIDENCE_SOFT:
            #  → Confirm( auto_confirm )
            if self._auto_confirm:
                status = ClarifyStatus.CLEAR
                strategy = ClarifyStrategy.PASS_THROUGH
            else:
                status = ClarifyStatus.NEEDS_CLARIFICATION
                strategy = ClarifyStrategy.SOFT_CLARIFY
        elif confidence >= self.CONFIDENCE_HARD:
            #  → Confirm
            status = ClarifyStatus.NEEDS_CLARIFICATION
            strategy = ClarifyStrategy.HARD_CLARIFY
        else:
            # 
            status = ClarifyStatus.NEEDS_CLARIFICATION
            strategy = ClarifyStrategy.HARD_CLARIFY

        # 5: Generate
        questions = []
        if status == ClarifyStatus.NEEDS_CLARIFICATION:
            questions = self._generate_questions(
                goal, ambiguities, high_risk, context
            )

        result = ClarifyResult(
            status=status,
            strategy=strategy,
            questions=questions[:self.MAX_QUESTIONS],  # 3
            confidence=round(confidence, 2),
            ambiguities=ambiguities,
            rejection_reason=constitution_reason,
            clarify_id=self._next_clarify_id(),
        )

        logger.info(
            f"[ClarifyPhase] : status={status.value}, "
            f"strategy={strategy.value}, confidence={confidence:.2f}, "
            f"questions={len(questions)}"
        )

        return result

    def evaluate_answer(
        self,
        clarify_result: ClarifyResult,
        answer: ClarifyAnswer,
    ) -> ClarifyResult:
        """ """
        if answer.clarify_id != clarify_result.clarify_id:
            logger.warning(
                f"[ClarifyPhase]  clarify_id : "
                f"{answer.clarify_id} != {clarify_result.clarify_id}"
            )

        # Checkis
        if not answer.all_required_answered:
            #  →  NEEDS_CLARIFICATION
            remaining = []
            for i, q in enumerate(clarify_result.questions):
                if q.required and i not in answer.answers:
                    remaining.append(q)
            return ClarifyResult(
                status=ClarifyStatus.NEEDS_CLARIFICATION,
                strategy=clarify_result.strategy,
                questions=remaining,
                confidence=clarify_result.confidence,
                ambiguities=clarify_result.ambiguities,
                clarify_id=clarify_result.clarify_id,
            )

        #  →  CLEAR
        new_confidence = min(1.0, clarify_result.confidence + 0.3)

        # is CONSTITUTION_GATE,UserConfirm
        if clarify_result.strategy == ClarifyStrategy.CONSTITUTION_GATE:
            return ClarifyResult(
                status=ClarifyStatus.CLEAR,
                strategy=ClarifyStrategy.CONSTITUTION_GATE,
                confidence=new_confidence,
                clarify_id=clarify_result.clarify_id,
            )

        return ClarifyResult(
            status=ClarifyStatus.CLEAR,
            strategy=ClarifyStrategy.PASS_THROUGH,
            confidence=new_confidence,
            clarify_id=clarify_result.clarify_id,
        )

    def _next_clarify_id(self) -> str:
        """ """
        self._clarify_counter += 1
        return f"clarify_{self._clarify_counter}_{int(time.time() * 1000)}"

    # --- (v0.13) ---

    def detect_orphan_tool_tail(self, response: Dict[str, Any]) -> bool:
        """ """
        try:
            if not isinstance(response, dict):
                #  dict Type,not
                self._reset_orphan_count()
                return False

            has_tool_calls = bool(response.get('tool_calls'))
            content = response.get('content')
            # content  None /  /  "Content"
            has_content = bool(content and str(content).strip())

            if has_tool_calls and not has_content:
                #  tool_calls Content → 
                self._orphan_count += 1
                logger.warning(
                    f"[ClarifyPhase] : "
                    f"tool_calls={has_tool_calls}, content, "
                    f"={self._orphan_count}/{self._orphan_threshold}"
                )
                if self._orphan_count >= self._orphan_threshold:
                    logger.error(
                        f"[ClarifyPhase]  {self._orphan_count} ,"
                        f" orphan tool call, HARD_CLARIFY "
                    )
                    return True
                # Value,not
                return False

            # Content → 
            if has_content:
                self._reset_orphan_count()

            return False

        except Exception as e:
            logger.warning(f"[ClarifyPhase] : {e}")
            return False

    def _reset_orphan_count(self) -> None:
        """ """
        if self._orphan_count > 0:
            logger.debug("[ClarifyPhase] ")
        self._orphan_count = 0

    def get_orphan_status(self) -> Dict[str, Any]:
        """ """
        return {
            'orphan_count': self._orphan_count,
            'orphan_threshold': self._orphan_threshold,
            'is_orphan': self._orphan_count >= self._orphan_threshold,
        }

    # ---  ---

    @staticmethod
    def _detect_missing_info(goal: str) -> List[str]:
        """ """
        missing = []
        for info_type, patterns in _MISSING_INFO_INDICATORS.items():
            for pattern in patterns:
                if re.search(pattern, goal, re.IGNORECASE):
                    missing.append(info_type)
                    break  # Type
        return missing

    @staticmethod
    def _detect_ambiguity(goal: str) -> List[str]:
        """ """
        ambiguous = []
        for pattern, desc in _AMBIGUITY_INDICATORS:
            if re.search(pattern, goal, re.IGNORECASE):
                ambiguous.append(desc)
        return ambiguous

    @staticmethod
    def _detect_insufficient_context(goal: str) -> List[str]:
        """ """
        insufficient = []
        for pattern in _CONTEXT_INSUFFICIENT_INDICATORS:
            if re.search(pattern, goal, re.IGNORECASE):
                insufficient.append(f": {pattern}")
        return insufficient

    @staticmethod
    def _detect_high_risk(goal: str) -> List[str]:
        """ """
        risks = []
        for pattern, desc in _HIGH_RISK_PATTERNS:
            if re.search(pattern, goal, re.IGNORECASE):
                risks.append(desc)
        return risks

    # --- Generate ---

    def _generate_questions(
        self,
        goal: str,
        ambiguities: List[AmbiguityType],
        high_risk: List[str],
        context: Dict[str, Any],
    ) -> List[ClarifyQuestion]:
        """ """
        questions = []

        # 1: OpConfirm
        if high_risk and AmbiguityType.HIGH_RISK_OPERATION in ambiguities:
            risk_desc = "、".join(high_risk[:2])
            questions.append(ClarifyQuestion(
                question=f"{risk_desc},?",
                options=["", "", ""],
                reason=f": {risk_desc}",
                required=True,
                ambiguity_type=AmbiguityType.HIGH_RISK_OPERATION,
            ))

        # 2: Info
        if AmbiguityType.MISSING_INFO in ambiguities:
            missing_info = self._detect_missing_info(goal)
            q = self._generate_missing_info_question(goal, missing_info)
            if q:
                questions.append(q)

        # 3: 
        if AmbiguityType.AMBIGUOUS_INTENT in ambiguities:
            ambiguous = self._detect_ambiguity(goal)
            q = self._generate_ambiguity_question(goal, ambiguous)
            if q:
                questions.append(q)

        # 4: 
        if AmbiguityType.INSUFFICIENT_CONTEXT in ambiguities:
            questions.append(ClarifyQuestion(
                question="Info?",
                options=["", ""],
                reason="not",
                default="InfoExec",
                required=False,
                ambiguity_type=AmbiguityType.INSUFFICIENT_CONTEXT,
            ))

        return questions[:self.MAX_QUESTIONS]

    @staticmethod
    def _generate_missing_info_question(
        goal: str,
        missing_types: List[str],
    ) -> Optional[ClarifyQuestion]:
        """ """
        if not missing_types:
            return None

        # TypeGeneratenot
        type_question_map = {
            "target_missing": ClarifyQuestion(
                question="Opis?",
                reason="TargetOp",
                required=True,
                ambiguity_type=AmbiguityType.MISSING_INFO,
            ),
            "scope_missing": ClarifyQuestion(
                question="OpandCount?",
                options=["", "", ""],
                reason="Target/Count",
                required=True,
                ambiguity_type=AmbiguityType.MISSING_INFO,
            ),
            "condition_missing": ClarifyQuestion(
                question="ExecOp?",
                options=["", "", ""],
                reason="TargetExec",
                default="Exec",
                required=False,
                ambiguity_type=AmbiguityType.MISSING_INFO,
            ),
            "time_missing": ClarifyQuestion(
                question="TimeExecOp?",
                options=["", "", ""],
                reason="TargetTime",
                default="Exec",
                required=False,
                ambiguity_type=AmbiguityType.MISSING_INFO,
            ),
        }

        # Type()
        for mtype in missing_types:
            if mtype in type_question_map:
                return type_question_map[mtype]

        return None

    @staticmethod
    def _generate_ambiguity_question(
        goal: str,
        ambiguous_descs: List[str],
    ) -> Optional[ClarifyQuestion]:
        """ """
        if not ambiguous_descs:
            return None

        desc = ambiguous_descs[0]  # 
        return ClarifyQuestion(
            question=f"({desc}),?",
            options=["", "", ""],
            reason=f": {desc}",
            required=True,
            ambiguity_type=AmbiguityType.AMBIGUOUS_INTENT,
        )

# =============================================================================
# 
# =============================================================================

def clarify_goal(
    goal: str,
    context: Optional[Dict[str, Any]] = None,
    constitution_enforcer: Optional[Any] = None,
) -> ClarifyResult:
    """ """
    phase = ClarifyPhase(constitution_enforcer=constitution_enforcer)
    return phase.analyze_goal(goal, context)

def format_clarify_questions(result: ClarifyResult) -> str:
    """ """
    if result.status == ClarifyStatus.CLEAR:
        return ",."

    if result.status == ClarifyStatus.REJECT:
        return f"⛔ : {result.rejection_reason}"

    # NEEDS_CLARIFICATION
    lines = [f"📋 (: {result.confidence:.0%}):\n"]
    for i, q in enumerate(result.questions, 1):
        lines.append(f"  {i}. {q.question}")
        if q.reason:
            lines.append(f"     : {q.reason}")
        if q.options:
            option_str = " / ".join(q.options)
            lines.append(f"     : {option_str}")
        if q.default:
            lines.append(f"     : {q.default}")
        required_mark = "()" if q.required else "()"
        lines.append(f"     {required_mark}")

    return "\n".join(lines)
