# B.1.1 SubAgent 数据类

**预计：** 10min  
**阶段：** subagent 重写  
**产出：** 1 git commit

## 目标

定义 SubAgent 系统的数据类和接口。

## 步骤

1. 新建 `tical_code/core/subagent_interface.py`
2. 定义数据类：

```python
@dataclass
class SubAgentTask:
    goal: str
    context: str = ""
    tools: List[str] = field(default_factory=lambda: ["bash", "file_read", "file_write"])
    max_rounds: int = 5
    timeout_sec: float = 120.0

@dataclass
class SubAgentResult:
    success: bool
    output: str = ""
    error: str = ""
    tool_calls_made: int = 0
    elapsed_sec: float = 0.0
```

## 验证

```bash
python3 -c "
import sys; sys.path.insert(0, '.')
from tical_code.core.subagent_interface import SubAgentTask, SubAgentResult
t = SubAgentTask(goal='test', context='test')
r = SubAgentResult(success=True, output='ok')
print('import OK')
"
```
