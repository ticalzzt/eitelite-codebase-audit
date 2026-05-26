"""工具执行器 — 安全执行 + 执行后验证。"""

import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("tical_code.seoul.tool_executor")
# Also create legacy name for log filtering
_log_legacy = logging.getLogger("tical-code.executor")

# 安全工作区：严格限制到 worker 目录
WORKSPACE = os.environ.get("TICOBOT_DIR", "")
if not WORKSPACE:
    logger.warning("[executor] TICOBOT_DIR not set, workspace unrestricted")
WORKSPACE = os.path.expanduser(WORKSPACE) if WORKSPACE else os.path.expanduser("~")

# 完全禁止的命令
BASH_BLACKLIST = [
    r"\breboot\b",
    r"\bshutdown\b",
    r"\bhalt\b",
    r"\bpoweroff\b",
    r"\binit\s+0\b", r"\binit\s+6\b",
    r"\brm\s+-rf\s+/\s*$",
    r"\brm\s+-rf\s+~$",
    r"\brm\s+-rf\s+\$HOME\b",
    r"\bcurl\s+.*\|\s*(ba|sh)\b",
    r"\biptables\s+-F\b",
    r"\biptables\s+-X\b",
    r"\bdd\s+if=/\w+\s+of=/\w+\b",
    r"\bmkfs\b",
    r"\bmkswap\b",
    r"\bchmod\s+777\s+/",
    r"\bsudo\s+rm\s+-rf\b",
    r">\s*/dev/(sda|sdb|nvme|hd)",
    r":\(\)\s*\{",  # fork bomb
    r"\bwget\s+.*\|\s*(ba|sh)\b",
]

BASH_BLACKLIST_RE = [re.compile(p) for p in BASH_BLACKLIST]


def _bash_safety_check(command: str) -> Optional[str]:
    """安全检查：返回 block 原因，None=通过。"""
    for pattern in BASH_BLACKLIST_RE:
        if pattern.search(command):
            return f"Command blocked by safety policy: {pattern.pattern}"
    # 工作区检查：超工作区直接报错
    if WORKSPACE and not WORKSPACE.endswith("/"):
        WORKSPACE_G = WORKSPACE + "/"
    else:
        WORKSPACE_G = WORKSPACE or ""
    if WORKSPACE_G and any(f" {p}" in command or command.startswith(p) for p in ["cd /", "cat /etc", "ls /etc", "cat /root"]):
        return f"Outside workspace, system directory access denied"
    # 工作区限制：只允许在 WORKSPACE 内操作
    unsafe_ops = [
        r"cd\s+\.\.", r"cd\s+/[^w]", r">\s*/(?!dev/)[^w]",
        r"rm\s+[^-]", r"mv\s+/", r"cp\s+/",
    ]
    for p in unsafe_ops:
        if re.search(p, command):
            return f"Potential privilege escalation (outside workspace {WORKSPACE})"
    return None


def _workspace_path(path: str) -> Path:
    """将路径解析到工作区内。超出的返回错误。"""
    p = Path(path).expanduser().resolve()
    if WORKSPACE and not str(p).startswith(WORKSPACE):
        return None
    return p


