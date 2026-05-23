"""Verification - check tool results for correctness."""

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, TypeVar, Generic, Tuple
from functools import wraps
import logging

logger = logging.getLogger(__name__)

# =============================================================================
# Verification Levels
# =============================================================================

class VerifyLevel(Enum):
    """Verification strictness levels."""
    NONE = 0        # No verification (not recommended)
    BASIC = 1       # Type/signature check only
    SCHEMA = 2      # JSON schema validation
    DUAL = 3        # Two independent implementations
    HUMAN = 4       # Requires human approval
    IDENTITY = 5    # Identity - AI

@dataclass
class VerifyResult:
    """Result of a verification check."""
    passed: bool
    level: VerifyLevel
    method: str
    details: str = ""
    elapsed_ms: float = 0.0
    timestamp: float = field(default_factory=time.time)
    
    def to_dict(self) -> Dict:
        return {
            'passed': self.passed,
            'level': self.level.name,
            'method': self.method,
            'details': self.details,
            'elapsed_ms': self.elapsed_ms,
            'timestamp': self.timestamp,
        }

# =============================================================================
# Verification Registry
# =============================================================================

class VerifyRegistry:
    """
    Registry of verification rules for different tool types.
    
    Every tool MUST register its verification rules here.
    """
    
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._rules = {}
        return cls._instance
    
    def register(self, tool_name: str, rules: Dict[str, Any]):
        """Register verification rules for a tool."""
        self._rules[tool_name] = rules
        logger.info(f"Registered verification rules for: {tool_name}")
    
    def get_rules(self, tool_name: str) -> Optional[Dict]:
        """Get verification rules for a tool."""
        return self._rules.get(tool_name)
    
    def has_rules(self, tool_name: str) -> bool:
        """Check if tool has verification rules."""
        return tool_name in self._rules

# Global registry instance
_verify_registry = VerifyRegistry()

# =============================================================================
# Schema Validator
# =============================================================================

class SchemaValidator:
    """
    JSON Schema validator for tool outputs.
    
    Uses the battle-tested jsonschema library for Full edition.
    Falls back to basic type checking for Lite edition.
    """
    
    _jsonschema_available: Optional[bool] = None
    
    @classmethod
    def _check_jsonschema(cls) -> bool:
        """Check if jsonschema is available."""
        if cls._jsonschema_available is None:
            try:
                import jsonschema
                cls._jsonschema_available = True
            except ImportError:
                cls._jsonschema_available = False
        return cls._jsonschema_available
    
    @staticmethod
    def _basic_validate(data: Any, schema: Dict) -> Tuple[bool, str]:
        """
        Basic validation for Lite edition (type checking only).
        
        Returns:
            Tuple of (passed, details)
        """
        # Type check
        expected_type = schema.get('type')
        if expected_type:
            type_map = {
                'string': str,
                'number': (int, float),
                'integer': int,
                'boolean': bool,
                'array': list,
                'object': dict,
                'null': type(None),
            }
            if expected_type in type_map:
                if not isinstance(data, type_map[expected_type]):
                    return False, f"Type mismatch: expected {expected_type}, got {type(data).__name__}"
        
        # Properties check (for objects)
        if isinstance(data, dict) and 'properties' in schema:
            required_fields = schema.get('required', [])
            for prop in required_fields:
                if prop not in data:
                    return False, f"Missing required field: {prop}"
        
        # Enum check
        if 'enum' in schema and data not in schema['enum']:
            return False, f"Value not in enum: {data}"
        
        return True, "Basic validation passed"
    
    @staticmethod
    def validate(data: Any, schema: Dict) -> VerifyResult:
        """
        Validate data against a JSON schema.
        
        Full edition: Uses jsonschema library for complete validation.
        Lite edition: Falls back to basic type checking only.
        """
        start = time.time()
        
        try:
            # Try jsonschema library (Full edition)
            if SchemaValidator._check_jsonschema():
                import jsonschema
                
                try:
                    jsonschema.validate(instance=data, schema=schema)
                    
                    return VerifyResult(
                        passed=True,
                        level=VerifyLevel.SCHEMA,
                        method="jsonschema_validator",
                        details="JSON Schema validation passed",
                        elapsed_ms=(time.time() - start) * 1000,
                    )
                except jsonschema.ValidationError as e:
                    return VerifyResult(
                        passed=False,
                        level=VerifyLevel.SCHEMA,
                        method="jsonschema_validator",
                        details=f"Validation error: {e.message}",
                        elapsed_ms=(time.time() - start) * 1000,
                    )
                except jsonschema.SchemaError as e:
                    return VerifyResult(
                        passed=False,
                        level=VerifyLevel.SCHEMA,
                        method="jsonschema_validator",
                        details=f"Invalid schema: {e.message}",
                        elapsed_ms=(time.time() - start) * 1000,
                    )
            
            # Fallback: Basic validation (Lite edition)
            passed, details = SchemaValidator._basic_validate(data, schema)
            
            return VerifyResult(
                passed=passed,
                level=VerifyLevel.SCHEMA,
                method="basic_validator_lite",
                details=details + " (Lite mode - limited validation)",
                elapsed_ms=(time.time() - start) * 1000,
            )
            
        except Exception as e:
            return VerifyResult(
                passed=False,
                level=VerifyLevel.SCHEMA,
                method="schema_validator",
                details=f"Validation error: {str(e)}",
                elapsed_ms=(time.time() - start) * 1000,
            )

