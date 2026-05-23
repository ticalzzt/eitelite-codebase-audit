# B.1.3 Worker 循环

**预计：** 45min  
**阶段：** subagent 重写  
**产出：** 1 git commit

## 目标

实现 subagent worker 的进程内主循环：读任务 → LLM 调用 → 工具执行 → 结果收集 → 写回。

## 步骤

1. 新建 `tical_code/core/subagent_worker.py`
2. 主函数 `run_task(task: SubAgentTask) -> SubAgentResult`：
   - 从 stdin 读 JSON（任务描述）
   - 初始化 LLM backend
   - 循环：LLM call → 如果有 tool_calls → 执行 → 继续；否则返回结果
   - 最多 `max_rounds` 轮
   - 捕获超时/异常
3. stdout 写 JSON 结果
4. 错误输出到 stderr

## 验证

```bash
echo '{"goal":"print(1+1)","context":"","tools":["bash"],"max_rounds":2}' | \
python3 -c "
import sys; sys.path.insert(0, '.')
from tical_code.core.subagent_worker import run_task
from tical_code.core.subagent_interface import SubAgentTask
task = SubAgentTask(goal='echo hello')
result = run_task(task)
print(f'success={result.success} output={result.output[:40]}')
"
```
