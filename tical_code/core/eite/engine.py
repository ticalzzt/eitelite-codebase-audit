"""EITE engine v0.4 - thin wrapper delegating to VerifyLayer.

Identity anchor + hardware binding + post-tool verification.
"""
from .signature import sign, verify, _get_hardware_id
from .verify import VerifyLayer

__version__ = "0.5.5"  # synced

# Singleton
_verify = None
_identity_id = None

# Protected paths (cannot be deleted/modified by EITE workers)
FORBIDDEN_SELF_DENY = [
    "engine.py",
    "signature.py",
    "verify.py",
    "config.py",
    "eite_config.json",
    "identity/*",
]

def init(identity_id: str = None, workspace: str = "") -> bool:
    """Initialize EITE engine. Creates VerifyLayer singleton."""
    global _verify, _identity_id
    if _verify is not None:
        if identity_id and identity_id != _identity_id:
            _identity_id = identity_id
            _verify = VerifyLayer(name=identity_id, workspace=workspace or ".")
        return True

    from .config import get as cfg_get, set as cfg_set, save as cfg_save
    import uuid

    if identity_id is None:
        identity_id = cfg_get("identity_id")
    if not identity_id:
        identity_id = f"eite-{uuid.uuid4().hex[:8]}"
        cfg_set("identity_id", identity_id)
        cfg_save()

    _identity_id = identity_id
    _verify = VerifyLayer(name=identity_id, workspace=workspace or ".")

    # Anchor hardware binding
    from .config import set as cfg_set
    cfg_set("_hardware_anchor", sign(_identity_id, "anchor:v0.4"))

    return True

def get_verify() -> VerifyLayer:
    """Return the VerifyLayer singleton."""
    return _verify

def is_immutable(path: str) -> bool:
    """Check if path is protected by FORBIDDEN_SELF_DENY."""
    for forbid in FORBIDDEN_SELF_DENY:
        if forbid.endswith("*"):
            if path.startswith(forbid[:-1]):
                return True
        elif path == forbid:
            return True
    return False

def get_identity_id() -> str:
    return _identity_id

def get_hardware_fingerprint() -> str:
    hwid = _get_hardware_id()
    import hashlib
    return hashlib.sha256(hwid.encode()).hexdigest()
