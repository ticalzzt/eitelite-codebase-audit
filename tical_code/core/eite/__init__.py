"""EITE - Execution Integrity & Trust Enforcement.

Post-tool verification layer for tical-code workers.
"""
from .verify_engine_v2 import VerificationEngine
from .signature import sign, verify, _get_hardware_id