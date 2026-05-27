"""
SubAgent Worker — runs inside a separate Python process.

B.1.3: Reads SubAgentTask from stdin (JSON)
       Initializes DeepSeekProvider from env
       Calls chat_with_tools with executor bound to tool_executor.execute
       Writes SubAgentResult to stdout (JSON)
B.1.4: Timeout per round (default 30s), cleanup on exit
"""

import json
import logging
import os
import sys
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("subagent-worker")


def main():
    # Read task from stdin (parent sent it as a single JSON line)
    raw = sys.stdin.readline()
    if not raw:
        result = {"success": False, "error": "No input received on stdin"}
        print(json.dumps(result))
        sys.exit(0)

    try:
        task = json.loads(raw.strip())
    except json.JSONDecodeError as e:
        result = {"success": False, "error": f"Invalid task JSON: {e}"}
        print(json.dumps(result))
        sys.exit(0)

    goal = task.get("goal", "")
    context = task.get("context", "")
    tool_names = task.get("tools", ["bash", "file_read", "file_write"])
    max_rounds = task.get("max_rounds", 5)
    timeout_sec = task.get("timeout_sec", 120)

    # Add parent dir to path
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

    # Build messages
    messages = [{"role": "system", "content": f"You are a sub-agent. Goal: {goal}"}]
    if context:
        messages.append({"role": "user", "content": f"Context: {context}"})
    messages.append({"role": "user", "content": goal})

    # Load tools from TOOL_SCHEMAS (only requested tools)
    try:
        from tical_code.core.tool_executor import TOOL_SCHEMAS, execute
        tools = [
            s for s in TOOL_SCHEMAS
            if s["function"]["name"] in tool_names
        ]
    except Exception as e:
        result = {"success": False, "error": f"Tool loading failed: {e}"}
        print(json.dumps(result))
        sys.exit(0)

    # Create executor callback
    def executor(name, arguments):
        return execute(name, arguments)

    # Create LLM provider
    try:
        from tical_code.core.llm_interface import DeepSeekProvider
        # Model: AI_MODEL > DEEPSEEK_MODEL > OPENAI_MODEL > default
        _model = (
            os.environ.get("AI_MODEL", "")
            or os.environ.get("DEEPSEEK_MODEL", "")
            or os.environ.get("OPENAI_MODEL", "")
            or "deepseek-chat"
        )
        llm = DeepSeekProvider(
            api_key=os.environ.get("OPENAI_API_KEY", "") or os.environ.get("DEEPSEEK_API_KEY", ""),
            base_url=os.environ.get("OPENAI_BASE_URL", "") or os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
            model=_model,
        )
    except Exception as e:
        result = {"success": False, "error": f"LLM init failed: {e}"}
        print(json.dumps(result))
        sys.exit(0)

    # Run
    try:
        resp = llm.chat_with_tools(
            messages,
            tools=tools,
            executor=executor,
            max_rounds=max_rounds,
            timeout=min(timeout_sec, 30),
        )
        result = {
            "success": resp.finish_reason != "error",
            "output": resp.content[:10000],
            "tool_calls_made": len(resp.tool_calls),
            "finish_reason": resp.finish_reason,
        }
    except Exception as e:
        result = {"success": False, "error": f"Execution error: {e}"}

    print(json.dumps(result))


if __name__ == "__main__":
    main()
