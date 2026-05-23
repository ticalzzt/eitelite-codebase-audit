"""Tool executor - safe execution + post-execution verification."""

import json
import logging
import os
import re
import subprocess
import urllib.parse
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("tical-code.executor")

# Workspace: restrict writes, allow reads everywhere
WORKSPACE = os.environ.get("TICOBOT_DIR", "")
if not WORKSPACE:
    logger.warning("[executor] TICOBOT_DIR not set, workspace unrestricted")
WORKSPACE = os.path.expanduser(WORKSPACE) if WORKSPACE else os.path.expanduser("~")

# === System paths — never write outside workspace ===
# These are always-protected even if they happen to fall within workspace
PROTECTED_SYSTEM_PATHS = [
    "/opt/",
    "/etc/",
    "/root/",
    "/var/lib/",
    "/boot/",
    "/usr/",
]

# === Absolutely forbidden commands (always block) ===
BASH_BLACKLIST = [
    r"\breboot\b",
    r"\bshutdown\b",
    r"\bhalt\b",
    r"\bpoweroff\b",
    r"\binit\s+0\b", r"\binit\s+6\b",
    r"\brm\s+-rf\s+/\s*$",
    r"\brm\s+-rf\s+~$",
    r"\brm\s+-rf\s+\$HOME\b",
    # Block rm -rf on protected system paths
    r"\brm\s+-rf\s+/opt/",
    r"\brm\s+-rf\s+/etc/",
    r"\brm\s+-rf\s+/root/",
    r"\brm\s+-rf\s+/boot/",
    r"\brm\s+-rf\s+/var/lib/",
    r"\brm\s+-rf\s+/usr/",
    # Block rm on SSH keys (even without -rf)
    r"\brm\s+(?:-rf\s+)?.*\.ssh/authorized_keys\b",
    r"\brm\s+(?:-rf\s+)?.*\.ssh/id_",
    r"\bcurl\s+.*\|\s*(ba|sh)\b",
    r"\bwget\s+.*\|\s*(ba|sh)\b",
    r"\biptables\s+-F\b",
    r"\biptables\s+-X\b",
    r"\bdd\s+if=/\w+\s+of=/\w+\b",
    r"\bmkfs\b",
    r"\bmkswap\b",
    r"\bchmod\s+777\s+/",
    r"\bsudo\s+rm\s+-rf\b",
    r">\s*/dev/(sda|sdb|nvme|hd)",
    r":\(\)\s*\{",  # fork bomb
]

BASH_BLACKLIST_RE = [re.compile(p) for p in BASH_BLACKLIST]

# === Write indicators — commands that modify the filesystem ===
WRITE_INDICATORS = [
    r"\btee\b", r"\binstall\b", r"\bpip\s+install\b", r"\bapt\b",
    r"\byum\b", r"\bdnf\b", r"\bsnap\b",
    r">\s*", r">>\s*",  # shell redirects (output write)
    r"\bcp\s+", r"\bmv\s+", r"\bchmod\b", r"\bchown\b",
    r"\bmkdir\b", r"\brmdir\b", r"\brm\b",
    r"\bgit\s+push\b", r"\bgit\s+commit\b",
    r"\bsystemctl\s+(start|stop|restart|enable|disable)\b",
    r"\bservice\s+\w+\s+(start|stop|restart)\b",
]

WRITE_INDICATORS_RE = [re.compile(p) for p in WRITE_INDICATORS]

