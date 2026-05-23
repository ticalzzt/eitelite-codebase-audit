# EITElite 递推工作清单 — Work Orders

> 记录需要推进的未完成任务，按优先级和依赖排序。
> 每完成一项标记 ✅ 并记录 git commit。

## 状态图例

- ❌ 未开始
- ✅ 已完成
- 🔧 进行中
- ⏸️ 暂停/阻塞

---

## Epic C — LLM Interface Function Calling

**目标：** 将 `unified_worker.py` + `tool_executor.py` 的紧耦合工具执行逻辑重构到 `llm_interface.py`，实现 `chat_with_tools` 多轮循环 + 健壮错误处理。

### C.2.1 chat() 加 tools 参数 ❌
- `.tickets/C.2.1.md`
- chat() 加 `tools: Optional[List[Dict]] = None` 参数，tools 直接传 API

### C.2.2 解析 tool_calls 返回 ToolCall ❌
- `.tickets/C.2.2.md`
- 解析 API 响应的 tool_calls → `ChatResponse(content, tool_calls=[ToolCall(id, name, args)])`

### C.2.3 多轮 tool 调用循环 ❌
- `.tickets/C.2.3.md`
- `chat_with_tools(messages, tools, max_rounds=10, executor=...)` — 循环：调 LLM → 执行 → 喂回

### C.2.4 错误处理 ❌
- `.tickets/C.2.4.md`
- 超时 / 非法 tools / HTTP 4xx 不崩、自动重试

### C.2.5 替换 worker 的 llm_backend → llm_interface ❌
- `.tickets/C.2.5.md`
- `unified_worker.py` 改 import，适配 `ChatResponse` 数据结构

### C.2.6 端到端验证 ❌
- `.tickets/C.2.6.md`
- tical-chat 发消息 → LLM 决定调工具 → 执行 → 返回

---

## Epic B — Subagent 重写

**目标：** 重写 delegate_task/subagent_result/subagent_list 工具，用子进程隔离代替当前的内存队列。

### B.1.1 SubAgent 数据类 ✅
- `.tickets/B.1.1.md`
- `SubAgentTask` + `SubAgentResult` 数据类

### B.1.2 _spawn_process 启动子进程 ❌
- `.tickets/B.1.2.md`
- 独立 Python 进程跑 subagent worker

### B.1.3 Worker 循环 ❌
- `.tickets/B.1.3.md`
- 子进程内：读任务 → LLM → 工具 → 写结果

### B.1.4 超时 / 错误 / 清理 ❌
- `.tickets/B.1.4.md`
- signal 超时、异常捕获、清理

### B.1.5 tool_executor 集成 ❌
- 替换当前 delegate_task/subagent_result/subagent_list 实现

### B.1.6 eitelite_cli submit/result 命令 ❌
- 命令行列表面向用户的 subagent 提交

### B.1.7 多 worker 并发 ❌
- 允许最多 N 个并发 subagent

### B.1.8 端到端集成测试 ❌
- 全链路通过

---

## Done ✅

| 项目 | Commit | 日期 |
|---|---|---|
| tical-code 全量同步 | `76ac153` | 2026-05-23 |
| 记忆系统升级 | `edb055d` | 2026-05-23 |
| 一键部署 install.sh | `3fdb5f3` | 2026-05-23 |
| WORK_ORDERS + .tickets/ | `3fdb5f3` | 2026-05-23 |

## 当前进度

- C.2: 6/6 工单已写（C.2.1-2.6）— 等待执行
- B.1: 4/8 工单已写（B.1.1-1.4）— 缺 B.1.5-1.8
- 待执行：C.2.1 → C.2.2 → C.2.3 → C.2.4 → C.2.5 → C.2.6
