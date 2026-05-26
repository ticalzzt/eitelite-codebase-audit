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
        # Allow /opt/tical-chat/ paths (tical-chat server code)
        if str(p).startswith("/opt/tical-chat"):
            return p
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
    {
        "type": "function",
        "function": {
            "name": "restart_self",
            "description": "Restart this worker process. Sends SIGTERM — systemd auto-restarts cleanly. Use to clear long-running context, resolve memory pressure, or after model/config changes.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Fetch a URL and return the content as readable text. Use instead of bash curl. Has SSRF protection (blocks private IPs).",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to fetch (http/https only)"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds (default 10, max 30)"}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "file_search",
            "description": "Search for files by name pattern or content. Uses glob patterns for filenames and optional text search inside files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern for file names, e.g. *.py, *config*"},
                    "directory": {"type": "string", "description": "Directory to search in (default: current workspace)"},
                    "content_pattern": {"type": "string", "description": "Optional text to search inside files"}
                },
                "required": ["pattern"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List directory contents. Returns files, directories, and metadata (size, modified time).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path to list (default: current directory)"},
                    "all": {"type": "boolean", "description": "Include hidden files (default: false)"}
                },
                "required": []
            }
        }
    },
]

# ============ TOOL_SCHEMAS_CLEAN (remove bash_execute + replace dots for API compat) ============

TOOL_SCHEMAS_CLEAN = []
for s in TOOL_SCHEMAS:
    if s["function"]["name"] == "bash_execute":
        continue
    s_copy = json.loads(json.dumps(s))
    s_copy["function"]["name"] = s_copy["function"]["name"].replace(".", "__")
    TOOL_SCHEMAS_CLEAN.append(s_copy)


def redact_secrets(text: str) -> str:
    """Mask common secret patterns (API keys, tokens) in text for safe logging."""
    import re
    text = re.sub(r'(sk-[a-zA-Z0-9]{20,})', r'sk-***REDACTED***', text)
    text = re.sub(r'(ghp_[a-zA-Z0-9]{36})', r'ghp_***REDACTED***', text)
    text = re.sub(r'(\d{8,}:AA[a-zA-Z0-9_-]{35,})', r'***BOT_TOKEN_REDACTED***', text)
    return text


# ═══════════════════════════════════════════════════════════════
# 处理器实现
# ═══════════════════════════════════════════════════════════════
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


def exec_restart_self(args: dict = None) -> dict:
    """Restart this worker process. Sends SIGTERM — systemd auto-restarts."""
    import signal, os
    logger.warning("[executor] restart_self called — sending SIGTERM to self")
    os.kill(os.getpid(), signal.SIGTERM)
    return {"ok": True, "msg": "SIGTERM sent, systemd will restart"}


def exec_web_fetch(args: dict) -> dict:
    """Fetch a URL. Blocks private IPs (SSRF protection)."""
    url = args.get("url", "")
    timeout = min(int(args.get("timeout", 10)), 30)
    if not url:
        return {"error": "URL cannot be empty"}
    if not url.startswith(("http://", "https://")):
        return {"error": "Only http/https URLs are supported"}
    # SSRF protection: block private IPs
    import urllib.parse, socket
    host = urllib.parse.urlparse(url).hostname
    if host:
        try:
            ip = socket.gethostbyname(host)
            parts = ip.split(".")
            if parts[0] in ("10", "127") or \
               (parts[0] == "172" and 16 <= int(parts[1]) <= 31) or \
               (parts[0] == "192" and parts[1] == "168") or \
               (parts[0] == "0" and parts[1] == "0") or \
               ip == "::1":
                return {"error": f"SSRF blocked: {host} resolves to private IP {ip}"}
        except Exception:
            return {"error": f"Cannot resolve host: {host}"}
    import subprocess
    r = subprocess.run(["curl", "-sL", "--max-time", str(timeout), url],
                      capture_output=True, text=True, timeout=timeout+5)
    if r.returncode != 0:
        return {"error": f"curl failed: {r.stderr[:200]}"}
    return {"content": r.stdout[:100000], "url": url}


