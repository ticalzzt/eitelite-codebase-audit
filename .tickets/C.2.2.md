# C.2.2 解析 tool_calls 返回 ToolCall

**预计：** 30min  
**阶段：** llm_interface → function calling  
**产出：** 1 git commit

## 目标

解析 DeepSeek API 响应中的 `tool_calls` 字段，返回结构化 `ToolCall` 对象而非纯文本。

## 步骤

1. 在 `llm_interface.py` 中定义 `ToolCall` 数据类：

```python
@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict
```

2. 修改 `chat()` 返回值，从返回字符串改为返回 `ChatResponse`：

```python
@dataclass
class ChatResponse:
    content: str
    tool_calls: List[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
```

3. 解析 API 响应中的 `choices[0].message.tool_calls`，每个转为 `ToolCall`
4. 更新所有调用 `chat()` 的地方适配新返回值

## 验证

```bash
python3 -c "
import sys; sys.path.insert(0, '.')
from tical_code.core.llm_interface import ToolCall, ChatResponse
print('data classes OK')
"
```
