# C.2.1 chat() 签名加 tools 参数

**预计：** 30min  
**阶段：** llm_interface → function calling  
**产出：** 1 git commit

## 目标

在 `llm_interface.py` 的 `DeepSeekerProvider.chat()` 方法签名中增加 `tools` 参数，并将其透传到 DeepSeek API 的 request body 中。

## 步骤

1. 读 `tical_code/core/llm_interface.py`，定位 `class DeepSeekProvider` 和 `chat()` 方法
2. 在 `chat()` 签名加 `tools: Optional[List[dict]] = None` 参数
3. 在 request body 构建处：如果 `tools is not None`，添加 `"tools": tools` 字段
4. 保持现有逻辑不变（tools 为空时行为完全相同）

## 验证

```bash
python3 -c "
import sys; sys.path.insert(0, '.')
from tical_code.core.llm_interface import DeepSeekProvider
print('import OK')
p = DeepSeekProvider(api_key='test', base_url='https://api.deepseek.com/v1', model='deepseek-chat')
# 不带 tools 调用，不能报错
import inspect
sig = inspect.signature(p.chat)
assert 'tools' in sig.parameters, 'tools param missing'
print('tools param OK')
"
```

## Commit

```
git commit -m "C.2.1: add tools param to chat()"
```