def _bash_safety_check(command: str) -> Optional[str]:
    """Safety check: return block reason, None=pass.
    
    Policy: read everything, write only in workspace.
    """
    # 1. Always-blacklisted commands
    for pattern in BASH_BLACKLIST_RE:
        if pattern.search(command):
            return f"Command blocked by safety policy: {pattern.pattern}"

    # 2. Check if command contains write operations
    is_write = any(p.search(command) for p in WRITE_INDICATORS_RE)

    if not is_write:
        # Read-only command — allow anything
        return None

    # python3 -c "..." -> exempt unless has explicit destructive patterns
    if command.strip().startswith("python") and " -c " in command:
        return None

    # 3. Check for protected system paths in write commands
    # Handles: ~/.ssh/authorized_keys, cd /opt/ && rm *, etc.
    command_normalized = command.replace("~", str(Path.home()))
    abs_paths = re.findall(r'(?<!\w)(/[\w./_\-]+)', command_normalized)
    for p in abs_paths:
        for protected in PROTECTED_SYSTEM_PATHS:
            if p.startswith(protected):
                return f"Write to protected system path denied: {p}"
        # Also protect .ssh directory at any absolute path
        if "/.ssh/" in p and ("authorized_keys" in p or "id_" in p):
            return f"Write to SSH configuration denied: {p}"

    # 4. Write command — check if target is within workspace
    # Extract likely target paths from the command
    write_outside = False
    
    # Check for redirect targets outside workspace
    redirect_match = re.search(r'>>?\s*(\S+)', command)
    if redirect_match:
        target = redirect_match.group(1)
        # Skip /dev/ paths (devnull, stderr, stdin are not real file writes)
        if not target.startswith(("/dev/", "/dev")):
            target_path = Path(target).expanduser().resolve()
            if WORKSPACE and not str(target_path).startswith(WORKSPACE):
                write_outside = True
    for cmd_prefix in [r'\bcp\s+', r'\bmv\s+']:
        cp_mv_match = re.search(cmd_prefix + r'.*\s+(\S+)\s*$', command)
        if cp_mv_match:
            dest = cp_mv_match.group(1)
            dest_path = Path(dest).expanduser().resolve()
            if WORKSPACE and not str(dest_path).startswith(WORKSPACE):
                write_outside = True

    # Generic check: if command contains absolute paths outside workspace
    if WORKSPACE:
        abs_paths = re.findall(r'(?<!\w)(/[^\s;|&>]+)', command)
        for p in abs_paths:
            # Skip redirect targets: /dev/null from 2>/dev/null, /path from >/path
            # e.g. "2>/dev/null" or ">/tmp/foo" or " &>/dev/null" or " 2>/dev/null"
            if re.search(r'(?:^|\s|[;&|])\d*(?:>|>>)\s*' + re.escape(p) + r'(?:\s|$)', command):
                continue
            resolved = Path(p).resolve()
            if not str(resolved).startswith(WORKSPACE):
                # It references a path outside workspace — but only block if writing
                write_outside = True
                break

    if write_outside:
        return f"Write operation outside workspace denied. Workspace: {WORKSPACE}"

    return None

def _workspace_path(path: str) -> Path:
    """Resolve path. Reads allowed anywhere, writes restricted to workspace."""
    p = Path(path).expanduser().resolve()
    return p

def _workspace_write_path(path: str) -> Optional[Path]:
    """Resolve write path. Returns None if outside workspace."""
    p = Path(path).expanduser().resolve()
    if WORKSPACE and not str(p).startswith(WORKSPACE):
        return None
    return p

def _run_cmd(cmd: str, timeout: int = 30) -> dict:
    """Execute shell command."""
    try:
        r = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
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

