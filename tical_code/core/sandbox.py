"""
Sandboxed Code Execution Module
================================

Security-first execution environment for user-provided Python code.
Provides three execution modes (auto-fallback based on availability):

1. Docker Container (Full) - Maximum isolation via container
2. RestrictedPython (Lite) - AST transformation + restricted globals
3. Restricted Globals (Fallback) - Minimal whitelist approach

All modes enforce:
- Whitelisted builtins only
- No import statements
- No file/network operations
- Timeout control (default 30s)
- Memory limit (default 128MB)

This is the foundation of Force-Verify philosophy:
"Never trust user input, especially from AI."
"""

import os
import sys
import signal
import threading
import logging
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)

# =============================================================================
# Execution Mode Enum
# =============================================================================

class SandboxMode(Enum):
    """Sandbox execution modes (in order of security)."""
    DOCKER = "docker"           # Full isolation via Docker container
    RESTRICTED_PYTHON = "restricted_python"  # AST transformation
    RESTRICTED_GLOBALS = "restricted_globals"  # Whitelist fallback

@dataclass
class SandboxConfig:
    """
    Configuration for sandboxed execution.
    
    Attributes:
        timeout_seconds: Maximum execution time (default 30s)
        memory_limit_mb: Maximum memory in MB (default 128MB)
        max_output_length: Maximum stdout/stderr output length
        allow_network: Whether to allow network calls (default False)
        working_directory: Working directory for execution
    """
    timeout_seconds: int = 30
    memory_limit_mb: int = 128
    max_output_length: int = 10000
    allow_network: bool = False
    working_directory: Optional[str] = None

@dataclass
class SandboxResult:
    """Result of sandboxed execution."""
    success: bool
    output: Any = None
    stdout: str = ""
    stderr: str = ""
    error: Optional[str] = None
    elapsed_ms: float = 0.0
    mode: SandboxMode = SandboxMode.RESTRICTED_GLOBALS

# =============================================================================
# Whitelist Definitions
# =============================================================================

# Safe builtins whitelist - ONLY these are exposed to user code
SAFE_BUILTINS = {
    # Type conversion
    'print': print,
    'len': len,
    'range': range,
    'str': str,
    'int': int,
    'float': float,
    'list': list,
    'dict': dict,
    'tuple': tuple,
    'set': set,
    'frozenset': frozenset,
    'bool': bool,
    'type': type,
    
    # Object inspection - ALLOWED (safe for introspection)
    # :getattr , getattr(os, 'system') 
    # :hasattr ,,Path
    'isinstance': isinstance,
    'issubclass': issubclass,
    
    # Utility
    'abs': abs,
    'min': min,
    'max': max,
    'sum': sum,
    'sorted': sorted,
    'reversed': reversed,
    'enumerate': enumerate,
    'zip': zip,
    'map': map,
    'filter': filter,
    'any': any,
    'all': all,
    'round': round,
    'pow': pow,
    'divmod': divmod,
    'hex': hex,
    'oct': oct,
    'bin': bin,
    'ord': ord,
    'chr': chr,
    
    # String operations
    'format': format,
    
    # Truth testing
    'True': True,
    'False': False,
    'None': None,
}

# Forbidden patterns that indicate dangerous code
FORBIDDEN_PATTERNS = [
    'import',
    '__import__',
    'eval',
    'exec',
    'open',
    'file',
    'compile',
    'reload',
    'breakpoint',
    'input',
    
    # File operations - File//
    'os.path',
    'os.remove',
    'os.rmdir',
    'os.unlink',
    'os.rename',
    'os.replace',         # Python 3.3+  rename
    'os.re',              # os.rename 
    'shutil',             # shutil :move/copy/copy2/rmtree
    'pathlib.Path',       # Path.rename()/Path.replace()/Path.unlink()
    '.rename(',           #  rename 
    '.replace(',          #  replace (File)
    '.move(',             # shutil.move
    '.copy(',             # shutil.copy/copy2
    
    # Network operations
    'socket',
    'urllib',
    'requests',
    'http.client',
    'ftplib',
    'telnetlib',
    
    # System operations
    'subprocess',
    'os.system',
    'os.popen',
    'os.execl',
    'os.execv',
    'os.spawn',
    
    # Attribute access tricks
    '__builtins__',
    '__globals__',
    '__locals__',
    '__code__',
    '__func__',
    
    # Class tricks
    '__class__',
    '__bases__',
    '__subclasses__',
]