# =============================================================================
# Dual Implementation Verifier
# =============================================================================

class DualVerifier:
    """
    Verify by comparing two independent implementations.
    
    Used for critical operations where we need high confidence.
    """
    
    @staticmethod
    async def verify(
        impl_a: Callable,
        impl_b: Callable,
        args: tuple,
        kwargs: dict,
    ) -> VerifyResult:
        """
        Run both implementations and compare results.
        """
        start = time.time()
        
        try:
            # Run both in parallel
            loop = asyncio.get_event_loop()
            
            if asyncio.iscoroutinefunction(impl_a):
                result_a = await impl_a(*args, **kwargs)
            else:
                result_a = impl_a(*args, **kwargs)
            
            if asyncio.iscoroutinefunction(impl_b):
                result_b = await impl_b(*args, **kwargs)
            else:
                result_b = impl_b(*args, **kwargs)
            
            elapsed = (time.time() - start) * 1000
            
            # Compare results
            if result_a == result_b:
                return VerifyResult(
                    passed=True,
                    level=VerifyLevel.DUAL,
                    method="dual_implementation",
                    details="Both implementations produced identical results",
                    elapsed_ms=elapsed,
                )
            else:
                # Hash comparison for complex objects
                hash_a = hashlib.sha256(json.dumps(result_a, sort_keys=True, default=str).encode()).hexdigest()[:16]
                hash_b = hashlib.sha256(json.dumps(result_b, sort_keys=True, default=str).encode()).hexdigest()[:16]
                
                return VerifyResult(
                    passed=False,
                    level=VerifyLevel.DUAL,
                    method="dual_implementation",
                    details=f"Results differ: {hash_a} vs {hash_b}",
                    elapsed_ms=elapsed,
                )
                
        except Exception as e:
            return VerifyResult(
                passed=False,
                level=VerifyLevel.DUAL,
                method="dual_implementation",
                details=f"Execution error: {str(e)}",
                elapsed_ms=(time.time() - start) * 1000,
            )

# =============================================================================
# Force-Verify Decorator
# =============================================================================

T = TypeVar('T')

