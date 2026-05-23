# C.2.4 错误处理：超时 / 格式异常 / 重试

**预计：** 30min  
**阶段：** llm_interface → function calling  
**产出：** 1 git commit

## 目标

给 function calling 增加健壮的错误处理：API 超时不崩、tools 参数格式异常不崩、HTTP 4xx/5xx 自动重试。

## 步骤

1. `chat_with_tools` 每轮加 timeout 保护（默认 30s）
2. tools 参数校验：每个 tool 必须有 `name` 和 `parameters`，非法则跳过而非崩
3. 自动重试：HTTP 429/5xx 时最多 3 次指数退避重试
4. HTTP 400 不重试（参数错），直接返回错误
5. `chat()` 中 `tool_calls` 解析加 try/except，解析失败当纯文本处理

## 验证

```bash
# 传非法 tools 参数
python3 -c "
import sys; sys.path.insert(0, '.')
from tical_code.core.llm_interface import DeepSeekProvider
p = DeepSeekProvider(...)
resp = p.chat([{'role':'user','content':'hi'}], tools=[{'bad': 'format'}])
print('Did not crash:', resp)
# 应该正常返回文本回复
"
```