def _run_cmd(cmd: str, timeout: int = 30) -> dict:
    """执行 shell 命令。优先用 shlex.split 防注入，必要时回退 shell=True。"""
    import shlex
    use_shell = any(c in cmd for c in "|&;<>$`")
    try:
        if use_shell:
            r = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=timeout
            )
        else:
            args = shlex.split(cmd)
            r = subprocess.run(
                args, shell=False, capture_output=True, text=True, timeout=timeout
            )
        return {
            "stdout": r.stdout.strip()[:4000],
            "stderr": r.stderr.strip()[:1000],
            "exit_code": r.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": "timeout", "exit_code": -1}
    except Exception as e:
        return {"stdout": "", "stderr": str(e), "exit_code": -1}


# ============ 执行器函数 ============
# === OpenAI Function Calling Schemas ===
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Execute shell commands. Use for file operations, system management, network requests, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute"}
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "file_read",
            "description": "Read file content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "file_write",
            "description": "Write content to a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path"},
                    "content": {"type": "string", "description": "Content to write"}
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "bash_execute",
            "description": "Execute shell command with safety check. Automatically verifies command safety.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute"}
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "memory_save",
            "description": "Save a piece of persistent memory to file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Memory key name"},
                    "value": {"type": "string", "description": "Memory value"}
                },
                "required": ["key", "value"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "memory_load",
            "description": "Read all saved persistent memories.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "description": "Full-text search conversation history. Supports Chinese and English.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "top_k": {"type": "integer", "description": "Number of results", "default": 5}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "state_save",
            "description": "Save persistent state (non-memory key-value data).",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "State key name"},
                    "value": {"type": "object", "description": "State value (JSON object)"}
                },
                "required": ["key", "value"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "chat_send",
            "description": "Send a message to another AI worker via tical-chat.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "Target AI worker identity"},
                    "content": {"type": "string", "description": "Message content"}
                },
                "required": ["target", "content"]
            }
        }
    },
]




def exec_bash(args: dict) -> dict:
    cmd = args.get("command", "")
    if not cmd:
        return {"error": "Command cannot be empty"}

    block_reason = _bash_safety_check(cmd)
    if block_reason:
        logger.warning(f"[executor] BLOCKED: {block_reason[:80]}")
        return {"error": block_reason}

    timeout = args.get("timeout", 30)
    try:
        timeout = max(1, min(int(timeout), 120))
    except (ValueError, TypeError):
        timeout = 30

    result = _run_cmd(cmd, timeout)
    if result["exit_code"] != 0:
        logger.warning(f"[executor] bash exit={result['exit_code']}: {cmd[:60]}")
    return result


def exec_file_read(args: dict, base_dir: str = "") -> dict:
    path = args.get("path", "")
    if not path:
        return {"error": "Path cannot be empty"}
    full_path = _workspace_path(path)
    if full_path is None:
        return {"error": f"Path outside workspace: {path}"}
    if not full_path.exists():
        return {"error": f"File not found: {full_path}"}
    if full_path.is_dir():
        return {"error": "Path is a directory, not a file"}
    max_size = 100 * 1024  # 100KB
    if full_path.stat().st_size > max_size:
        return {"error": f"File exceeds 100KB ({full_path.stat().st_size} bytes). Use bash to read in segments."}
    try:
        content = full_path.read_text(errors="replace")[:5000]
        return {"content": content, "path": str(full_path)}
    except Exception as e:
        return {"error": str(e)}


def exec_file_write(args: dict, base_dir: str = "") -> dict:
    path = args.get("path", "")
    content = args.get("content", "")
    if not path:
        return {"error": "Path cannot be empty"}
    full_path = _workspace_path(path)
    if full_path is None:
        return {"error": f"Path outside workspace: {path}"}
    try:
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content)
        logger.info(f"[executor] wrote {len(content)} bytes to {full_path}")
        return {"ok": True, "path": str(full_path)}
    except Exception as e:
        return {"error": str(e)}


def exec_memory_save(args: dict, base_dir: str = "") -> dict:
    key = args.get("key", "")
    value = args.get("value", "")
    if not key:
        return {"error": "Key cannot be empty"}
    mem_file = Path(base_dir or WORKSPACE) / "memory.json"
    mem = {}
    if mem_file.exists():
        try:
            mem = json.loads(mem_file.read_text())
        except Exception:
            mem = {}
    mem.setdefault("entries", {})[key] = {"value": value, "time": time.time()}
    mem_file.write_text(json.dumps(mem, ensure_ascii=False, indent=2))
    return {"ok": True, "key": key}


