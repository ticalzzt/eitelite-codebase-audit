"""EITE verification module — powered by verify_engine_v2."""
from tical_code.core.eite.verify_engine_v2 import VerificationEngine

def get_verify():
    """Return VerificationEngine instance or None."""
    try:
        return VerificationEngine()
    except (ImportError, Exception):
        return None
