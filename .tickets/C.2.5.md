# C.2.5 unified_worker 替换 llm_backend → llm_interface

**预计：** 30min  
**阶段：** llm_interface → function calling  
**产出：** 1 git commit

## 目标

将 `unified_worker.py` 中 `self.llm = create_llm_backend(...)` 替换为 `self.llm = DeepSeekProvider(...)`。

## 步骤

1. 改 `unified_worker.py` 的 import：`from tical_code.core.llm_interface import DeepSeekProvider, ChatResponse, ToolCall`
2. 替换 `create_llm_backend` 为 `DeepSeekProvider` 初始化
3. 适配 `self.llm.call()` 调用 → 改为 `self.llm.chat()`
4. `chat()` 返回 `ChatResponse`，适配所有读取 `response.content` 和 `response.tool_calls` 的代码
5. 保持 `_executor_llm` 全局暴露不变

## 关键改动

```python
# 之前
self.llm = create_llm_backend(model=..., api_key=..., base_url=...)
response = self.llm.call(conv, tools=TOOL_SCHEMAS_CLEAN)
content = response.get("content", "")
tool_calls = response.get("tool_calls", [])

# 之后
self.llm = DeepSeekProvider(api_key=..., base_url=..., model=...)
response = self.llm.chat(conv, tools=TOOL_SCHEMAS_CLEAN)
content = response.content
tool_calls = [{"id": tc.id, "name": tc.name, "args": tc.args} for tc in response.tool_calls]
```

## 验证

worker 启动不报错：
```
sudo systemctl restart unified-worker-ani && sleep 3 && systemctl is-active unified-worker-ani
# 应输出 active
```
