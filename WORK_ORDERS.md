# EITElite 工单列表

每个工单为30分钟可完成的任务。AI 会话读取 `git log --oneline -5` 即可知道从哪里接。

## 状态图例

- ⬜ 未开始
- 🟡 进行中
- ✅ 已完成
- ❌ 已取消

---

## Epic C — LLM Interface Function Calling

C.2 `llm_interface.py` 加 function calling 支持

| # | 状态 | Commit | 描述 |
|---|------|--------|------|
| C.2.1 | ⬜ | — | `chat()` 签名加 `tools` 参数，透传到 API request body |
| C.2.2 | ⬜ | — | 解析 API 响应中 `tool_calls`，返回结构化 `ToolCall` 对象 |
| C.2.3 | ⬜ | — | 多轮 tool 调用循环（LLM→tool_call→执行→结果喂回→继续） |
| C.2.4 | ⬜ | — | 错误处理：超时 / 格式异常 / 重试 |
| C.2.5 | ⬜ | — | `unified_worker` 中 `llm_backend` → `llm_interface` 替换 |
| C.2.6 | ⬜ | — | 端到端：worker 接收任务 → 调工具 → 返回结果 |

---

## Epic B — Subagent 重写

B.1 子代理系统，独立 Python 子进程执行

| # | 状态 | Commit | 描述 |
|---|------|--------|------|
| B.1.1 | ⬜ | — | 数据类：`SubAgentTask` + `SubAgentResult` |
| B.1.2 | ⬜ | — | `_spawn_process`：启动独立 Python 子进程 |
| B.1.3 | ⬜ | — | 子进程内调 LLM（复用 `llm_interface`） |
| B.1.4 | ⬜ | — | 子进程内调 `tool_executor` 执行工具 |
| B.1.5 | ⬜ | — | 子进程 → 父进程结果回传（stdout JSON） |
| B.1.6 | ⬜ | — | 限制子进程超时 + 内存 |
| B.1.7 | ⬜ | — | `unified_worker` 接入 `delegate_task` 调用新 subagent |
| B.1.8 | ⬜ | — | 端到端 + 压力测试 |

---

## 小活 Z

| # | 状态 | Commit | 描述 | 预计 |
|---|------|--------|------|------|
| Z.1 | ⬜ | — | 删除 `hive.py`（未使用模块） | 5min |
| Z.2 | ⬜ | — | 跑 EITE benchmark mock（验证 integrity stage） | 15min |

---

## 当前进度

```
Epic → C.2 [■■□□□□] 0/6   B.1 [□□□□□□□□] 0/8   Z [□□] 0/2
```

下一个 AI 工作会话：
1. 读本文件，找第一个 ⬜ 状态的工单
2. 读 `git log --oneline -5` 确认上下文
3. 执行工单
4. `git commit -m "C.2.1: ..."` 提交
5. 更新本文件对应状态为 ✅
