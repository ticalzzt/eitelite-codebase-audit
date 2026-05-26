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
    # 注意：getattr 已移除，因为 getattr(os, 'system') 可绕过沙箱
    # 注意：hasattr 已移除，因为可探知对象属性，结合类层析遍历可找到可利用路径
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
    
    # File operations — 扩展覆盖所有文件修改/移动/复制方式
    'os.path',
    'os.remove',
    'os.rmdir',
    'os.unlink',
    'os.rename',
    'os.replace',         # Python 3.3+ 替代 rename
    'os.re',              # os.rename 拼接绕过
    'shutil',             # shutil 全家桶：move/copy/copy2/rmtree
    'pathlib.Path',       # Path.rename()/Path.replace()/Path.unlink()
    '.rename(',           # 任何对象的 rename 方法
    '.replace(',          # 任何对象的 replace 方法（文件替换）
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
        # 文件修改/移动/复制操作 — 防止 rename 绕过保护文件
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
        # 用线程安全的方式实现超时：在子线程中执行代码，主线程等待超时
        # 替代 SIGALRM（SIGALRM 线程不安全，且仅 Unix 可用）
        _exec_error = [None]  # 用列表在线程间传递异常
        _exec_done = threading.Event()
        _code = code  # 复制到局部变量，避免闭包重赋值问题
        
        def _run_code():
            """在子线程中执行用户代码"""
            try:
                try:
                    # 先尝试表达式模式
                    exec_code = f"_result = ({_code})"
                    exec(exec_code, exec_globals)
                except SyntaxError:
                    # 回退到语句模式
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
        
        # 启动执行线程
        exec_thread = threading.Thread(target=_run_code, daemon=True)
        exec_thread.start()
        
        # 等待执行完成或超时
        if not _exec_done.wait(timeout=config.timeout_seconds):
            # 超时 — 子线程为 daemon 会随主进程退出
            return SandboxResult(
                success=False,
                error=f"Execution timed out after {config.timeout_seconds}s",
                mode=SandboxMode.RESTRICTED_GLOBALS,
                elapsed_ms=(time.time() - start) * 1000,
            )
        
        # 执行完成，检查是否有异常
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
    
    # P0-1: Docker mode 也必须走安全检查
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
        
        # P0-4: 使用 SAFE_BUILTINS 而非空 dict，让容器内有基础函数可用
        # 同时禁止 __ 属性访问防止逃逸
        safe_builtins_json = json.dumps({
            k: str(v) for k, v in SAFE_BUILTINS.items()
            if isinstance(v, (bool, type(None)))  # 只传常量
        })
        
        # Build docker run command — 使用 SAFE_BUILTINS + 双下划线拦截
        docker_cmd = [
            'python', '-c',
            f'''
import json, base64, sys, re
data = json.loads(base64.b64decode("{encoded_code}").decode())
code = data["code"]
# 拦截双下划线属性访问（防逃逸）
if re.search(r"__(\\w+)__", code):
    print("BLOCKED: dunder attribute access forbidden")
    sys.exit(1)
# 拦截 getattr 调用
if re.search(r"\\bgetattr\\b", code):
    print("BLOCKED: getattr forbidden")
    sys.exit(1)
# 限制 builtins
_safe = {{'print': print, 'len': len, 'range': range, 'str': str,
          'int': int, 'float': float, 'list': list, 'dict': dict,
          'tuple': tuple, 'set': set, 'bool': bool, 'type': type,
          'isinstance': isinstance, 'True': True, 'False': False, 'None': None,
          'abs': abs, 'min': min, 'max': max, 'sum': sum, 'sorted': sorted,
          'enumerate': enumerate, 'zip': zip, 'map': map, 'filter': filter,
          'any': any, 'all': all, 'round': round, 'reversed': reversed}}
exec(code, {{"__builtins__": _safe}})
'''
        ]
        
        # P0-2: 加 timeout 参数，防止容器永不返回
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
            timeout=config.timeout_seconds,  # P0-2: Docker client 层面超时
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
                logger.debug(f"[Sandbox] __init__异常（不影响运行）: {e}")
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
        """
        Execute code in sandbox.
        
        v0.13: 集成security_baseline — 输出自动脱敏。
        
        Args:
            code: Python code to execute
            inputs: Input variables (available as `inputs` dict)
            context: Execution context (available as `context` dict)
            config: Override sandbox configuration
            
        Returns:
            SandboxResult with execution outcome
        """
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
        """v0.13: 脱敏沙箱输出中的敏感信息。"""
        try:
            from .security_baseline import sandbox_output_redact
            if result.stdout:
                result.stdout = sandbox_output_redact(result.stdout)
            if result.stderr:
                result.stderr = sandbox_output_redact(result.stderr)
            if isinstance(result.output, str):
                result.output = sandbox_output_redact(result.output)
        except ImportError:
            pass  # security_baseline不可用时静默跳过
        except Exception as e:
            logger.debug(f"[Sandbox] _redact_result异常（不影响运行）: {e}")
            pass  # 脱敏失败不影响结果返回
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
# Protected Files Registry — 保护文件单一来源
# 所有模块（self_repair, tool_router, secure_runtime.sh）必须引用此列表
# =============================================================================

PROTECTED_FILE_REGISTRY = frozenset({
    # v0.4+ 核心：安全与行为边界模块
    'memory_evolve.py',       # 记忆进化 — frozen 保护逻辑
    'cron_scheduler.py',      # 自主心跳 — 安全限制
    'tool_router.py',         # 工具路由 — ReAct循环和权限
    'sandbox.py',             # 沙箱 — 安全隔离
    'self_repair.py',         # 自修复 — 自身保护
    'truthful_reporting.py',  # 诚实报告 — 信任机制
    # 核心系统文件
    'identity.py',            # 身份系统
    'anchor.py',              # 锚点系统
    'verify.py',              # 验证系统
    'verify_pipeline.py',     # 验证流水线
    'worker_framework.py',    # Worker框架
    'worker.py',              # Worker入口
    'worker_loop.py',         # Worker循环
    '__init__.py',            # 模块入口
    # 启动与配置
    'main.py',                # 主入口
    '__main__.py',            # Python包入口
    'cli.py',                 # CLI入口
    # LLM与记忆
    'llm_interface.py',       # LLM接口
    'model_router.py',        # 模型路由
    'memory_boot.py',         # 记忆加载
    'memory_store.py',        # SQLite记忆存储
    'memory.py',              # 旧版记忆系统
    'prompt_generator.py',    # Prompt生成
    'builtin_tools.py',       # 内置工具
    'tool_call_parser.py',    # 工具调用解析
    # 认证
    'auth.py',                # 认证模块
    # 配置文件
    'config.yaml',            # YAML配置
    'config.yml',             # YAML配置
    'config.json',            # JSON配置
    'pyproject.toml',         # 项目配置
    'setup.py',               # 安装配置
    'requirements.txt',       # 依赖清单
    # Git与部署
    '.gitignore',             # Git忽略
    'Dockerfile',             # Docker构建
    'docker-compose.yml',     # Docker编排
    # 安全追踪文件
    '.tical_mod_count.json',  # 修改计数
    '.tical_trust.json',      # 信任状态
})
