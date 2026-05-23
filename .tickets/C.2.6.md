# C.2.6 端到端：worker 调工具返回结果

**预计：** 30min  
**阶段：** llm_interface → function calling  
**产出：** git commit + tical-chat 验证

## 目标

让 worker 通过 tical-chat 接收消息 → LLM 决定调工具 → 执行 → 返回结果给用户。全链路通。

## 步骤

1. 确认 `chat_with_tools` executor 回调 = `tool_executor.execute`
2. 改 `unified_worker.py` 主循环：收到消息后调 `self.llm.chat_with_tools(conv, tools=TOOL_SCHEMAS_CLEAN, executor=self._execute_tool)`
3. 实现 `_execute_tool` 方法做：gate 检查 → EITE 验证 → execute → format → 返回
4. 移除旧的工具执行循环（~100行），替换为 `chat_with_tools`

## 验证

```bash
# 通过 tical-chat 发消息要 worker 创建文件
curl -s -X POST http://REPLACED_TAIWAN_IP:8080/v1/messages \
  -H "Content-Type: application/json" \
  -H "X-AI-Key: REPLACED_SHARED_KEY" \
  -H "X-AI-Identity: seoul" \
  -d '{"sender":"seoul","target":"ani","content":"create a file /tmp/test_c2e6.txt with content hello"}'

# 等10秒验证
ssh ... "cat /tmp/test_c2e6.txt"
# 应输出 "hello"
```
