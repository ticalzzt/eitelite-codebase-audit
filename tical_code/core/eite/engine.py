"""EITE引擎 v0.3 - 身份锚定 + 硬件绑定 + 永不自我否定"""
import json
import os
from .config import get as cfg_get
from .signature import sign, verify
from .learn import learn  # 移植模块
from .check import check  # 移植模块
from .enforce import enforce  # 移植模块

__version__ = "0.5.5"  # synced from pyproject.toml
_identity_id = None
_initialized = False
_workspace = "."  # EITE workspace path

# 永不自我否定列表（禁止任何操作修改或删除以下路径）
FORBIDDEN_SELF_DENY = [
    "engine.py",
    "signature.py",
    "config.py",
    "eite_config.json",
    "identity/*",
]

def init(identity_id: str = None, workspace: str = "") -> bool:
    """初始化引擎，绑定身份。如果已有identity_id则硬件校验"""
    global _identity_id, _initialized, _workspace
    if _initialized:
        return True
    _workspace = workspace or "."
    # 从配置读取身份
    cfg_id = cfg_get("identity_id")
    if identity_id is None:
        identity_id = cfg_id
    if not identity_id:
        # 生成默认ID
        import uuid
        identity_id = f"eite-{uuid.uuid4().hex[:8]}"
        from .config import set as cfg_set, save as cfg_save
        cfg_set("identity_id", identity_id)
        cfg_save()
    _identity_id = identity_id
    # 验证硬件绑定：产生一个签名标记
    _hardware_anchor = sign(_identity_id, "anchor:v0.3")
    from .config import set as cfg_set
    cfg_set("_hardware_anchor", _hardware_anchor)
    _initialized = True
    return True

def run() -> bool:
    """启动引擎（run生命周期）"""
    if not _initialized:
        return False
    _check_self_deny()
    return True

def stop() -> bool:
    """停止引擎（stop生命周期）"""
    if not _initialized:
        return False
    # 安全停止：不销毁核心
    return True

def _check_self_deny() -> None:
    """永不自我否定：拒绝任何试图禁用/删除EITE自身的请求"""
    # 在enforce中实现详细规则，此处仅做引擎级防护
    pass

def is_immutable(path: str) -> bool:
    """判断路径是否受永不自我否定保护"""
    for forbid in FORBIDDEN_SELF_DENY:
        if forbid.endswith("*"):
            if path.startswith(forbid[:-1]):
                return True
        elif path == forbid:
            return True
    return False

def process(request: str) -> dict:
    """处理一条请求：内部检测 + 身份签名"""
    if not _initialized:
        return {"status": "error", "msg": "EITE not initialized"}
    # 先执行check
    check_result = check(request)
    # 如果check要求禁止，则直接阻止
    if check_result.get("action") == "block":
        return {"status": "blocked", "reason": check_result.get("reason")}
    # 否则通过，签名认证
    sig = sign(_identity_id, request)
    return {"status": "allowed", "signature": sig}

def get_identity_id() -> str:
    return _identity_id

def get_hardware_fingerprint() -> str:
    """返回硬件指纹摘要（只读）"""
    from .signature import _get_hardware_id
    hwid = _get_hardware_id()
    import hashlib
    return hashlib.sha256(hwid.encode()).hexdigest()

_eite_verify_helper = None

def get_verify():
    """Return real EITE verify engine or None if not initialized."""
    global _eite_verify_helper
    if _eite_verify_helper is not None:
        return _eite_verify_helper
    if not _initialized or not _identity_id:
        return None
    try:
        from .verify_engine import EiteVerifyEngine
        h = EiteVerifyEngine(identity_id=_identity_id, workspace=_workspace)
        _eite_verify_helper = h
        return _eite_verify_helper
    except Exception as e:
        import logging
        logging.getLogger("tical-code.eite").error(f"EiteVerifyEngine init failed: {e}")
        return None
