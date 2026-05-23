# 接口缺失审计
# 日期: 2026-05-23
# 范围: EITElite 42 个 Python 文件

## 总览

- 42 个文件，**0** 个有正式接口（ABC/abstractmethod/Protocol）
- 18 个紧耦合导入，全在 unified_worker.py
- 8 个模块需要接口（~1,212 行）
- 已处理: 1/8 (llm_backend, C.2 50%)

## 需要接口的 8 个模块

| # | 模块 | 行数 | 建议接口 | 优先级 |
|---|------|------|---------|--------|
| 1 | llm_backend.py | 224 | `LLMProvider(ABC)` + `DeepSeekProvider` / `OpenAIProvider` | P0 — 进行中 |
| 2 | loop_detector.py | 184 | `LoopDetectorInterface(ABC)` | P2 |
| 3 | proposal_gate.py | 166 | `GateInterface(ABC)` | P2 |
| 4 | session_manager.py | 189 | `SessionInterface(ABC)` | P2 |
| 5 | truthful_reporter.py | 224 | `ReporterInterface(ABC)` | P2 |
| 6 | context_compactor.py | 118 | `CompactorInterface(ABC)` | P2 |
| 7 | eite/engine.py | 69 | `EiteEngineInterface(ABC)` | P3 |
| 8 | eite/signature.py | 38 | 无（纯工具函数） | P3 |

## 紧耦合导入 (18 处)

全部集中在 unified_worker.py。
