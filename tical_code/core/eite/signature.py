"""EITE身份签名模块 - 硬件绑定 + 身份ID + 防冒充"""
import hashlib
import json
import os
import hmac

_HW_FINGERPRINT_PATH = "/etc/machine-id"
_IDENTITY_SEED = "eite-identity-v0.3"

def _get_hardware_id() -> str:
    try:
        with open(_HW_FINGERPRINT_PATH, "r") as f:
            return f.read().strip()
    except:
        # fallback: 使用 /proc/sys/kernel/random/boot_id
        try:
            with open("/proc/sys/kernel/random/boot_id", "r") as f:
                return f.read().strip()
        except:
            return "unknown"

def _derive_secret(identity_id: str) -> str:
    hw_id = _get_hardware_id()
    raw = f"{_IDENTITY_SEED}:{identity_id}:{hw_id}"
    return hashlib.sha256(raw.encode()).hexdigest()

def sign(identity_id: str, payload: str) -> str:
    """对payload生成HMAC签名"""
    secret = _derive_secret(identity_id)
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()

def verify(identity_id: str, payload: str, signature: str) -> bool:
    """验证签名"""
    expected = sign(identity_id, payload)
    return hmac.compare_digest(expected, signature)

# 永不自我否定标识（engine会检查）
DEPRECATED_FLAG = "eite_never_self_deny"
