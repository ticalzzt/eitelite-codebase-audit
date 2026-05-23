"""
SubAgent Interface — data types for agent-to-agent delegation.

B.1.1: SubAgentTask + SubAgentResult dataclasses
B.1.2: spawn_subagent() — start isolated Python subprocess
"""

import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("tical-code.subagent")


# ═══════════════════════════════════════════════════════════════
# B.1.1: Data types
# ═══════════════════════════════════════════════════════════════

@dataclass
class SubAgentTask:
    """A task delegated to a sub-agent process."""
    goal: str
    context: str = ""
    tools: List[str] = field(default_factory=lambda: ["bash", "file_read", "file_write"])
    max_rounds: int = 5
    timeout_sec: float = 120.0


@dataclass
class SubAgentResult:
    """Result returned by a sub-agent process."""
    success: bool
    output: str = ""
    error: str = ""
    tool_calls_made: int = 0
    elapsed_sec: float = 0.0


# ═══════════════════════════════════════════════════════════════
# B.1.2: Process spawning
# ═══════════════════════════════════════════════════════════════

_WORKER_SCRIPT = Path(__file__).parent / "subagent_worker.py"


def spawn_subagent(task: SubAgentTask) -> subprocess.Popen:
    """
    Spawn a sub-agent process.
    
    The child process runs subagent_worker.py, which receives the task
    via stdin (JSON) and writes SubAgentResult to stdout (JSON).
    
    Args:
        task: The task definition
        
    Returns:
        Popen handle (caller manages lifecycle)
    """
    if not _WORKER_SCRIPT.exists():
        raise FileNotFoundError(
            f"SubAgent worker script not found: {_WORKER_SCRIPT}"
        )

    task_json = json.dumps({
        "goal": task.goal,
        "context": task.context,
        "tools": task.tools,
        "max_rounds": task.max_rounds,
        "timeout_sec": task.timeout_sec,
    })

    proc = subprocess.Popen(
        [sys.executable, str(_WORKER_SCRIPT)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=os.environ.copy(),
    )

    # Feed task via stdin
    proc.stdin.write(task_json + "\n")
    proc.stdin.flush()

    logger.info(f"[subagent] spawned PID={proc.pid} goal={task.goal[:60]}")
    return proc


def run_subagent(task: SubAgentTask, timeout: float = 120.0) -> SubAgentResult:
    """
    Spawn, wait for result, return SubAgentResult.
    
    B.1.5: Result is read from subprocess stdout (single JSON line).
    
    Args:
        task: The task to execute
        timeout: Total timeout including child execution
        
    Returns:
        SubAgentResult
    """
    t0 = time.time()
    try:
        proc = spawn_subagent(task)
        stdout, stderr = proc.communicate(timeout=min(timeout, task.timeout_sec))
        elapsed = time.time() - t0

        # Parse stdout as SubAgentResult
        if stdout.strip():
            try:
                data = json.loads(stdout.strip())
                return SubAgentResult(
                    success=data.get("success", False),
                    output=data.get("output", ""),
                    error=data.get("error", ""),
                    tool_calls_made=data.get("tool_calls_made", 0),
                    elapsed_sec=elapsed,
                )
            except json.JSONDecodeError:
                return SubAgentResult(
                    success=proc.returncode == 0,
                    output=stdout.strip()[:2000],
                    error=f"JSON parse error. stderr: {stderr[:500]}",
                    elapsed_sec=elapsed,
                )

        return SubAgentResult(
            success=proc.returncode == 0,
            error=f"Empty stdout. stderr: {stderr[:500]}",
            elapsed_sec=elapsed,
        )

    except subprocess.TimeoutExpired:
        proc.kill()
        return SubAgentResult(
            success=False,
            error=f"Timeout after {timeout}s",
            elapsed_sec=time.time() - t0,
        )
    except Exception as e:
        return SubAgentResult(
            success=False,
            error=str(e),
            elapsed_sec=time.time() - t0,
        )