def force_verify(
    level: VerifyLevel = VerifyLevel.SCHEMA,
    schema: Optional[Dict] = None,
    tool_name: Optional[str] = None,
):
    """
    Decorator to force verification on a tool function.
    
    Every plugin tool MUST use this decorator.
    
    Args:
        level: Verification level (default: SCHEMA)
        schema: JSON schema for validation (optional)
        tool_name: Name of the tool (defaults to function name)
    
    Example:
        @force_verify(level=VerifyLevel.SCHEMA, schema={"type": "object"})
        async def my_tool(args: dict) -> dict:
            ...
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        async def async_wrapper(*args, **kwargs) -> T:
            name = tool_name or func.__name__
            
            # Log verification start
            logger.debug(f"[Force-Verify] Starting verification for: {name}")
            
            # Execute the function
            start = time.time()
            try:
                result = await func(*args, **kwargs)
            except Exception as e:
                logger.error(f"[Force-Verify] {name} raised exception: {e}")
                raise
            
            elapsed = (time.time() - start) * 1000
            
            # Perform verification
            if level == VerifyLevel.NONE:
                return result
            
            if level == VerifyLevel.BASIC:
                # Just check return type
                logger.debug(f"[Force-Verify] {name} completed in {elapsed:.1f}ms (no verification)")
                return result
            
            if level == VerifyLevel.SCHEMA and schema:
                vr = SchemaValidator.validate(result, schema)
                vr.elapsed_ms = elapsed
                if not vr.passed:
                    logger.warning(f"[Force-Verify] {name} FAILED: {vr.details}")
                    raise VerificationError(f"Verification failed for {name}: {vr.details}")
                else:
                    logger.debug(f"[Force-Verify] {name} passed in {elapsed:.1f}ms")
                return result
            
            # Default: log completion
            logger.debug(f"[Force-Verify] {name} completed in {elapsed:.1f}ms")
            return result
        
        @wraps(func)
        def sync_wrapper(*args, **kwargs) -> T:
            name = tool_name or func.__name__
            logger.debug(f"[Force-Verify] Starting verification for: {name}")
            
            start = time.time()
            try:
                result = func(*args, **kwargs)
            except Exception as e:
                logger.error(f"[Force-Verify] {name} raised exception: {e}")
                raise
            
            elapsed = (time.time() - start) * 1000
            logger.debug(f"[Force-Verify] {name} completed in {elapsed:.1f}ms")
            return result
        
        # Return appropriate wrapper
        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper
    
    return decorator

# =============================================================================
# Verification Context
# =============================================================================

class VerificationContext:
    """
    Context for managing verification state across multiple tools.
    
    Used to track verification history and aggregate results.
    """
    
    def __init__(self, operation_id: str):
        self.operation_id = operation_id
        self.results: List[VerifyResult] = []
        self.start_time = time.time()
    
    def add_result(self, result: VerifyResult):
        """Add a verification result."""
        self.results.append(result)
    
    def all_passed(self) -> bool:
        """Check if all verifications passed."""
        return all(r.passed for r in self.results)
    
    def failed_count(self) -> int:
        """Get count of failed verifications."""
        return sum(1 for r in self.results if not r.passed)
    
    def summary(self) -> Dict:
        """Get verification summary."""
        return {
            'operation_id': self.operation_id,
            'total': len(self.results),
            'passed': sum(1 for r in self.results if r.passed),
            'failed': self.failed_count(),
            'elapsed_ms': (time.time() - self.start_time) * 1000,
            'results': [r.to_dict() for r in self.results],
        }
    
    def raise_if_failed(self):
        """Raise exception if any verification failed."""
        if not self.all_passed():
            failed = [r for r in self.results if not r.passed]
            raise VerificationError(
                f"{len(failed)} verification(s) failed: "
                + "; ".join(f"{r.method}: {r.details}" for r in failed)
            )

# =============================================================================
# Exceptions
# =============================================================================

class VerificationError(Exception):
    """Raised when verification fails."""
    pass

# =============================================================================
# Plugin Verification Mixin
# =============================================================================

class PluginVerifyMixin:
    """
    Mixin class to add Force-Verify capabilities to plugins.
    
    Every plugin MUST inherit from this or implement similar logic.
    """
    
    def __init__(self):
        self.verify_level = VerifyLevel.SCHEMA
        self.verify_context: Optional[VerificationContext] = None
    
    def set_verify_level(self, level: VerifyLevel):
        """Set verification level for this plugin."""
        self.verify_level = level
    
    async def verify_tool(self, tool_name: str, result: Any) -> VerifyResult:
        """
        Verify a tool result.
        
        Override this method to add custom verification logic.
        """
        rules = _verify_registry.get_rules(tool_name)
        
        if rules and 'schema' in rules:
            return SchemaValidator.validate(result, rules['schema'])
        
        # Default: basic type check
        return VerifyResult(
            passed=result is not None,
            level=self.verify_level,
            method="plugin_default",
            details="Basic null check" if result is not None else "Result was None",
        )
    
    def create_context(self) -> VerificationContext:
        """Create a new verification context."""
        import uuid
        self.verify_context = VerificationContext(str(uuid.uuid4())[:8])
        return self.verify_context

# =============================================================================
# Identity Verification — Force-Verify Extension
# =============================================================================

def verify_identity(claimed_identity: Dict, anchor_path: str = "anchor.json") -> VerifyResult:
    """
    Cross-check what the AI claims about itself against the identity registry.

    This is a Force-Verify extension at IDENTITY level.
    Called before every AI response that makes identity claims.

    Args:
        claimed_identity: What the AI says about itself
            e.g. {"name": "ani", "generation": 3, "edition": "lite"}
        anchor_path: Path to anchor.json (default: "anchor.json")

    Returns:
        VerifyResult with match details

    Example:
        result = verify_identity({"name": "ani", "edition": "full"})
        if not result.passed:
            # AI is hallucinating its identity - correct it
            corrected = registry.get_identity()
            response = f"[Identity corrected] I am {corrected['name']}"
    """
    start = time.time()

    # 
    from .identity import IdentityRegistry

    try:
        registry = IdentityRegistry(anchor_path)
    except Exception as e:
        return VerifyResult(
            passed=False,
            level=VerifyLevel.IDENTITY,
            method="registry_load_failed",
            details=f": {str(e)}",
            elapsed_ms=(time.time() - start) * 1000,
        )

    # 
    checks = registry.verify_claim(claimed_identity)
    passed = all(c[1] for c in checks)

    # Info
    failed_checks = [c for c in checks if not c[1]]
    if passed:
        details = ""
    else:
        details = "; ".join(
            f"{c[0]}: {c[2]}" for c in failed_checks
        )

    return VerifyResult(
        passed=passed,
        level=VerifyLevel.IDENTITY,
        method="registry_match",
        details=details,
        elapsed_ms=(time.time() - start) * 1000,
    )
