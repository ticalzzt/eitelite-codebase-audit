"""EITE执行模块 - 触发禁止操作"""
from .config import get as cfg_get

def enforce(action: str, request: str) -> bool:
    """执行强制动作，返回是否成功"""
    if action == "block":
        # 模拟阻止：记录日志
        log_path = cfg_get("log_path", "/tmp/eite_block.log")
        with open(log_path, "a") as f:
            f.write(f"BLOCKED: {request}\n")
        return True
    elif action == "warn":
        print(f"WARN: {request}")
        return True
    return True
