"""Response formatting - tool results to human-readable text."""

import json
import logging

logger = logging.getLogger("tical-code.formatter")

def format_error(name: str, error: str) -> str:
    return f"[{name}] error: {error}"

def format_progress(name: str, status: str) -> str:
    return f"[{name}] {status}"

def format_result(name: str, result: dict) -> str:
    """Tool execution result to one-line summary."""
    if not result:
        return f"[{name}] no result"

    if "error" in result:
        return f"[{name}] {result['error']}"

    # bash
    if name == "bash" and "exit_code" in result:
        out = result.get("stdout", "")
        err = result.get("stderr", "")
        code = result.get("exit_code", -1)
        if code == 0 and out:
            return out[:4000]
        elif code != 0:
            return f"[bash] exit={code} {err[:200]}"
        return "[bash] done (no output)"

    # file_read
    if name == "file_read" and "content" in result:
        return f"[file] {result['path']}: {result['content'][:4000]}"

    # file_write
    if name == "file_write":
        return f"[file] written to {result.get('path', '?')}" if result.get("ok") else "[file] write failed"

    # memory
    if name == "memory_save":
        return f"[memory] saved key={result.get('key', '?')}"

    if name == "memory_load":
        entries = result.get("entries", {})
        if entries:
            return "[memory] " + "; ".join(
                f"{k}: {v.get('value', '')[:30]}"
                for k, v in list(entries.items())[:5]
            )
        return "[memory] no entries"

    # state
    if name == "state_save":
        return f"[state] saved {result.get('key', '?')}" if result.get("ok") else "[state] save failed"

    # chat_send
    if name == "chat_send":
        target = result.get("target", "?")
        return f"[chat] sent to {target}" if result.get("ok") else "[chat] send failed"

    return json.dumps(result, ensure_ascii=False)[:4000]