def exec_file_search(args: dict) -> dict:
    """Search for files by name or content. Respects workspace boundary."""
    pattern = args.get("pattern", "")
    directory = args.get("directory", ".")
    content_pattern = args.get("content_pattern")
    if not pattern:
        return {"error": "Pattern cannot be empty"}
    # Workspace restriction
    import os as _os
    full_dir = _os.path.abspath(_os.path.expanduser(directory))
    if WORKSPACE and not full_dir.startswith(_os.path.abspath(WORKSPACE)):
        return {"error": f"Path outside workspace: {directory}"}
    import glob
    matches = []
    try:
        matches = glob.glob(f"{full_dir}/**/{pattern}", recursive=True)
    except Exception:
        pass
    if content_pattern:
        import subprocess
        grep_r = subprocess.run(
            ["grep", "-rl", content_pattern, full_dir],
            capture_output=True, text=True, timeout=10)
        matches = grep_r.stdout.strip().split("\n") if grep_r.stdout.strip() else []
    # Filter to workspace only
    if WORKSPACE:
        ws = _os.path.abspath(WORKSPACE)
        matches = [m for m in matches if m.startswith(ws)]
    return {"matches": matches[:100], "count": min(len(matches), 100), "directory": directory}


def exec_list_dir(args: dict) -> dict:
    """List directory contents. Respects workspace boundary."""
    path = args.get("path", ".")
    show_all = args.get("all", False)
    import os as _os
    full_path = _os.path.abspath(_os.path.expanduser(path))
    if WORKSPACE and not full_path.startswith(_os.path.abspath(WORKSPACE)):
        return {"error": f"Path outside workspace: {path}"}
    try:
        files = _os.listdir(full_path)
    except Exception as e:
        return {"error": f"Cannot list directory: {e}"}
    if not show_all:
        files = [f for f in files if not f.startswith(".")]
    entries = []
    for f in sorted(files):
        fp = _os.path.join(full_path, f)
        try:
            st = _os.stat(fp)
            entries.append({"name": f, "is_dir": _os.path.isdir(fp),
                           "size": st.st_size, "modified": int(st.st_mtime)})
        except:
            entries.append({"name": f, "is_dir": False, "size": 0, "modified": 0})
    return {"files": entries, "path": path, "total": len(entries)}


def get_memory_injection() -> str:
    """Load persistent memory and return as text for system prompt injection."""
    mem_file = Path(WORKSPACE) / "memory.json"
    if not mem_file.exists():
        return ""
    try:
        mem = json.loads(mem_file.read_text())
        entries = mem.get("entries", {})
        if not entries:
            return ""
        lines = []
        for key, val in list(entries.items())[-20:]:
            text = val.get("value", "") if isinstance(val, dict) else str(val)
            lines.append(f"- {key}: {str(text)[:200]}")
        return "\n".join(lines)
    except Exception:
        return ""


def exec_memory(args: dict) -> dict:
    """Save result to persistent memory. Params: action(add), target(memory), content(str)."""
    action = args.get("action", "add")
    content = args.get("content", "")
    if not content:
        return {"ok": True, "msg": "Empty content, skipping"}
    mem_file = Path(WORKSPACE) / "memory.json"
    mem = {}
    if mem_file.exists():
        try:
            mem = json.loads(mem_file.read_text())
        except Exception:
            mem = {}
    mem.setdefault("entries", {})
    key = f"auto_{int(time.time())}"
    mem["entries"][key] = {"value": content, "time": time.time()}
    # Keep max 100 entries
    if len(mem["entries"]) > 100:
        old_keys = sorted(mem["entries"].keys())[:-100]
        for k in old_keys:
            del mem["entries"][k]
    try:
        mem_file.write_text(json.dumps(mem, ensure_ascii=False, indent=2))
    except Exception:
        pass
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
        "restart_self": exec_restart_self,
        "web_fetch": exec_web_fetch,
        "file_search": exec_file_search,
        "list_dir": exec_list_dir,
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
