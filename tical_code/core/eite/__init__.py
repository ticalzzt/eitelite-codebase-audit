"""EITE - Execution Integrity & Trust Enforcement.

Post-tool verification layer for tical-code workers.
"""
from .signature import sign, verify, _get_hardware_id
from .verify import VerifyLayer
from .engine import (
    init,
    get_verify,
    is_immutable,
    get_identity_id,
    get_hardware_fingerprint,
    FORBIDDEN_SELF_DENY,
    __version__,
)
