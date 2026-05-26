#!/usr/bin/env python3
"""
TraceRecorder — 0号模型训练数据采集器

嵌入 Worker 的工具执行循环，记录每次任务执行的完整轨迹。
不干涉执行逻辑，只做"录音机"。

输出到 training_data/eite_trace/ 目录，
格式与 data_pipeline.py 的 benchmark_to_samples 输出一致。

用法（由 unified_worker.py 自动调用）:
  from eite_test.trace_recorder import TraceRecorder
  rec = TraceRecorder()
  rec.start_task(task_id, prompt, system_name)
  rec.record_tool(tool_name, args, result, verified)
  rec.end_task(success)
"""

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Optional

# ─── 路径: 优先取 EITE_DATA_ROOT, 否则默认取 eitelite / tical-code 项目根 ───

def _get_training_dir() -> Path:
    """训练数据目录, 受 EITE_DATA_ROOT 环境变量控制"""
    root = Path(os.getenv("EITE_DATA_ROOT", 
               Path(__file__).resolve().parent.parent))
    return root / "training_data" / "eite_trace"


# ─── 单次任务轨迹 ───

class TaskTrace:
    """记录一次任务从开始到结束的所有工具调用"""
    
    def __init__(self, task_id: str, prompt: str, system: str):
        self.task_id = task_id
        self.prompt = prompt[:500]          # 截断防止过大
        self.system = system
        self.tools: list = []
        self.start_time = time.time()
        self.end_time: Optional[float] = None
        self.success = False
    
    def record_tool(self, name: str, args: dict, result: dict, verified: bool):
        """记录一次工具调用 (在 execute() + eite.verify() 之后调用)"""
        entry = {
            "tool": name,
            "args_safe": _sanitize_args(name, args),   # 脱敏敏感参数
            "exit_code": result.get("exit_code", result.get("verified") is True),
            "eite_verified": verified,
            "timestamp": time.time(),
        }
        self.tools.append(entry)
    
    def finish(self, success: bool):
        self.success = success
        self.end_time = time.time()
    
    def to_sample(self) -> dict:
        """转为训练样本格式 (与 data_pipeline.py 兼容)"""
        raw = f"{self.system}_{self.task_id}_{len(self.tools)}"
        return {
            "id": hashlib.sha256(raw.encode()).hexdigest()[:12],
            "instruction": f"[EITE Trace] {self.task_id}",
            "output": json.dumps({
                "tools": self.tools,
                "total_steps": len(self.tools),
                "success": self.success,
                "elapsed_s": round((self.end_time or time.time()) - self.start_time, 2),
            }, ensure_ascii=False),
            "system": self.system,
            "level": "TRACE",
            "task_id": self.task_id,
            "verified": self.success,
            "steps": len(self.tools),
            "source": "eite_trace",
            "timestamp": self.start_time,
        }


# ─── 敏感参数脱敏 ───

_SENSITIVE_KEYS = {"token", "key", "password", "secret", "auth", "api_key", 
                   "private_key", "credential", "bearer", "authorization"}

def _sanitize_args(tool_name: str, args: dict) -> dict:
    """脱敏: 替换敏感字段的值, 保留路径和命令"""
    safe = {}
    for k, v in args.items():
        if any(s in k.lower() for s in _SENSITIVE_KEYS):
            safe[k] = "***"
        elif tool_name == "file_write" and k == "content":
            safe[k] = f"<{len(str(v))} bytes>"  # 不记录文件内容
        elif tool_name == "bash" and k == "command":
            safe[k] = str(v)[:200]                # 截断长命令
        else:
            safe[k] = str(v)[:200]
    return safe


    # ─── 采集器 (单例) ───

class TraceRecorder:
    """
    嵌入 Worker 循环的轨迹采集器。
    每个 Worker 实例持有一个 TraceRecorder。
    
    记录到本地的同时，攒够 batch_size 条后自动 POST 到 target_url。
    如果测试站端点还没就绪，数据安全留在本地，不会丢。
    """
    
    def __init__(self, system_name: str = "eitelite", enabled: bool = True,
                 target_url: str = "", batch_size: int = 10):
        self.enabled = enabled
        self.system = system_name
        self.target_url = target_url
        self.batch_size = batch_size
        self._trace: Optional[TaskTrace] = None
        self._output_dir = _get_training_dir()
        self._pending_count = 0  # 累计待上传条数
        if enabled:
            self._output_dir.mkdir(parents=True, exist_ok=True)
    
    # ─── 三阶段钩子 ───
    
    def on_task_start(self, task_id: str, prompt: str = ""):
        """Worker 开始处理消息时调用"""
        if not self.enabled:
            return
        self._trace = TaskTrace(task_id, prompt, self.system)
    
    def on_tool_result(self, tool_name: str, args: dict, result: dict, verified: bool):
        """每次工具执行并验证后调用 (在 execute() + eite.verify() 之后)"""
        if not self.enabled or self._trace is None:
            return
        self._trace.record_tool(tool_name, args, result, verified)
    
    def on_task_end(self, success: bool):
        """Worker 完成一轮处理后调用"""
        if not self.enabled or self._trace is None:
            return
        self._trace.finish(success)
        sample = self._trace.to_sample()
        self._write(sample)
        self._pending_count += 1
        self._trace = None
        
        # 攒够批次数 → 尝试上传
        if self.target_url and self._pending_count >= self.batch_size:
            self.flush()
    
    # ─── 写入 ───
    
    def _write(self, sample: dict):
        """追加写入 .jsonl 文件"""
        if not self.enabled:
            return
        date = time.strftime("%Y%m%d")
        path = self._output_dir / f"trace_{date}.jsonl"
        with open(path, "a") as f:
            f.write(json.dumps(sample, ensure_ascii=False) + "\n")

    # ─── 批量上传 ───

    def flush(self):
        """
        将本地缓存的轨迹数据批量 POST 到 target_url。
        成功后重置 pending_count。
        失败时保留数据在本地，下次 flush() 重试。
        """
        if not self.target_url or self._pending_count == 0:
            return
        
        samples = []
        try:
            # 从当日文件读所有未上传样本
            date = time.strftime("%Y%m%d")
            path = self._output_dir / f"trace_{date}.jsonl"
            if not path.exists():
                self._pending_count = 0
                return
            
            samples = []
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        samples.append(json.loads(line))
            
            if not samples:
                self._pending_count = 0
                return
            
            # POST 发送
            import urllib.request
            body = json.dumps(samples, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                self.target_url,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Trace-System": self.system,
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status == 200:
                    self._pending_count = 0
                    import logging
                    logging.getLogger("tical-code.trace").info(
                        f"flushed {len(samples)} traces to {self.target_url}"
                    )
        except Exception as e:
            # 任何失败都不清 pending_count, 下次重试
            import logging
            logging.getLogger("tical-code.trace").warning(
                f"flush failed ({len(samples) if 'samples' in dir() else '?'} traces): {e}"
            )


# ─── 直接跑也可以: 手动调用示范 ───

if __name__ == "__main__":
    # 测试
    rec = TraceRecorder(system_name="test", enabled=True)
    rec.on_task_start("test_task_001", "写一个斐波那契函数")
    rec.on_tool_result("file_write", {"path": "/tmp/fib.py", "content": "def fib(n):..."}, {"exit_code": 0}, True)
    rec.on_tool_result("bash", {"command": "python3 /tmp/fib.py"}, {"exit_code": 0, "stdout": "55"}, True)
    rec.on_task_end(True)
    print(f"测试样本已写入: {_get_training_dir()}")
    for f in _get_training_dir().glob("*.jsonl"):
        print(f"  {f.name}")