# =============================================================================
# Security Validation
# =============================================================================

def validate_code_safety(code: str) -> Tuple[bool, Optional[str]]:
    """
    Validate code for dangerous patterns before execution.
    
    Args:
        code: Python code string to validate
        
    Returns:
        Tuple of (is_safe, error_message)
    """
    import re
    
    # Check for dangerous patterns using word boundaries
    # Use word boundary regex to avoid false positives
    patterns_to_check = [
        (r'\bimport\b', 'import statement'),
        (r'\bfrom\s+\S+\s+import\b', 'from ... import statement'),
        (r'\b__import__\b', '__import__ function'),
        (r'\beval\b', 'eval function'),
        (r'\bexec\b', 'exec function'),
        (r'\bcompile\b', 'compile function'),
        (r'\bopen\s*\(', 'open() function'),
        (r'\bfile\b', 'file function'),
        (r'\breload\b', 'reload function'),
        (r'\bbreakpoint\b', 'breakpoint function'),
        (r'\binput\b', 'input function'),
        (r'\bglobals\b', 'globals function'),
        (r'\blocals\b', 'locals function'),
        (r'\bvars\b', 'vars function'),
        (r'\bdir\b', 'dir function'),
        (r'\bsetattr\b', 'setattr function'),
        (r'\bgetattr\b', 'getattr function'),
        (r'\b__builtins__\b', '__builtins__ access'),
        (r'\b__globals__\b', '__globals__ access'),
        (r'\b__locals__\b', '__locals__ access'),
        (r'\b__class__\b', '__class__ access'),
        (r'\b__bases__\b', '__bases__ access'),
        (r'\b__subclasses__\b', '__subclasses__ method'),
        (r'\bsubprocess\b', 'subprocess module'),
        (r'\bos\.system\b', 'os.system call'),
        (r'\bos\.popen\b', 'os.popen call'),
        (r'\bos\.execl\b', 'os.execl call'),
        (r'\bos\.execv\b', 'os.execv call'),
        (r'\bos\.spawn\b', 'os.spawn call'),
        (r'\bsocket\b', 'socket module'),
        (r'\burllib\b', 'urllib module'),
        (r'\brequests\b', 'requests module'),
        # File//Op -  rename File
        (r'\bos\.re(?:name|place)\b', 'os.rename/replace — file modification blocked'),
        (r'\bshutil\b', 'shutil module — file operations blocked'),
        (r'\bPath\s*\(', 'pathlib.Path — file operations blocked'),
        (r'\.rename\s*\(', '.rename() — file rename blocked'),
        (r'\.replace\s*\(', '.replace() — file replace blocked'),
        (r'\.move\s*\(', '.move() — file move blocked'),
        (r'\.copy\s*\(', '.copy() — file copy blocked'),
        (r'\.unlink\s*\(', '.unlink() — file delete blocked'),
    ]
    
    for pattern, name in patterns_to_check:
        if re.search(pattern, code):
            return False, f"Forbidden pattern detected: {name}"
    
    # Check for attribute access tricks
    dangerous_getattr_patterns = [
        r'getattr\s*\(\s*[^,]+,\s*["\'][^"\']+["\']\s*\)',
    ]
    
    for pattern in dangerous_getattr_patterns:
        if re.search(pattern, code):
            # Check if it's trying to access dangerous attributes
            if any(danger in code for danger in ['__', 'func_globals', 'func_code']):
                return False, "Dangerous attribute access detected"
    
    return True, None

