# EITElite Manifest

## 文件清单（34 files · 5,920 lines）

| 文件 | 行数 | 内部依赖 | 相对导入 | 外部依赖 |
|------|------|----------|----------|----------|
| `__init__.py` | 0 | — | — | — |
| `core/__init__.py` | 0 | — | — | — |
| `core/channel.py` | 160 | — | — | — |
| `core/clarify.py` | 621 | — | — | — |
| `core/config.py` | 60 | — | — | — |
| `core/cron_scheduler.py` | 702 | — | — | — |
| `core/eite/__init__.py` | 15 | — | from .signature import sign, verify, _get_hardware_id, EITE_IMMUTABLE_FLAG; from .verify import VerifyLayer; from .engine import init, get_verify, is_immutable, get_identity_id, get_hardware_fingerprint, FORBIDDEN_SELF_DENY, __version__ | — |
| `core/eite/config.py` | 34 | — | — | — |
| `core/eite/engine.py` | 69 | — | from .signature import sign, verify, _get_hardware_id, EITE_IMMUTABLE_FLAG; from .verify import VerifyLayer; from .config import get, set, save; from .config import set | — |
| `core/eite/signature.py` | 38 | — | — | import hmac |
| `core/eite/verify.py` | 149 | — | from .signature import sign, verify, _get_hardware_id | — |
| `core/llm_backend.py` | 186 | — | — | — |
| `core/memory_store.py` | 543 | — | — | — |
| `core/modules/__init__.py` | 0 | — | — | — |
| `core/modules/context_compactor.py` | 118 | — | — | — |
| `core/modules/loop_detector.py` | 184 | — | — | — |
| `core/modules/proposal_gate.py` | 163 | — | — | — |
| `core/modules/session_manager.py` | 189 | — | — | — |
| `core/modules/truthful_reporter.py` | 232 | — | — | — |
| `core/prompt.py` | 105 | — | — | — |
| `core/response_formatter.py` | 79 | — | — | — |
| `core/tool_executor.py` | 971 | from tical_code.core.clarify import ClarifyPhase; from tical_code.core.cron_scheduler import CronScheduler; from tical_code.core.memory_store import MemoryFTSStore | from .memory_sense import conversation_search | import tical_code.core.tool_executor; from plugins.vision import VisionPlugin; from plugins.vision import VisionPlugin; from plugins.browser.browser_controller import BrowserController |
| `core/unified_worker.py` | 536 | from tical_code.core.channel import Message, Response, TelegramChannel, TicalChatChannel; from tical_code.core.llm_backend import create_llm_backend; from tical_code.core.tool_executor import execute, TOOL_SCHEMAS; from tical_code.core.response_formatter import format_result, format_error, format_progress; from tical_code.core.eite import init, get_verify; from tical_code.core.prompt import build_system_prompt; from tical_code.core.config import load_config; from tical_code.core.modules.session_manager import SessionManager; from tical_code.core.modules.context_compactor import ContextCompactor; from tical_code.core.modules.loop_detector import LoopDetector; from tical_code.core.modules.truthful_reporter import TruthfulReporter; from tical_code.core.modules.proposal_gate import ProposalGate; from tical_code.vigil import build_vigil, NewInstruction | — | import tical_code.core.tool_executor |
| `vigil/__init__.py` | 80 | — | from .vigil_config import VigilConfig, load_config; from .signal_collector import SignalCollector, CombinedSignal, InteractionSignal, PhysioSignal; from .ai_signal_collector import AISignalCollector, AISignal; from .state_classifier import StateClassifier, StateResult, StateRecord; from .ai_state_classifier import AIStateClassifier, AIStateResult; from .vigil_judge import VigilJudge, VigilVerdict, InterventionRequest; from .interrupt_evaluator import AIInterruptEvaluator, NewInstruction, InterruptVerdict; from .instruction_queue import InstructionQueue, QueuedInstruction; from .trace_log import VigilTraceStore, VigilTrace; from .actions import VigilActions | — |
| `vigil/actions.py` | 48 | — | from .vigil_judge import VigilVerdict; from .vigil_config import VigilCoreConfig | import smtplib; from email.mime.text import MIMEText |
| `vigil/ai_signal_collector.py` | 96 | — | — | — |
| `vigil/ai_state_classifier.py` | 38 | — | from .ai_signal_collector import AISignal | — |
| `vigil/instruction_queue.py` | 40 | — | from .interrupt_evaluator import NewInstruction, InterruptVerdict | — |
| `vigil/interrupt_evaluator.py` | 59 | — | from .ai_state_classifier import AIStateResult | — |
| `vigil/signal_collector.py` | 101 | — | — | — |
| `vigil/state_classifier.py` | 109 | — | from .signal_collector import CombinedSignal, InteractionSignal, PhysioSignal; from .vigil_config import ClassifierConfig | — |
| `vigil/trace_log.py` | 49 | — | from .state_classifier import StateResult; from .vigil_judge import VigilVerdict, InterventionRequest | — |
| `vigil/vigil_config.py` | 86 | — | — | import yaml |
| `vigil/vigil_judge.py` | 61 | — | from .state_classifier import StateResult; from .interrupt_evaluator import AIInterruptEvaluator, NewInstruction, InterruptVerdict; from .vigil_config import VigilCoreConfig | — |

**总计：5921 行**

## Import 活跃度

所有 internal import 都是活的（unified_worker 启动时加载链上的）。
没有死 import。

## 与 tical-code 的边界

| | EITElite | tical-code |
|---|---|---|
| 仓库 | ticalzzt/eitelite | ticalzzt/tical-code |
| Python包名 | tical_code（同名，不能同机共存） | tical_code |
| 文件数 | 34 | 100+ |
| 行数 | ~5,920 | ~47,481 |
| 包含 | 核心运行时 + vigil + eite | 全量（含插件/benchmarks/tests/research） |
| 不含 | plugins/, benchmarks/, tests/, research/, 未接线的core模块 | — |
| 运行时工具 | 24个（bash/file/memory/chat/browser/cron/delegate等） | 同（工具定义在tool_executor.py） |
| 外部依赖 | 0（纯stdlib） | 需要各种第三方包 |
| 适用场景 | 小VPS、最小化部署、快速启动 | 开发、测试、完整功能 |

## 未包含的 tical-code 文件（可后续按需接入）

### plugins/（7个插件目录）
- browser/ — 浏览器自动化（stealth_browser, tls_fingerprint等）
- xurl/ — X/Twitter操作
- vision/ — 图像分析
- search_plugin/ — 搜索
- messenger/ — 消息平台
- trading/ — 交易
- cloud_device/ — 云设备

### core/ 未接入模块（约50个文件）
- self_repair.py — 自修复
- sandbox.py — 沙箱执行
- detection.py — 检测
- doom_loop.py — 死循环检测（被loop_detector替代）
- worker_loop.py — 旧版主循环（被unified_worker替代）
- worker.py / worker_framework.py — 旧版worker
- ticobot_worker.py — 旧版worker
- agent_runtime.py — agent运行时
- browser_bridge_tool.py — 浏览器桥接
- memory.py / memory_boot.py / memory_evolve.py / memory_sense.py — 高级记忆
- model_router.py / enhanced_router.py — 模型路由
- reflection.py / reflection_adapter.py / refiner.py — 反思
- constitution.py / axioms.py — 宪法/公理
- verify.py / verify_pipeline.py — 验证管道
- heartbeat.py / hive.py / identity.py — 心跳/集群/身份
- 更多...