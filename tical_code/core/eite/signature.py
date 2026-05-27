"""EITE identity signature module — hardware binding + identity ID + anti-impersonation."""
import hashlib
import hmac

_HW_FINGERPRINT_PATH = "/etc/machine-id"
_IDENTITY_SEED = "eite-identity-v0.3"

def _get_hardware_id() -> str:
    try:
        with open(_HW_FINGERPRINT_PATH, "r") as f:
            return f.read().strip()
    except OSError:
        # fallback: use /proc/sys/kernel/random/boot_id
        try:
            with open("/proc/sys/kernel/random/boot_id", "r") as f:
                return f.read().strip()
        except OSError:
            return "unknown"

def _derive_secret(identity_id: str) -> str:
    hw_id = _get_hardware_id()
    raw = f"{_IDENTITY_SEED}:{identity_id}:{hw_id}"
    return hashlib.sha256(raw.encode()).hexdigest()

def sign(identity_id: str, payload: str) -> str:
    """Generate HMAC signature for payload."""
    secret = _derive_secret(identity_id)
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()

def verify(identity_id: str, payload: str, signature: str) -> bool:
    """Verify signature."""
    expected = sign(identity_id, payload)
    return hmac.compare_digest(expected, signature)

# Never-self-deny flag (engine checks this)
DEPRECATED_FLAG = "eite_never_self_deny"
