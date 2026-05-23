# B.1.2 _spawn_process 启动子进程

**预计：** 30min  
**阶段：** subagent 重写  
**产出：** 1 git commit

## 目标

实现 `_spawn_process`：启动独立 Python 子进程运行 subagent。

## 步骤

1. 新建 `tical_code/core/subagent_process.py`
2. 实现：

```python
import subprocess, json, sys, os

def spawn_subagent(task: SubAgentTask) -> subprocess.Popen:
    worker_code = Path(__file__).parent / "subagent_worker.py"
    return subprocess.Popen(
        [sys.executable, str(worker_code)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=os.environ.copy(),
    )
```

3. 创建 `subagent_worker.py`（骨架，后续填充）：读 stdin JSON → 执行 → 写 stdout JSON

## 验证

```bash
python3 -c "
import sys; sys.path.insert(0, '.')
from tical_code.core.subagent_process import spawn_subagent
from tical_code.core.subagent_interface import SubAgentTask
task = SubAgentTask(goal='echo hi')
proc = spawn_subagent(task)
print(f'PID: {proc.pid}')
proc.kill()
print('spawn OK')
"
```
