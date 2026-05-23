# C.2.3 多轮 tool 调用循环

**预计：** 30min  
**阶段：** llm_interface → function calling  
**产出：** 1 git commit

## 目标

实现多轮 tool 调用循环：LLM 返回 `tool_call` → 执行工具 → 结果喂回 LLM → 继续（最多 N 轮）。

## 步骤

1. 在 `DeepSeekProvider` 加方法 `chat_with_tools(messages, tools, max_rounds=10)`：

```python
def chat_with_tools(self, messages, tools=None, max_rounds=10):
    for round in range(max_rounds):
        resp = self.chat(messages, tools)
        if not resp.tool_calls:
            return resp.content
        # 添加 assistant 消息含 tool_calls
        messages.append({"role": "assistant", "content": resp.content, "tool_calls": [...]})
        for tc in resp.tool_calls:
            # 执行工具（由调用方提供 executor）
            result = executor(tc.name, tc.arguments)
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result)})
    return "Max rounds reached"
```

2. `executor` 是个回调：`Callable[[str, dict], dict]`，由 `tool_executor.execute` 实现
3. 每轮记录日志

## 验证

mock 一个 executor 返回 fake 结果，调 `chat_with_tools` 看是否循环 N 轮后终止。