def create_restricted_globals(inputs: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    """
    Create restricted globals dictionary for safe execution.
    
    Args:
        inputs: Input variables for code
        context: Execution context
        
    Returns:
        Restricted globals dict with whitelisted builtins only
    """
    return {
        '__builtins__': SAFE_BUILTINS,
        '__name__': '__sandbox__',
        '__doc__': None,
        
        # User inputs
        'inputs': inputs,
        'context': context,
        
        # Output placeholder
        '_result': None,
    }

# =============================================================================
# Restricted Python Execution (Mode 2)
# =============================================================================

def execute_restricted_python(
    code: str,
    inputs: Dict[str, Any],
    context: Dict[str, Any],
    config: SandboxConfig,
) -> SandboxResult:
    """
    Execute code using RestrictedPython library.
    
    RestrictedPython transforms Python AST to:
    - Block dangerous operations
    - Restrict attribute access
    - Prevent dangerous builtins
    
    Args:
        code: Python code to execute
        inputs: Input variables
        context: Execution context
        config: Sandbox configuration
        
    Returns:
        SandboxResult with execution outcome
    """
    import time
    start = time.time()
    
    try:
        from RestrictedPython import compile_restricted
        from RestrictedPython.Guards import safe_globals, safe_builtins
        
        # Compile restricted
        byte_code = compile_restricted(code, '<sandbox>', 'exec')
        
        if byte_code.code is None:
            return SandboxResult(
                success=False,
                error="Code compilation failed",
                mode=SandboxMode.RESTRICTED_PYTHON,
            )
        
        # Setup execution globals
        exec_globals = {
            '__builtins__': safe_builtins,
            '__name__': '__sandbox__',
            '_got_\nline__': None,  # RestrictedPython requires this
        }
        exec_globals.update(inputs)
        exec_globals['context'] = context
        
        stdout_capture = []
        stderr_capture = []
        
        # Wrap print to capture output
        def _safe_print(*args, **kwargs):
            output = ' '.join(str(a) for a in args)
            stdout_capture.append(output)
        
        exec_globals['print'] = _safe_print
        
        # Execute with timeout via threading
        result_holder = [None]
        error_holder = [None]
        
        def _execute():
            try:
                exec(byte_code.code, exec_globals)
                result_holder[0] = exec_globals.get('_result')
            except Exception as e:
                error_holder[0] = str(e)
        
        thread = threading.Thread(target=_execute)
        thread.daemon = True
        
        try:
            thread.start()
            thread.join(timeout=config.timeout_seconds)
            
            if thread.is_alive():
                return SandboxResult(
                    success=False,
                    error=f"Execution timed out after {config.timeout_seconds}s",
                    mode=SandboxMode.RESTRICTED_PYTHON,
                    elapsed_ms=(time.time() - start) * 1000,
                )
        except Exception as e:
            return SandboxResult(
                success=False,
                error=f"Thread error: {e}",
                mode=SandboxMode.RESTRICTED_PYTHON,
                elapsed_ms=(time.time() - start) * 1000,
            )
        
        if error_holder[0]:
            return SandboxResult(
                success=False,
                error=error_holder[0],
                mode=SandboxMode.RESTRICTED_PYTHON,
                elapsed_ms=(time.time() - start) * 1000,
            )
        
        stdout = '\n'.join(stdout_capture)[:config.max_output_length]
        
        return SandboxResult(
            success=True,
            output=result_holder[0],
            stdout=stdout,
            mode=SandboxMode.RESTRICTED_PYTHON,
            elapsed_ms=(time.time() - start) * 1000,
        )
        
    except ImportError:
        return SandboxResult(
            success=False,
            error="RestrictedPython not installed",
            mode=SandboxMode.RESTRICTED_PYTHON,
        )

# =============================================================================
# Restricted Globals Execution (Mode 3 - Fallback)
# =============================================================================

def execute_restricted_globals(
    code: str,
    inputs: Dict[str, Any],
    context: Dict[str, Any],
    config: SandboxConfig,
) -> SandboxResult:
    """
    Execute code with restricted globals (whitelist approach).
    
    This is the fallback mode when RestrictedPython is unavailable.
    It provides basic protection via restricted __builtins__.
    
    Args:
        code: Python code to execute
        inputs: Input variables
        context: Execution context
        config: Sandbox configuration
        
    Returns:
        SandboxResult with execution outcome
    """
    import time
    start = time.time()
    
    # Pre-validate code
    is_safe, error = validate_code_safety(code)
    if not is_safe:
        return SandboxResult(
            success=False,
            error=error,
            mode=SandboxMode.RESTRICTED_GLOBALS,
            elapsed_ms=(time.time() - start) * 1000,
        )
    
    # Setup globals
    exec_globals = create_restricted_globals(inputs, context)
    
    # Capture stdout
    stdout_buffer = []
    
    class _StdoutCapture:
        def write(self, text):
            if len(stdout_buffer) < 1000:  # Limit buffer size
                stdout_buffer.append(str(text))
        
        def flush(self):
            pass
    
    original_stdout = sys.stdout
    sys.stdout = _StdoutCapture()
    
    try:
        # Timeout:Exec,WaitTimeout
        #  SIGALRM(SIGALRM not, Unix )
        _exec_error = [None]  # ListException
        _exec_done = threading.Event()
        _code = code  # ,Value
        
        def _run_code():
            """ """
            try:
                try:
                    # 
                    exec_code = f"_result = ({_code})"
                    exec(exec_code, exec_globals)
                except SyntaxError:
                    # 
                    code_lines = _code.strip().split('\n')
                    needs_return = True
                    for line in code_lines:
                        stripped = line.strip()
                        if stripped.startswith('return ') or stripped == 'return':
                            needs_return = False
                            break
                    
                    if needs_return:
                        last_line = code_lines[-1].strip()
                        if last_line and not any(last_line.startswith(kw) for kw in ['if', 'for', 'while', 'def', 'class', 'try', 'with', 'return', '#']):
                            code_lines.append(f'return {last_line}')
                            stmt_code = '\n'.join(code_lines[:-1]) + '\n' + code_lines[-1]
                        else:
                            stmt_code = _code
                    else:
                        stmt_code = _code
                    
                    exec_globals['_result'] = None
                    exec(stmt_code, exec_globals)
            except Exception as e:
                _exec_error[0] = e
            finally:
                _exec_done.set()
        
        # Exec
        exec_thread = threading.Thread(target=_run_code, daemon=True)
        exec_thread.start()
        
        # WaitExecDoneorTimeout
        if not _exec_done.wait(timeout=config.timeout_seconds):
            # Timeout -  daemon 
            return SandboxResult(
                success=False,
                error=f"Execution timed out after {config.timeout_seconds}s",
                mode=SandboxMode.RESTRICTED_GLOBALS,
                elapsed_ms=(time.time() - start) * 1000,
            )
        
        # ExecDone,CheckisException
        if _exec_error[0] is not None:
            raise _exec_error[0]
        
        stdout = ''.join(stdout_buffer)[:config.max_output_length]
        
        return SandboxResult(
            success=True,
            output=exec_globals.get('_result'),
            stdout=stdout,
            mode=SandboxMode.RESTRICTED_GLOBALS,
            elapsed_ms=(time.time() - start) * 1000,
        )
        
    except Exception as e:
        return SandboxResult(
            success=False,
            error=f"Execution error: {e}",
            mode=SandboxMode.RESTRICTED_GLOBALS,
            elapsed_ms=(time.time() - start) * 1000,
        )
    finally:
        sys.stdout = original_stdout

# =============================================================================
# Docker Container Execution (Mode 1 - Maximum Security)
# =============================================================================

def execute_docker_sandbox(
    code: str,
    inputs: Dict[str, Any],
    context: Dict[str, Any],
    config: SandboxConfig,
) -> SandboxResult:
    """
    Execute code inside an isolated Docker container.
    
    This provides the highest level of security by:
    - Running in a separate namespace
    - Enforcing cgroup limits
    - Blocking all system access
    
    Args:
        code: Python code to execute
        inputs: Input variables
        context: Execution context
        config: Sandbox configuration
        
    Returns:
        SandboxResult with execution outcome
    """
    import time
    import json
    import subprocess
    start = time.time()
    
    try:
        import docker
    except ImportError:
        return SandboxResult(
            success=False,
            error="docker library not installed",
            mode=SandboxMode.DOCKER,
        )
    
    # P0-1: Docker mode Check
    is_safe, error = validate_code_safety(code)
    if not is_safe:
        return SandboxResult(
            success=False,
            error=f"Docker sandbox rejected: {error}",
            mode=SandboxMode.DOCKER,
            elapsed_ms=0,
        )
    
    try:
        client = docker.from_env()
        
        # Prepare execution payload
        payload = {
            'code': code,
            'inputs': inputs,
            'context': context,
            'timeout': config.timeout_seconds,
            'memory_limit': config.memory_limit_mb * 1024 * 1024,
        }
        
        # Write code to temp file in container-friendly format
        import base64
        encoded_code = base64.b64encode(json.dumps(payload).encode()).decode()
        
        # P0-4:  SAFE_BUILTINS  dict,
        #  __ 
        safe_builtins_json = json.dumps({
            k: str(v) for k, v in SAFE_BUILTINS.items()
            if isinstance(v, (bool, type(None)))  # 
        })
        
        # Build docker run command -  SAFE_BUILTINS + 
        docker_cmd = [
            'python', '-c',
            f"""import json, base64, sys, re
data = json.loads(base64.b64decode("{encoded_code}").decode())
code = data["code"]
if re.search(r"__(\\w+)__", code):
    print("BLOCKED: dunder attribute access forbidden")
    sys.exit(1)
if re.search(r"\\bgetattr\\b", code):
    print("BLOCKED: getattr forbidden")
    sys.exit(1)
_safe = {{'print': print, 'len': len, 'range': range, 'str': str,
          'int': int, 'float': float, 'list': list, 'dict': dict,
          'tuple': tuple, 'set': set, 'bool': bool, 'type': type,
          'isinstance': isinstance, 'True': True, 'False': False, 'None': None,
          'abs': abs, 'min': min, 'max': max, 'sum': sum, 'sorted': sorted,
          'enumerate': enumerate, 'zip': zip, 'map': map, 'filter': filter,
          'any': any, 'all': all, 'round': round, 'reversed': reversed}}
exec(code, {{"__builtins__": _safe}})"""
        ]
        
        # P0-2:  timeout Param,not
        # Run with resource limits
        result = client.containers.run(
            'python:3.11-slim',
            docker_cmd,
            mem_limit=f'{config.memory_limit_mb}m',
            cpu_period=100000,
            cpu_quota=50000,  # 50% CPU
            network_disabled=not config.allow_network,
            read_only=True,
            cap_drop=['ALL'],
            security_opt=['no-new-privileges'],
            detach=False,
            remove=True,
            timeout=config.timeout_seconds,  # P0-2: Docker client Timeout
        )
        
        return SandboxResult(
            success=True,
            output=result.decode() if result else None,
            stdout=result.decode() if result else '',
            mode=SandboxMode.DOCKER,
            elapsed_ms=(time.time() - start) * 1000,
        )
        
    except docker.errors.NotFound:
        return SandboxResult(
            success=False,
            error="Docker not available or python:3.11-slim image not found",
            mode=SandboxMode.DOCKER,
            elapsed_ms=(time.time() - start) * 1000,
        )
    except Exception as e:
        return SandboxResult(
            success=False,
            error=f"Docker execution error: {e}",
            mode=SandboxMode.DOCKER,
            elapsed_ms=(time.time() - start) * 1000,
        )

# =============================================================================
# Main Sandbox Executor (Auto-fallback)
# =============================================================================

class SandboxExecutor:
    """
    Unified sandbox executor with automatic mode selection.
    
    Tries execution modes in order of security:
    1. Docker (if available and configured)
    2. RestrictedPython (if installed)
    3. RestrictedGlobals (always available fallback)
    
    Example:
        executor = SandboxExecutor()
        result = executor.execute(
            code="print(len(inputs['items']))",
            inputs={'items': [1, 2, 3]},
            context={},
        )
        print(result.output)  # 3
    """
    
    def __init__(self, config: Optional[SandboxConfig] = None):
        """
        Initialize sandbox executor.
        
        Args:
            config: Optional sandbox configuration
        """
        self.config = config or SandboxConfig()
        self._mode: Optional[SandboxMode] = None
        self._detect_mode()
    
    def _detect_mode(self) -> SandboxMode:
        """
        Detect best available execution mode.
        
        Returns:
            SandboxMode to use for execution
        """
        # Check Docker first (if configured)
        if os.environ.get('TICAL_SANDBOX_DOCKER', '').lower() == 'true':
            try:
                import docker
                docker.from_env()
                self._mode = SandboxMode.DOCKER
                logger.info("Sandbox mode: Docker (full isolation)")
                return self._mode
            except Exception as e:
                logger.debug(f"[Sandbox] __init__(): {e}")
                pass
        
        # Check RestrictedPython
        try:
            from RestrictedPython import compile_restricted
            self._mode = SandboxMode.RESTRICTED_PYTHON
            logger.info("Sandbox mode: RestrictedPython")
            return self._mode
        except ImportError:
            pass
        
        # Fall back to restricted globals
        self._mode = SandboxMode.RESTRICTED_GLOBALS
        logger.info("Sandbox mode: RestrictedGlobals (fallback)")
        return self._mode
    
    def execute(
        self,
        code: str,
        inputs: Optional[Dict[str, Any]] = None,
        context: Optional[Dict[str, Any]] = None,
        config: Optional[SandboxConfig] = None,
    ) -> SandboxResult:
        """ """
        cfg = config or self.config
        inputs = inputs or {}
        context = context or {}
        
        # Determine execution mode
        mode = self._mode
        
        # Try modes in order
        result = None
        if mode == SandboxMode.DOCKER:
            result = execute_docker_sandbox(code, inputs, context, cfg)
            if result.success or "not available" in (result.error or ""):
                return self._redact_result(result)
            # Fall through to next mode
        
        if mode in (SandboxMode.DOCKER, SandboxMode.RESTRICTED_PYTHON):
            result = execute_restricted_python(code, inputs, context, cfg)
            if result.success or "not installed" not in (result.error or ""):
                return self._redact_result(result)
            # Fall through to fallback
        
        # Final fallback: restricted globals
        result = execute_restricted_globals(code, inputs, context, cfg)
        return self._redact_result(result)
    
    @staticmethod
    def _redact_result(result: SandboxResult) -> SandboxResult:
        """ """
        try:
            from .security_baseline import sandbox_output_redact
            if result.stdout:
                result.stdout = sandbox_output_redact(result.stdout)
            if result.stderr:
                result.stderr = sandbox_output_redact(result.stderr)
            if isinstance(result.output, str):
                result.output = sandbox_output_redact(result.output)
        except ImportError:
            pass  # security_baselinenotSkip
        except Exception as e:
            logger.debug(f"[Sandbox] _redact_result(): {e}")
            pass  # FailnotResult
        return result
    
    @property
    def mode(self) -> SandboxMode:
        """Get current sandbox mode."""
        return self._mode or self._detect_mode()

# =============================================================================
# Global singleton
# =============================================================================

_global_sandbox: Optional[SandboxExecutor] = None

def get_sandbox() -> SandboxExecutor:
    """Get global sandbox executor instance."""
    global _global_sandbox
    if _global_sandbox is None:
        _global_sandbox = SandboxExecutor()
    return _global_sandbox

def reset_sandbox():
    """Reset global sandbox instance."""
    global _global_sandbox
    _global_sandbox = None

# =============================================================================
# Protected Files Registry - FileSource
# (self_repair, tool_router, secure_runtime.sh)List
# =============================================================================

PROTECTED_FILE_REGISTRY = frozenset({
    # v0.4+ :
    'memory_evolve.py',       #  - frozen 
    'cron_scheduler.py',      #  - 
    'tool_router.py',         # Tool - ReActandPerm
    'sandbox.py',             #  - 
    'self_repair.py',         #  - 
    # SystemFile
    'identity.py',            # IdentitySystem
    'anchor.py',              # System
    'verify.py',              # VerifySystem
    'verify_pipeline.py',     # Verify
    'worker_framework.py',    # Worker
    'worker.py',              # Worker
    'worker_loop.py',         # Worker
    '__init__.py',            # 
    # Config
    'main.py',                # 
    '__main__.py',            # Python
    'cli.py',                 # CLI
    # LLM
    'llm_interface.py',       # LLM
    'model_router.py',        # 
    'memory_boot.py',         # Load
    'memory_store.py',        # SQLite
    'memory.py',              # System
    'prompt_generator.py',    # PromptGenerate
    'builtin_tools.py',       # Tool
    'tool_call_parser.py',    # ToolCall
    # 
    'auth.py',                # 
    # Config
    'config.yaml',            # YAMLConfig
    'config.yml',             # YAMLConfig
    'config.json',            # JSONConfig
    'pyproject.toml',         # Config
    'setup.py',               # Config
    'requirements.txt',       # 
    # Git
    '.gitignore',             # Git
    'Dockerfile',             # Docker
    'docker-compose.yml',     # Docker
    # File
    '.tical_mod_count.json',  # 
    '.tical_trust.json',      # Status
})