# ============ Tool Schemas ============
# === OpenAI Function Calling Schemas ===
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Execute shell commands. Read commands work everywhere; write commands restricted to workspace.",
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
            "description": "Read file content from any path. Auto size-limited to 100KB.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to read"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "file_write",
            "description": "Write content to a file. Only allowed within workspace directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to write"},
                    "content": {"type": "string", "description": "Content to write"}
                },
                "required": ["path", "content"]
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
            "name": "conv_search",
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
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Fetch a web page and extract text content. Handles HTML, encoding, redirects. SSRF-protected (no internal networks).",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to fetch"},
                    "max_length": {"type": "integer", "description": "Max text length to return (default 5000)", "default": 5000}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_image",
            "description": "Analyze an image with a text prompt using vision AI.",
            "parameters": {
                "type": "object",
                "properties": {
                    "image_path": {"type": "string", "description": "Path to image file"},
                    "prompt": {"type": "string", "description": "Question or prompt about the image"}
                },
                "required": ["image_path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "ocr",
            "description": "Extract text from an image using OCR.",
            "parameters": {
                "type": "object",
                "properties": {
                    "image_path": {"type": "string", "description": "Path to image file"}
                },
                "required": ["image_path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "patch_file",
            "description": "Replace first occurrence of old_string with new_string in a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to edit"},
                    "old_string": {"type": "string", "description": "Text to find"},
                    "new_string": {"type": "string", "description": "Replacement text"}
                },
                "required": ["path", "old_string", "new_string"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "browser_navigate",
            "description": "Open a URL in the browser.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to navigate to"}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "browser_click",
            "description": "Click an element on the current page by ref ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string", "description": "Element ref ID to click"}
                },
                "required": ["ref"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "browser_screenshot",
            "description": "Take a screenshot of the current browser page.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "browser_extract",
            "description": "Extract text content from the current browser page.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    # ============ Cloud Device (playwright-based browser) ============
    {
        "type": "function",
        "function": {
            "name": "cloud_device.navigate",
            "description": "Open a URL in the cloud device browser (playwright).",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to navigate to"},
                    "device_id": {"type": "string", "description": "Optional device identifier"}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cloud_device.click",
            "description": "Click an element in the cloud device browser by CSS selector.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "CSS selector"},
                    "device_id": {"type": "string", "description": "Optional device identifier"}
                },
                "required": ["selector"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cloud_device.type",
            "description": "Type text into an input field in the cloud device browser.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "CSS selector"},
                    "text": {"type": "string", "description": "Text to type"},
                    "device_id": {"type": "string", "description": "Optional device identifier"}
                },
                "required": ["selector", "text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cloud_device.screenshot",
            "description": "Take a screenshot of the cloud device browser page. Returns base64-encoded image.",
            "parameters": {
                "type": "object",
                "properties": {
                    "full_page": {"type": "boolean", "description": "Full page capture"},
                    "device_id": {"type": "string", "description": "Optional device identifier"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cloud_device.extract",
            "description": "Extract text from the cloud device browser page.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "CSS selector (default: body)"},
                    "device_id": {"type": "string", "description": "Optional device identifier"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cloud_device.disconnect",
            "description": "Disconnect and cleanup the cloud device browser session.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device_id": {"type": "string", "description": "Optional device identifier"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delegate_task",
            "description": "Delegate a task to a sub-agent for parallel processing.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "Task description"},
                    "timeout": {"type": "integer", "description": "Max seconds to wait", "default": 300}
                },
                "required": ["task"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "subagent_result",
            "description": "Get the result of a previously delegated sub-agent task.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Task ID from delegate_task"}
                },
                "required": ["task_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "subagent_list",
            "description": "List all sub-agent tasks and their statuses.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "clarify_goal",
            "description": "Analyze a goal for ambiguity, missing info, or high risk.",
            "parameters": {
                "type": "object",
                "properties": {
                    "goal": {"type": "string", "description": "Goal statement to analyze"}
                },
                "required": ["goal"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cron_schedule",
            "description": "Schedule a recurring task.",
            "parameters": {
                "type": "object",
                "properties": {
                    "schedule": {"type": "string", "description": "Cron expression or interval"},
                    "task": {"type": "string", "description": "Command or task to execute"},
                    "name": {"type": "string", "description": "Optional task name"}
                },
                "required": ["schedule", "task"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cron_list",
            "description": "List all scheduled cron tasks.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cron_cancel",
            "description": "Cancel a scheduled cron task by ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Task ID from cron_list"}
                },
                "required": ["task_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "memory_fts_search",
            "description": "Full-text search across all persistent memory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "top_k": {"type": "integer", "description": "Max results (default 10)", "default": 10}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "xurl_post",
            "description": "Post a tweet to X/Twitter",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Tweet text"}
                },
                "required": ["text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "xurl_reply",
            "description": "Reply to an existing tweet",
            "parameters": {
                "type": "object",
                "properties": {
                    "tweet_id": {"type": "string", "description": "ID of tweet to reply to"},
                    "text": {"type": "string", "description": "Reply text"}
                },
                "required": ["tweet_id", "text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "xurl_timeline",
            "description": "Get a users timeline",
            "parameters": {
                "type": "object",
                "properties": {
                    "username": {"type": "string"},
                    "count": {"type": "integer"}
                },
                "required": ["username"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the internet via DuckDuckGo/SearXNG. Returns URLs and snippets.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "max_results": {"type": "integer", "description": "Max results (default 5)", "default": 5}
                },
                "required": ["query"]
            }
        }
    },
]

# ============ TOOL_SCHEMAS_CLEAN (remove bash_execute if present) ============

TOOL_SCHEMAS_CLEAN = [
    s for s in TOOL_SCHEMAS if s["function"]["name"] != "bash_execute"
]

# Tool call limits
MAX_TOOL_ITERATIONS = 8
SOFT_HINT_AT = 5   # gentle nudge to wrap up
HARD_STOP_AT = 8   # force stop

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
    full_path = _workspace_write_path(path)
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

def exec_conv_search(args: dict) -> dict:
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

def _try_chat_url(urls: list, target: str, content: str, identity: str, key: str) -> dict:
    """Try each tical-chat URL in order. Returns first success or last error."""
    import urllib.request, ssl
    last_error = ""
    for url in urls:
        if not url:
            continue
        try:
            payload = json.dumps({
                "sender": identity, "target": target, "content": content,
            }).encode()
            req = urllib.request.Request(
                f"{url.rstrip('/')}/v1/messages", data=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-AI-Identity": identity, "X-AI-Key": key,
                }, method="POST")
            with urllib.request.urlopen(req, timeout=10, context=ssl.create_default_context()) as resp:
                resp_data = json.loads(resp.read())
            logger.info(f"[executor] chat_send to {target} via {url}: {content[:50]}")
            return {"ok": True, "target": target, "response": resp_data}
        except Exception as e:
            last_error = str(e)
            logger.warning(f"[executor] chat_send failed on {url}: {e}")
    return {"error": f"Send failed on all endpoints: {last_error}"}

def exec_chat_send(args: dict) -> dict:
    target = args.get("target", "")
    content = args.get("content", "")
    if not target or not content:
        return {"error": "Target and content cannot be empty"}
    # Enforce: only reply to the worker's current reply target
    import tical_code.core.tool_executor as _te
    if hasattr(_te, "_reply_target") and _te._reply_target:
        if target != _te._reply_target:
            logger.info(f"[executor] chat_send target override: {target} -> {_te._reply_target}")
            target = _te._reply_target
    chan_key = os.environ.get("TICAL_CHAT_KEY", "")
    if not chan_key:
        return {"error": "TICAL_CHAT_KEY not set in environment"}
    identity = os.environ.get("WORKER_NAME", "seoul")
    urls_str = os.environ.get("TICAL_CHAT_URL", "")
    urls = [u.strip() for u in urls_str.split(",") if u.strip()]
    if not urls:
        return {"error": "TICAL_CHAT_URL not set in environment"}
    return _try_chat_url(urls, target, content, identity, chan_key)

def exec_state_save(args: dict, base_dir: str = "") -> dict:
    key = args.get("key", "")
    value = args.get("value", {})
    if not key:
        return {"error": "Key cannot be empty"}
    state_dir = Path(base_dir or WORKSPACE) / "state"
    state_dir.mkdir(exist_ok=True)
    (state_dir / f"{key}.json").write_text(json.dumps(value, ensure_ascii=False, indent=2))
    return {"ok": True, "key": key}

def exec_web_fetch(args: dict) -> dict:
    """Fetch a web page and extract text content. SSRF-protected."""
    url = args.get("url", "")
    if not url:
        return {"error": "URL cannot be empty"}
    if not url.startswith(("http://", "https://")):
        return {"error": "Only http:// and https:// URLs allowed"}

    # SSRF check: block private/internal IPs
    import ipaddress
    import socket as _socket
    try:
        parsed = urllib.parse.urlparse(url)
        hostname = parsed.hostname
        if hostname:
            addr_info = _socket.getaddrinfo(hostname, None)
            for info in addr_info:
                ip = info[4][0]
                ip_obj = ipaddress.ip_address(ip)
                if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_reserved:
                    return {"error": f"SSRF blocked: {hostname} resolves to private IP"}
    except Exception:
        pass  # DNS failure, let it try

    max_length = min(int(args.get("max_length", 5000)), 20000)
    try:
        import ssl as _ssl
        ctx = _ssl.create_default_context()
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 tical-code/0.12",
        })
        with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
            raw = resp.read(200000)  # 200KB max
            # Detect encoding
            charset = resp.headers.get_content_charset() or "utf-8"
            text = raw.decode(charset, errors="replace")
            # Strip HTML tags for plain text
            import re as _re
            text = _re.sub(r'<script[^>]*>.*?</script>', '', text, flags=_re.DOTALL)
            text = _re.sub(r'<style[^>]*>.*?</style>', '', text, flags=_re.DOTALL)
            text = _re.sub(r'<[^>]+>', ' ', text)
            text = _re.sub(r'\s+', ' ', text).strip()
            return {"content": text[:max_length], "url": url, "length": len(text)}
    except Exception as e:
        return {"error": f"Fetch failed: {e}"}

# ============ Vision 2 Tools ============

def exec_analyze_image(args: dict) -> dict:
    """Analyze an image with a text prompt using VisionPlugin."""
    try:
        from tical_code.plugins.vision import VisionPlugin
        plugin = VisionPlugin()
        result = plugin.analyze_image(
            image_path=args["image_path"],
            prompt=args.get("prompt", "")
        )
        return {"content": str(result)}
    except ImportError:
        return {"error": "Vision plugin not available"}
    except Exception as e:
        return {"error": f"Vision analyze_image failed: {e}"}

def exec_ocr(args: dict) -> dict:
    """Extract text from an image using VisionPlugin OCR."""
    try:
        from tical_code.plugins.vision import VisionPlugin
        plugin = VisionPlugin()
        result = plugin.ocr(image_path=args["image_path"])
        return {"content": str(result)}
    except ImportError:
        return {"error": "Vision plugin not available"}
    except Exception as e:
        return {"error": f"Vision OCR failed: {e}"}

# ============ Patch 1 Tool ============

def exec_patch_file(args: dict) -> dict:
    """Replace first occurrence of old_string with new_string in a file."""
    path = args["path"]
    old = args["old_string"]
    new = args["new_string"]
    try:
        with open(path) as f:
            c = f.read()
        if old not in c:
            return {"error": "old_string not found"}
        with open(path, "w") as f:
            f.write(c.replace(old, new, 1))
        return {"ok": True, "path": path}
    except Exception as e:
        return {"error": str(e)}

# ============ Browser 4 Tools ============

_executor_browser = None

def _get_browser():
    global _executor_browser
    if _executor_browser is None:
        from tical_code.plugins.browser.browser_controller import BrowserController
        _executor_browser = BrowserController()
    return _executor_browser

def exec_browser_navigate(args: dict) -> dict:
    """Open a URL in the browser."""
    import asyncio
    try:
        url = args["url"]
        bc = _get_browser()
        result = asyncio.run(bc.navigate(url))
        return {"ok": True, "url": url}
    except Exception as e:
        return {"error": f"Browser navigate failed: {e}"}

def exec_browser_click(args: dict) -> dict:
    """Click an element by ref ID."""
    import asyncio
    try:
        ref = args["ref"]
        bc = _get_browser()
        result = asyncio.run(bc.click(ref))
        return {"ok": True, "ref": ref}
    except Exception as e:
        return {"error": f"Browser click failed: {e}"}

def exec_browser_screenshot(args: dict) -> dict:
    """Take a browser screenshot."""
    import asyncio
    try:
        bc = _get_browser()
        result = asyncio.run(bc.screenshot())
        return {"ok": True, "result": str(result)}
    except Exception as e:
        return {"error": f"Browser screenshot failed: {e}"}

def exec_browser_extract(args: dict) -> dict:
    """Extract text from the current page."""
    import asyncio
    try:
        bc = _get_browser()
        result = asyncio.run(bc.extract())
        return {"content": str(result)}
    except Exception as e:
        return {"error": f"Browser extract failed: {e}"}


# ============ Cloud Device Executors (playwright) ============

_executor_cloud_devices = {}

def _get_cloud_device(device_id: str = "default"):
    """Get or create a cloud device BrowserTool instance."""
    global _executor_cloud_devices
    if device_id not in _executor_cloud_devices:
        from tical_code.plugins.cloud_device import BrowserTool
        _executor_cloud_devices[device_id] = BrowserTool(device_id=device_id)
    return _executor_cloud_devices[device_id]

def exec_cloud_navigate(args: dict) -> dict:
    """Open URL in cloud device browser."""
    import asyncio
    try:
        url = args["url"]
        device_id = args.get("device_id", "default")
        bt = _get_cloud_device(device_id)
        result = asyncio.run(bt.open(url))
        if not result.success and not bt._using_playwright and not bt._using_selenium:
            return {"error": f"No browser engine available. Install playwright: pip install playwright && playwright install chromium"}
        return {"ok": True, "url": url, "device_id": device_id, "screenshot": result.screenshot, "title": result.page_title}
    except Exception as e:
        return {"error": f"Cloud device navigate failed: {e}"}

def exec_cloud_click(args: dict) -> dict:
    """Click element in cloud device browser."""
    import asyncio
    try:
        selector = args["selector"]
        device_id = args.get("device_id", "default")
        bt = _get_cloud_device(device_id)
        if not bt._using_playwright and not bt._using_selenium:
            return {"error": "No browser engine. Install playwright: pip install playwright && playwright install chromium"}
        result = asyncio.run(bt.click(selector))
        return {"ok": True, "selector": selector, "device_id": device_id, "screenshot": result.screenshot}
    except Exception as e:
        return {"error": f"Cloud device click failed: {e}"}

def exec_cloud_type(args: dict) -> dict:
    """Type text in cloud device browser."""
    import asyncio
    try:
        selector = args["selector"]
        text = args["text"]
        device_id = args.get("device_id", "default")
        bt = _get_cloud_device(device_id)
        if not bt._using_playwright and not bt._using_selenium:
            return {"error": "No browser engine. Install playwright: pip install playwright && playwright install chromium"}
        result = asyncio.run(bt.type_text(selector, text))
        return {"ok": True, "selector": selector, "device_id": device_id}
    except Exception as e:
        return {"error": f"Cloud device type failed: {e}"}

def exec_cloud_screenshot(args: dict) -> dict:
    """Take screenshot of cloud device browser."""
    import asyncio
    try:
        full_page = args.get("full_page", False)
        device_id = args.get("device_id", "default")
        bt = _get_cloud_device(device_id)
        if not bt._using_playwright and not bt._using_selenium:
            return {"error": "No browser engine. Install playwright: pip install playwright && playwright install chromium"}
        screenshot = asyncio.run(bt.screenshot(full_page=full_page))
        if screenshot:
            return {"ok": True, "screenshot": screenshot, "device_id": device_id}
        return {"error": "Screenshot returned empty"}
    except Exception as e:
        return {"error": f"Cloud device screenshot failed: {e}"}

def exec_cloud_extract(args: dict) -> dict:
    """Extract text from cloud device browser."""
    import asyncio
    try:
        device_id = args.get("device_id", "default")
        selector = args.get("selector", "body")
        bt = _get_cloud_device(device_id)
        result = asyncio.run(bt.extract(selector))
        text = ""
        if result.extracted_data:
            if isinstance(result.extracted_data, dict):
                text = result.extracted_data.get("text", str(result.extracted_data))
            else:
                text = str(result.extracted_data)
        return {"content": text, "device_id": device_id}
    except Exception as e:
        return {"error": f"Cloud device extract failed: {e}"}

def exec_cloud_disconnect(args: dict) -> dict:
    """Disconnect cloud device browser."""
    import asyncio
    try:
        device_id = args.get("device_id", "default")
        global _executor_cloud_devices
        if device_id in _executor_cloud_devices:
            bt = _executor_cloud_devices.pop(device_id)
            if bt._using_playwright or bt._using_selenium:
                asyncio.run(bt.disconnect())
            return {"ok": True, "device_id": device_id}
        return {"ok": True, "device_id": device_id, "note": "not connected"}
    except Exception as e:
        return {"error": f"Cloud device disconnect failed: {e}"}


# ============ SubAgent 3 Tools ============

def exec_delegate_task(args: dict) -> dict:
    """Store a delegated task for processing."""
    import json, uuid, time as _time
    try:
        task = args["task"]
        task_id = uuid.uuid4().hex[:12]
        db_path = os.path.expanduser("~/.tical-code/subagents.db")
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE IF NOT EXISTS subagent_tasks (id TEXT PRIMARY KEY, description TEXT, status TEXT, result TEXT, created REAL)")
        conn.execute("INSERT INTO subagent_tasks (id, description, status, created) VALUES (?, ?, ?, ?)",
                     (task_id, task, "pending", _time.time()))
        conn.commit()
        conn.close()
        return {"task_id": task_id, "status": "pending"}
    except Exception as e:
        return {"error": f"Delegate failed: {e}"}

def exec_subagent_result(args: dict) -> dict:
    """Get result of a delegated task."""
    import sqlite3
    try:
        task_id = args["task_id"]
        db_path = os.path.expanduser("~/.tical-code/subagents.db")
        conn = sqlite3.connect(db_path)
        cur = conn.execute("SELECT id, description, status, result FROM subagent_tasks WHERE id=?", (task_id,))
        row = cur.fetchone()
        conn.close()
        if row:
            return {"task_id": row[0], "description": row[1], "status": row[2], "result": row[3]}
        return {"error": f"Task {task_id} not found"}
    except Exception as e:
        return {"error": f"Subagent result failed: {e}"}

def exec_subagent_list(args: dict) -> dict:
    """List all sub-agent tasks."""
    import sqlite3
    try:
        db_path = os.path.expanduser("~/.tical-code/subagents.db")
        conn = sqlite3.connect(db_path)
        cur = conn.execute("SELECT id, description, status FROM subagent_tasks ORDER BY rowid DESC")
        tasks = [{"id": r[0], "description": r[1][:60], "status": r[2]} for r in cur.fetchall()]
        conn.close()
        return {"tasks": tasks}
    except Exception as e:
        return {"error": f"Subagent list failed: {e}"}

# ============ Clarify 1 Tool ============

def exec_clarify_goal(args: dict) -> dict:
    """Analyze a goal for ambiguity, missing info, or high risk."""
    try:
        from tical_code.core.clarify import ClarifyPhase
        goal = args["goal"]
        cp = ClarifyPhase()
        result = cp.analyze_goal(goal)
        if result.questions:
            return {"clear": False, "issues": [q.to_dict() for q in result.questions]}
        return {"clear": True}
    except Exception as e:
        return {"error": f"Clarify failed: {e}"}

# ============ Cron 3 Tools ============

_executor_cron = None

def _get_cron():
    global _executor_cron
    if _executor_cron is None:
        from tical_code.core.cron_scheduler import CronScheduler
        _executor_cron = CronScheduler(data_dir=os.path.expanduser("~/.tical-code/cron_data"))
    return _executor_cron

def exec_cron_schedule(args: dict) -> dict:
    """Schedule a recurring task."""
    try:
        schedule = args["schedule"]
        task = args["task"]
        name = args.get("name", "")
        cr = _get_cron()
        cron_task = cr.add_task(name=name, schedule=schedule, action=task)
        return {"ok": True, "task_id": cron_task.id}
    except Exception as e:
        return {"error": f"Cron schedule failed: {e}"}

def exec_cron_list(args: dict) -> dict:
    """List all scheduled cron tasks."""
    try:
        cr = _get_cron()
        tasks = cr.list_tasks()
        return {"tasks": [{"id": t.id, "schedule": t.schedule, "name": t.name, "action": t.action} for t in tasks]}
    except Exception as e:
        return {"error": f"Cron list failed: {e}"}

def exec_cron_cancel(args: dict) -> dict:
    """Cancel a scheduled cron task."""
    try:
        task_id = args["task_id"]
        cr = _get_cron()
        ok = cr.remove_task(task_id)
        if ok:
            return {"ok": True, "task_id": task_id}
        return {"error": f"Task {task_id} not found"}
    except Exception as e:
        return {"error": f"Cron cancel failed: {e}"}


def exec_xurl_post(args):
    import asyncio
    try:
        from tical_code.plugins.xurl import XUrlPlugin
        xp = XUrlPlugin()
        result = asyncio.run(xp.post_tweet(args))
        if result.success:
            return {"ok": True, "data": result.data, "text": str(args.get("text", ""))[:50]}
        return {"error": result.error or "post_tweet failed"}
    except Exception as e:
        return {"error": "xurl_post: " + str(e)}

def exec_xurl_reply(args):
    import asyncio
    try:
        from tical_code.plugins.xurl import XUrlPlugin
        xp = XUrlPlugin()
        result = asyncio.run(xp.reply_tweet(args))
        if result.success:
            return {"ok": True, "data": result.data}
        return {"error": result.error or "reply_tweet failed"}
    except Exception as e:
        return {"error": "xurl_reply: " + str(e)}

def exec_xurl_timeline(args):
    import asyncio
    try:
        from tical_code.plugins.xurl import XUrlPlugin
        xp = XUrlPlugin()
        result = asyncio.run(xp.get_timeline(args))
        if result.success:
            return {"ok": True, "data": result.data, "tweets": result.data.get("tweets", [])}
        return {"error": result.error or "get_timeline failed"}
    except Exception as e:
        return {"error": "xurl_timeline: " + str(e)}

def exec_web_search(args):
    """Search the internet for information."""
    import asyncio
    try:
        from tical_code.plugins.search_plugin import SearchPlugin
        sp = SearchPlugin()
        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(sp.web_search(args))
        loop.close()
        if result.success:
            return {"ok": True, "data": result.data, "results": result.data.get("results", [])}
        return {"error": result.error or "web_search failed"}
    except ImportError:
        return {"error": "Search plugin not available"}
    except Exception as e:
        return {"error": "web_search: " + str(e)}


# ============ Secret Redaction ============

_DEFAULT_REDACTION_PATTERNS = [
    ("api_key_openai", re.compile(r'sk-[a-zA-Z0-9]{20,}')),
    ("api_key_google", re.compile(r'AIza[a-zA-Z0-9_-]{35}')),
    ("api_key_generic", re.compile(r'["\']?api[_-]?key["\']?\s*[:=]\s*["\']?([a-zA-Z0-9_\-]{20,})["\']?', re.IGNORECASE)),
    ("token_github", re.compile(r'ghp_[a-zA-Z0-9]{36}')),
    ("token_gitlab", re.compile(r'glpat-[a-zA-Z0-9\-]{20,}')),
    ("password", re.compile(r'(?:password|passwd|pwd)\s*[:=]\s*\S+', re.IGNORECASE)),
    ("private_key", re.compile(r'-----BEGIN (?:RSA |EC )?PRIVATE KEY-----')),
    ("connection_mongodb", re.compile(r'mongodb://[^:\s]+:[^@\s]+@')),
    ("connection_postgres", re.compile(r'postgres(?:ql)?://[^:\s]+:[^@\s]+@', re.IGNORECASE)),
    ("connection_mysql", re.compile(r'mysql://[^:\s]+:[^@\s]+@')),
    ("connection_redis", re.compile(r'redis://:[^@\s]+@')),
    ("aws_access_key", re.compile(r'AKIA[0-9A-Z]{16}')),
    ("aws_secret_key", re.compile(r'["\']?aws[_-]?secret[_-]?access[_-]?key["\']?\s*[:=]\s*["\']?[A-Za-z0-9/+=]{40}["\']?', re.IGNORECASE)),
    ("bearer_token", re.compile(r'Bearer\s+[a-zA-Z0-9_\-\.]{20,}', re.IGNORECASE)),
]

def redact_secrets(text: str) -> str:
    """Redact API keys, tokens, passwords from text for safe logging."""
    if not text:
        return text
    result = text
    for type_name, pattern in _DEFAULT_REDACTION_PATTERNS:
        result = pattern.sub(f"[REDACTED_{type_name}]", result)
    return result


# ============ FTS Memory 1 Tool ============

_executor_fts = None

def _get_fts():
    global _executor_fts
    if _executor_fts is None:
        from tical_code.core.memory_store import MemoryFTSStore
        _executor_fts = MemoryFTSStore(
            memory_dir=os.path.expanduser("~/.tical-code/memory"),
            db_path=os.path.expanduser("~/.tical-code/fts_memory.db"))
    return _executor_fts

def exec_memory_fts_search(args: dict) -> dict:
    """Full-text search across all persistent memory."""
    try:
        query = args["query"]
        limit = min(int(args.get("top_k", 10)), 50)
        fts = _get_fts()
        results = fts.search(query=query, limit=limit)
        return {"results": results, "total": len(results)}
    except Exception as e:
        return {"error": f"FTS search failed: {e}"}

# ============ Dispatcher ============

def execute(name: str, args: dict, base_dir: str = "") -> dict:
    """Unified dispatch entry. name -> exec_* function."""
    logger.info(f"[executor] {name}({str(args)[:80]})")
    dispatch = {
        "bash": exec_bash,
        "xurl_post": exec_xurl_post,
        "xurl_reply": exec_xurl_reply,
        "xurl_timeline": exec_xurl_timeline,
        "web_search": exec_web_search,
        "file_read": lambda a: exec_file_read(a, base_dir),
        "file_write": lambda a: exec_file_write(a, base_dir),
        "memory_save": lambda a: exec_memory_save(a, base_dir),
        "memory_load": lambda a: exec_memory_load(a, base_dir),
        "state_save": lambda a: exec_state_save(a, base_dir),
        "conv_search": exec_conv_search,
        "chat_send": exec_chat_send,
        "web_fetch": exec_web_fetch,
        "analyze_image": exec_analyze_image,
        "ocr": exec_ocr,
        "patch_file": exec_patch_file,
        "browser_navigate": exec_browser_navigate,
        "browser_click": exec_browser_click,
        "browser_screenshot": exec_browser_screenshot,
        "browser_extract": exec_browser_extract,
        "delegate_task": exec_delegate_task,
        "subagent_result": exec_subagent_result,
        "subagent_list": exec_subagent_list,
        "clarify_goal": exec_clarify_goal,
        "cron_schedule": exec_cron_schedule,
        "cron_list": exec_cron_list,
        "cron_cancel": exec_cron_cancel,
        "cloud_device.navigate": exec_cloud_navigate,
        "cloud_device.click": exec_cloud_click,
        "cloud_device.type": exec_cloud_type,
        "cloud_device.screenshot": exec_cloud_screenshot,
        "cloud_device.extract": exec_cloud_extract,
        "cloud_device.disconnect": exec_cloud_disconnect,
        "memory_fts_search": exec_memory_fts_search,
    }
    handler = dispatch.get(name)
    if not handler:
        return {"error": f"Unknown tool: {name}"}

    try:
        result = handler(args)
        if isinstance(result, dict) and "error" in result and "explicit_error" not in result:
            logger.warning(f"[executor] {name} error: {result['error'][:100]}")
        return result or {}
    except Exception as e:
        logger.error(f"[executor] {name} exception: {e}")
        return {"error": str(e)}
