# B.1.4 超时 / 错误 / 清理

**预计：** 30min  
**阶段：** subagent 重写  
**产出：** 1 git commit

## 目标

给 subagent worker 加安全保护：超时终止、异常捕获、资源清理。

## 步骤

1. `run_task` 加 `signal.alarm(timeout_sec)` 超时保护（Unix only，备选 threading.Timer）
2. 外层 try/except/finally：捕获任何异常，确保子进程不会变成僵尸
3. 超时时 kill 子进程组，防止残留
4. 清理临时文件
5. 超时结果：`SubAgentResult(success=False, error="timeout")`

## 验证

```python
task = SubAgentTask(goal='import time; time.sleep(999)', timeout_sec=2)
result = run_task(task)
assert result.success == False
assert "timeout" in result.error.lower()
```