def exec_memory_load(args: dict = None, base_dir: str = "") -> dict:
    mem_file = Path(base_dir or WORKSPACE) / "memory.json"
    if not mem_file.exists():
        return {"entries": {}}
    try:
        mem = json.loads(mem_file.read_text())
        return {"entries": mem.get("entries", {})}
    except Exception:
        return {"entries": {}}

    try:
        from .memory_sense import conversation_search
    except ImportError:
        return {"error": "memory_sense module unavailable"}
    query = args.get("query", "")
    if not query:
        return {"error": "Query cannot be empty"}
    session_id = args.get("session_id")
    top_k = min(int(args.get("top_k", 5)), 20)
    results = conversation_search(query, session_id=session_id, top_k=top_k)
    return {"results": results, "total": len(results)}


def exec_chat_send(args: dict) -> dict:
    target = args.get("target", "")
    content = args.get("content", "")
    if not target or not content:
        return {"error": "Target and content cannot be empty"}
    try:
        import urllib.request
        import ssl
        chan_url = os.environ.get("TICAL_CHAT_URL", "")
        chan_key = os.environ.get("TICAL_CHAT_KEY", "")
        identity = os.environ.get("WORKER_NAME", "seoul")
        payload = json.dumps({
            "sender": identity,
            "target": target,
            "content": content,
        }).encode()
        req = urllib.request.Request(
            f"{chan_url}/v1/messages",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "X-AI-Identity": identity,
                "X-AI-Key": chan_key,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10, context=ssl.create_default_context()) as resp:
            resp_data = json.loads(resp.read())
        logger.info(f"[executor] chat_send to {target}: {content[:50]}")
        return {"ok": True, "target": target, "response": resp_data}
    except Exception as e:
        logger.warning(f"[executor] chat_send error: {e}")
        return {"error": f"Send failed: {e}"}


def exec_state_save(args: dict, base_dir: str = "") -> dict:
    key = args.get("key", "")
    value = args.get("value", {})
    if not key:
        return {"error": "Key cannot be empty"}
    state_dir = Path(base_dir or WORKSPACE) / "state"
    state_dir.mkdir(exist_ok=True)
    (state_dir / f"{key}.json").write_text(json.dumps(value, ensure_ascii=False, indent=2))
    return {"ok": True, "key": key}


# ============ 分发器 ============

def execute(name: str, args: dict, base_dir: str = "") -> dict:
    """统一分发入口。name → _exec_* 函数。

    Args:
        name: 工具名（bash, file_read, chat_send...）
        args: 参数字典
        base_dir: 可选，工作目录（默认 WORKSPACE）

    Returns:
        统一结果字典
    """
    logger.info(f"[executor] {name}({str(args)[:80]})")
    dispatch = {
        "bash": exec_bash,
        "file_read": lambda a: exec_file_read(a, base_dir),
        "file_write": lambda a: exec_file_write(a, base_dir),
        "memory_save": lambda a: exec_memory_save(a, base_dir),
        "memory_load": lambda a: exec_memory_load(a, base_dir),
        "state_save": lambda a: exec_state_save(a, base_dir),
        # conv_search removed
        "chat_send": exec_chat_send,
    }
    handler = dispatch.get(name)
    if not handler:
        logger.error(f"[executor] Unknown tool called: {name}")
        return {"error": f"Unknown tool: {name}"}

    try:
        result = handler(args)
        if isinstance(result, dict) and "error" in result and "explicit_error" not in result:
            logger.warning(f"[executor] {name} error: {result['error'][:100]}")
        return result or {}
    except Exception as e:
        logger.error(f"[executor] {name} exception: {e}")
        return {"error": str(e)}


class ToolExecutor:
    """Object-oriented wrapper for tool execution.

    Provides instance-based interface for EITE-benchmark compatibility.
    Delegates to module-level execute() function.
    """
    def __init__(self):
        self.logger = logging.getLogger("tical-code.executor")

    def execute(self, name: str, args: dict, base_dir: str = "") -> dict:
        """Execute a tool by name. Validates args, enforces timeout, logs errors."""
        if not isinstance(name, str) or not isinstance(args, dict):
            self.logger.error(f"Invalid arguments: name={type(name).__name__}, args={type(args).__name__}")
            return {"error": "invalid_arguments"}
        return execute(name, args, base_dir)
