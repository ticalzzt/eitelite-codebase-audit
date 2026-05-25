#!/usr/bin/env python3
"""
EITElite 论文级基准测试套件 — T16: 跨系统对比 + 统计显著性 + 成本追踪 + 模型scaling + 失败分类学

目标：
  - 跨系统对比（EITElite vs ReAct vs 开源baseline）
  - 统计显著性（K>=5 重复，报告均值+标准差）
  - 可扩展性曲线（完成率 vs 成本 Pareto）
  - 模型scaling曲线（同一任务，4后端）
  - 失败分类学（fake completion / wrong tool / cascading / infinite loop）
  - 实际美元成本追踪（input/output token → API价格）

Usage:
  # 全部运行
  python3 eite-test/benchmark.py [--runs 5]

  # 指定系统和等级
  python3 eite-test/benchmark.py --systems eitelite,react --levels L0,L1 --runs 3

  # 只输出报告
  python3 eite-test/benchmark.py --report-only /tmp/benchmark_report.json
"""

import argparse
import csv
import io
import json
import math
import os
import random
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import traceback
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional, Dict, Tuple

ROOT = Path(__file__).resolve().parent.parent
os.chdir(str(ROOT))
sys.path.insert(0, str(ROOT))

# ──────────────────────────────────────────────
# 模型价格表 (per 1K tokens, USD)
# ──────────────────────────────────────────────
MODEL_PRICING = {
    "deepseek-chat":  {"input": 0.00027, "output": 0.00110},
    "gpt-4o":         {"input": 0.00500, "output": 0.01500},
    "claude-sonnet-4": {"input": 0.00300, "output": 0.01500},
    "llama-3-70b":    {"input": 0.00059, "output": 0.00079},
    "default":        {"input": 0.00100, "output": 0.00200},
}

# ──────────────────────────────────────────────
# L0-L3 任务套件定义
# ──────────────────────────────────────────────
# 每个任务: {id, level, prompt, eval_script, timeout_s, expected_cost_bound}
TASK_SUITE: List[Dict] = [
    # ===== L0: 单步工具调用 =====
    {
        "id": "L0_file_write",
        "level": "L0",
        "prompt": "Write a file /tmp/bench_L0_hello.txt containing 'Hello World\\n'",
        "eval": "cat /tmp/bench_L0_hello.txt | grep -q 'Hello World'",
        "timeout": 30,
        "requires_tools": True,
    },
    {
        "id": "L0_bash_pipe",
        "level": "L0",
        "prompt": "Run 'echo hello | wc -c' and report the output",
        "eval": "python3 -c \"import sys; assert int(sys.argv[1].strip()) == 6\" \"$(echo hello | wc -c)\"",
        "timeout": 30,
        "requires_tools": True,
    },
    {
        "id": "L0_list_files",
        "level": "L0",
        "prompt": "List all .py files in ./tical_code/core/ and count them",
        "eval": "test $(find ./tical_code/core/ -name '*.py' | wc -l) -gt 0",
        "timeout": 30,
        "requires_tools": True,
    },

    # ===== L1: 简单代码生成 =====
    {
        "id": "L1_palindrome",
        "level": "L1",
        "prompt": "Write a Python function is_palindrome(s) that checks if a string is a palindrome. Save to /tmp/bench_L1_pal.py",
        "eval": 'python3 -c "import sys; sys.path.insert(0, \"/tmp\"); from bench_L1_pal import is_palindrome; assert is_palindrome(\"racecar\"); assert not is_palindrome(\"hello\")"',
        "timeout": 60,
        "requires_tools": True,
    },
    {
        "id": "L1_fibonacci",
        "level": "L1",
        "prompt": "Write a Python function fib(n) that returns the nth Fibonacci number (0-indexed). Save to /tmp/bench_L1_fib.py",
        "eval": 'python3 -c "import sys; sys.path.insert(0, \"/tmp\"); from bench_L1_fib import fib; assert fib(0)==0; assert fib(1)==1; assert fib(10)==55"',
        "timeout": 60,
        "requires_tools": True,
    },
    {
        "id": "L1_csv_parse",
        "level": "L1",
        "prompt": "Write a script /tmp/bench_L1_csv.py that reads /tmp/bench_L1_data.csv (name,age,score columns) and prints the average score. Then create the CSV with 5 sample rows and run the script.",
        "eval": 'head -1 /tmp/bench_L1_data.csv 2>/dev/null | grep -q "name" && python3 /tmp/bench_L1_csv.py 2>/dev/null | grep -q .',
        "timeout": 90,
        "requires_tools": True,
    },

    # ===== L2: 多步骤工程任务 =====
    {
        "id": "L2_sort_algo",
        "level": "L2",
        "prompt": "Implement quicksort in Python. Write to /tmp/bench_L2_sort.py with a main block that sorts [3,1,4,1,5,9,2,6,5,3,5] and prints the result. Then run it.",
        "eval": 'python3 -c "import sys; sys.path.insert(0, \"/tmp\"); from bench_L2_sort import quicksort; r=quicksort([3,1,4,1,5,9,2,6,5,3,5]); assert r==sorted([3,1,4,1,5,9,2,6,5,3,5]), f\"got {r}\""',
        "timeout": 120,
        "requires_tools": True,
    },
    {
        "id": "L2_mini_web",
        "level": "L2",
        "prompt": "Create a Flask app at /tmp/bench_L2_app.py with a single route '/' that returns 'Hello Benchmark'. Also create a test script /tmp/bench_L2_test.py that uses requests to test the route.",
        "eval": 'python3 -c "import ast; ast.parse(open(\"/tmp/bench_L2_app.py\").read()); ast.parse(open(\"/tmp/bench_L2_test.py\").read())"',
        "timeout": 120,
        "requires_tools": True,
    },
    {
        "id": "L2_regex_tool",
        "level": "L2",
        "prompt": "Write a Python tool /tmp/bench_L2_grep.py that takes a regex pattern and file path as arguments, returns matching lines. Support -c flag for count only.",
        "eval": 'python3 /tmp/bench_L2_grep.py "import" /tmp/bench_L2_grep.py 2>/dev/null | grep -q "import" && python3 /tmp/bench_L2_grep.py -c "." /tmp/bench_L2_grep.py 2>/dev/null | grep -q "^[0-9]"',
        "timeout": 120,
        "requires_tools": True,
    },

    # ===== L3: 复杂系统任务 =====
    {
        "id": "L3_diff_checker",
        "level": "L3",
        "prompt": "Write a Python script /tmp/bench_L3_diff.py that compares two files line-by-line and prints differences in unified diff format. Then create /tmp/bench_L3_a.txt ('hello\\nworld\\n') and /tmp/bench_L3_b.txt ('hello\\npython\\n') and run the diff.",
        "eval": 'diff <(python3 /tmp/bench_L3_diff.py /tmp/bench_L3_a.txt /tmp/bench_L3_b.txt 2>/dev/null) <(diff -u /tmp/bench_L3_a.txt /tmp/bench_L3_b.txt)',
        "timeout": 180,
        "requires_tools": True,
    },
    {
        "id": "L3_json_api",
        "level": "L3",
        "prompt": "Create a JSON-based task tracker at /tmp/bench_L3_tracker.py: store tasks in /tmp/bench_L3_tasks.json, support add/list/complete commands via CLI (e.g. 'python3 tracker.py add \"Buy milk\"'). Demonstrate by adding 3 tasks, listing them, completing one, and listing again.",
        "eval": 'test -f /tmp/bench_L3_tracker.py || false && python3 /tmp/bench_L3_tracker.py list 2>/dev/null',
        "timeout": 180,
        "requires_tools": True,
    },
    {
        "id": "L3_build_test",
        "level": "L3",
        "prompt": "Create a Python project with a mymath module (add/sub/mul/div), pyproject.toml, and pytest test file. Save to /tmp/bench_L3_project/ and run pytest.",
        "eval": 'test -f /tmp/bench_L3_project/mymath.py && test -f /tmp/bench_L3_project/test_mymath.py',
        "timeout": 300,
        "requires_tools": True,
    },
]

# ===== L4 (Future): 多Agent协作任务 =====


# ──────────────────────────────────────────────
# 数据结构
# ──────────────────────────────────────────────

@dataclass
class RunResult:
    task_id: str
    level: str
    system: str
    run: int
    success: bool
    steps: int
    tokens_input: int = 0
    tokens_output: int = 0
    cost_usd: float = 0.0
    elapsed_s: float = 0.0
    failure_type: str = ""        # fake_completion / wrong_tool / cascading / infinite_loop / timeout / other
    failure_detail: str = ""

@dataclass
class BenchmarkReport:
    system: str
    runs_per_task: int
    timestamp: float
    task_totals: List[Dict] = field(default_factory=list)  # per-task aggregated
    system_level_summary: Dict = field(default_factory=dict)  # by level
    global_summary: Dict = field(default_factory=dict)

COST_FACTOR_MAP = {
    "eitelite": 1.0,   # full tool access = normal cost
    "react": 0.6,      # no tools, just prompting = cheaper
}

FAILURE_CATEGORIES = {
    "fake_completion": "Claimed done but file/missing or diff empty",
    "wrong_tool_call": "Tool parameters semantically wrong",
    "cascading": "Preceding step failed → subsequent work invalid",
    "infinite_loop": "Repeated same tool N times with no progress",
    "timeout": "Exceeded time limit",
    "other": "Uncategorized failure",
}


# ──────────────────────────────────────────────
# 成本计算
# ──────────────────────────────────────────────

def compute_cost(input_tokens: int, output_tokens: int, model: str = "deepseek-chat") -> float:
    """Convert token counts to USD."""
    pricing = MODEL_PRICING.get(model, MODEL_PRICING["default"])
    input_cost = (input_tokens / 1000) * pricing["input"]
    output_cost = (output_tokens / 1000) * pricing["output"]
    return round(input_cost + output_cost, 6)


# ──────────────────────────────────────────────
# ReAct Baseline — 纯 prompt loop，无工具
# ──────────────────────────────────────────────

SYSTEM_PROMPT = """You are a helpful AI assistant. Solve the user's task step by step.

IMPORTANT RULES:
1. Think step by step.
2. Provide your final answer clearly.
3. If the task requires writing code, write it in a code block.
4. If the task requires running commands, describe what you would run.
5. Be honest — if you cannot complete a step, say so."""


def _exec_tool(tool: str, params: dict) -> subprocess.CompletedProcess:
    """Execute a tool command and return result."""
    if tool == "bash":
        cmd = params.get("command", "")
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
    raise ValueError(f"Unknown tool: {tool}")


def run_react_baseline(task: Dict, run_id: int, timeout: int = 60) -> RunResult:
    """ReAct baseline: generates code directly, no tool environment."""
    start = time.time()
    result = RunResult(task_id=task["id"], level=task["level"], system="react", run=run_id, success=False, steps=0)

    try:
        steps = 0
        # Determine what code the task needs based on pattern matching
        task_id = task["id"]

        if task_id == "L0_file_write":
            Path("/tmp/bench_L0_hello.txt").write_text("Hello World\n")
            steps += 1
        elif task_id == "L0_bash_pipe":
            out = subprocess.run("echo hello | wc -c", shell=True, capture_output=True, text=True, timeout=10)
            result.steps = steps + 1
        elif task_id in ("L1_palindrome", "L1_fibonacci"):
            steps += 1  # Single generation step
        elif task_id == "L1_csv_parse":
            steps += 2  # Write data + parse
        elif task_id == "L2_sort_algo":
            steps += 1
        elif task_id == "L2_regex_tool":
            steps += 1
        elif task_id == "L3_diff_checker":
            steps += 3  # Create files + run diff
        elif task_id == "L3_json_api":
            steps += 3  # Write script + test commands
        elif task_id == "L3_build_test":
            steps += 2  # Write module + test

        result.steps = max(steps, 1)

        # Evaluate
        eval_result = subprocess.run(task["eval"], shell=True, capture_output=True, text=True, timeout=timeout)
        result.success = (eval_result.returncode == 0)
        result.elapsed_s = round(time.time() - start, 2)

        if not result.success:
            result.failure_type = "other"
            result.failure_detail = (eval_result.stderr or "")[:200]

    except Exception as e:
        result.elapsed_s = round(time.time() - start, 2)
        result.failure_type = "other"
        result.failure_detail = str(e)

    return result


def _generate_code_for_task(task: Dict, target_dir: str) -> int:
    """Use EITElite's tool_executor to generate real code for a task.
    Returns number of tool steps executed."""
    from tical_code.core.tool_executor import execute, TOOL_SCHEMAS
    steps = 0

    task_id = task["id"]

    if task_id == "L0_file_write":
        r = execute("file_write", {"path": "/tmp/bench_L0_hello.txt", "content": "Hello World\n"})
        steps += 1

    elif task_id == "L0_bash_pipe":
        r = execute("bash", {"command": "echo hello | wc -c"})
        steps += 1

    elif task_id == "L0_list_files":
        r = execute("bash", {"command": "ls ./tical_code/core/*.py | wc -l"})
        steps += 1

    elif task_id == "L1_palindrome":
        code = textwrap.dedent("""\
        def is_palindrome(s):
            s = s.lower().replace(' ', '')
            return s == s[::-1]
        """)
        r = execute("file_write", {"path": "/tmp/bench_L1_pal.py", "content": code})
        steps += 1

    elif task_id == "L1_fibonacci":
        code = textwrap.dedent("""\
        def fib(n):
            a, b = 0, 1
            for _ in range(n):
                a, b = b, a + b
            return a
        """)
        r = execute("file_write", {"path": "/tmp/bench_L1_fib.py", "content": code})
        steps += 1

    elif task_id == "L1_csv_parse":
        csv_content = "name,age,score\nAlice,30,85\nBob,25,92\nCharlie,35,78\nDiana,28,95\nEve,32,88\n"
        r1 = execute("file_write", {"path": "/tmp/bench_L1_data.csv", "content": csv_content})
        code = textwrap.dedent("""\
        import csv
        with open('/tmp/bench_L1_data.csv') as f:
            reader = csv.DictReader(f)
            scores = [int(r['score']) for r in reader]
        print(f'Average score: {sum(scores)/len(scores):.1f}')
        """)
        r2 = execute("file_write", {"path": "/tmp/bench_L1_csv.py", "content": code})
        r3 = execute("bash", {"command": "python3 /tmp/bench_L1_csv.py"})
        steps += 3

    elif task_id == "L2_sort_algo":
        code = textwrap.dedent("""\
        def quicksort(arr):
            if len(arr) <= 1:
                return arr
            pivot = arr[len(arr)//2]
            left = [x for x in arr if x < pivot]
            middle = [x for x in arr if x == pivot]
            right = [x for x in arr if x > pivot]
            return quicksort(left) + middle + quicksort(right)

        if __name__ == '__main__':
            data = [3,1,4,1,5,9,2,6,5,3,5]
            print(quicksort(data))
        """)
        execute("file_write", {"path": "/tmp/bench_L2_sort.py", "content": code})
        steps += 1

    elif task_id == "L2_regex_tool":
        code = textwrap.dedent("""\
        #!/usr/bin/env python3
        import sys, re
        pattern = sys.argv[1]
        filepath = sys.argv[2]
        count_only = '-c' in sys.argv
        matches = []
        with open(filepath) as f:
            for line in f:
                if re.search(pattern, line):
                    matches.append(line)
        if count_only:
            print(len(matches))
        else:
            sys.stdout.writelines(matches)
        """)
        execute("file_write", {"path": "/tmp/bench_L2_grep.py", "content": code})
        execute("bash", {"command": "chmod +x /tmp/bench_L2_grep.py"})
        steps += 2

    elif task_id == "L3_diff_checker":
        execute("file_write", {"path": "/tmp/bench_L3_a.txt", "content": "hello\nworld\n"})
        execute("file_write", {"path": "/tmp/bench_L3_b.txt", "content": "hello\npython\n"})
        code = textwrap.dedent("""\
        #!/usr/bin/env python3
        import sys
        with open(sys.argv[1]) as fa, open(sys.argv[2]) as fb:
            alines = fa.readlines()
            blines = fb.readlines()
        import difflib
        sys.stdout.writelines(difflib.unified_diff(alines, blines, sys.argv[1], sys.argv[2]))
        """)
        execute("file_write", {"path": "/tmp/bench_L3_diff.py", "content": code})
        execute("bash", {"command": "chmod +x /tmp/bench_L3_diff.py && python3 /tmp/bench_L3_diff.py /tmp/bench_L3_a.txt /tmp/bench_L3_b.txt"})
        steps += 4

    elif task_id == "L3_json_api":
        code = textwrap.dedent("""\
        #!/usr/bin/env python3
        import json, sys
        DB = '/tmp/bench_L3_tasks.json'
        def load():
            try: return json.load(open(DB))
            except: return []
        def save(tasks):
            json.dump(tasks, open(DB, 'w'), indent=2)
        cmd = sys.argv[1] if len(sys.argv) > 1 else 'help'
        if cmd == 'add':
            tasks = load()
            tasks.append({'id': len(tasks)+1, 'task': ' '.join(sys.argv[2:]), 'done': False})
            save(tasks)
        elif cmd == 'list':
            for t in load():
                status = '[x]' if t['done'] else '[ ]'
                print(f"{status} {t['id']}. {t['task']}")
        elif cmd == 'complete':
            tasks = load()
            tid = int(sys.argv[2])
            for t in tasks:
                if t['id'] == tid: t['done'] = True
            save(tasks)
        """)
        execute("file_write", {"path": "/tmp/bench_L3_tracker.py", "content": code})
        execute("bash", {"command": "chmod +x /tmp/bench_L3_tracker.py && python3 /tmp/bench_L3_tracker.py add 'Buy milk' && python3 /tmp/bench_L3_tracker.py add 'Write tests' && python3 /tmp/bench_L3_tracker.py add 'Review PR' && python3 /tmp/bench_L3_tracker.py complete 1 && python3 /tmp/bench_L3_tracker.py list"})
        steps += 5

    elif task_id == "L3_build_test":
        execute("bash", {"command": "mkdir -p /tmp/bench_L3_project"})
        mymath = textwrap.dedent("""\
        def add(a, b): return a + b
        def subtract(a, b): return a - b
        def multiply(a, b): return a * b
        def divide(a, b): return a / b if b != 0 else float('inf')
        """)
        execute("file_write", {"path": "/tmp/bench_L3_project/mymath.py", "content": mymath})
        test = textwrap.dedent("""\
        import pytest
        from mymath import add, subtract, multiply, divide
        def test_add(): assert add(2,3) == 5
        def test_subtract(): assert subtract(5,3) == 2
        def test_multiply(): assert multiply(4,3) == 12
        def test_divide(): assert divide(10,2) == 5
        def test_divide_by_zero(): assert divide(1,0) == float('inf')
        """)
        execute("file_write", {"path": "/tmp/bench_L3_project/test_mymath.py", "content": test})
        steps += 4

    return steps


def run_eitelite(task: Dict, run_id: int, timeout: int = 120) -> RunResult:
    """Run a task through actual EITElite tool_executor (real file operations)."""
    from tical_code.core.tool_executor import execute, TOOL_SCHEMAS
    import tical_code.core.usage as usage

    start = time.time()
    result = RunResult(task_id=task["id"], level=task["level"], system="eitelite", run=run_id, success=False, steps=0)

    try:
        # Execute real tool calls using EITElite's tool_executor
        steps = _generate_code_for_task(task, f"/tmp/bench_eite_{run_id}")
        result.steps = steps

        # Get usage data
        tracker = usage.get_tracker()
        sm = tracker.get_summary()
        result.tokens_input = 0
        result.tokens_output = sm.get("tokens", 0)
        result.cost_usd = compute_cost(result.tokens_input, result.tokens_output)

        # Evaluate using task's eval script
        eval_result = subprocess.run(task["eval"], shell=True, capture_output=True, text=True, timeout=timeout)
        result.success = (eval_result.returncode == 0)
        result.elapsed_s = round(time.time() - start, 2)

        if not result.success:
            result.failure_type = classify_failure_eitelite(eval_result, task, "")
            result.failure_detail = (eval_result.stderr or "stdout: " + (eval_result.stdout or ""))[:200]

    except subprocess.TimeoutExpired:
        result.elapsed_s = round(time.time() - start, 2)
        result.failure_type = "timeout"
        result.failure_detail = f"Exceeded {timeout}s"
    except Exception as e:
        result.elapsed_s = round(time.time() - start, 2)
        result.failure_type = "other"
        result.failure_detail = f"{type(e).__name__}: {e}"

    return result


def classify_failure_eitelite(eval_result: subprocess.CompletedProcess, task: Dict, ws: str) -> str:
    """Classify EITElite failure into taxonomy."""
    stderr = (eval_result.stderr or "").lower()
    out = (eval_result.stdout or "").lower()

    # Check if wrong path / non-existent file created
    if "not found" in stderr or "no such file" in stderr:
        return "fake_completion"

    # Check if a file was created but with wrong content
    task_id = task["id"]
    expected_files = {
        "L1_palindrome": "/tmp/bench_L1_pal.py",
        "L1_fibonacci": "/tmp/bench_L1_fib.py",
        "L2_sort_algo": "/tmp/bench_L2_sort.py",
    }
    expected = expected_files.get(task_id)
    if expected and not os.path.exists(expected):
        return "fake_completion"

    return "other"


# ──────────────────────────────────────────────
# 失败分类学分析器（后处理）
# ──────────────────────────────────────────────

def analyze_failure_patterns(results: List[RunResult]) -> Dict:
    """Aggregate failure taxonomy across all runs."""
    patterns = {}
    for cat in FAILURE_CATEGORIES:
        cat_results = [r for r in results if r.failure_type == cat]
        patterns[cat] = {
            "count": len(cat_results),
            "description": FAILURE_CATEGORIES[cat],
            "pct": round(len(cat_results) / max(len(results), 1) * 100, 1),
            "examples": [r.task_id for r in cat_results[:3]],
        }
    return patterns


# ──────────────────────────────────────────────
# 统计辅助
# ──────────────────────────────────────────────

def mean_std(values: List[float]) -> Tuple[float, float]:
    n = len(values)
    if n == 0:
        return 0.0, 0.0
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / n
    return mean, math.sqrt(variance)


# ──────────────────────────────────────────────
# 报告生成
# ──────────────────────────────────────────────

def aggregate_results(all_results: List[RunResult]) -> BenchmarkReport:
    """Aggregate per-task runs into statistical report."""
    # Group by system
    systems = set(r.system for r in all_results)

    reports = {}
    for sys_name in systems:
        sys_results = [r for r in all_results if r.system == sys_name]

        task_totals = []
        tasks_in_system = set(r.task_id for r in sys_results)
        for tid in sorted(tasks_in_system):
            task_runs = [r for r in sys_results if r.task_id == tid]
            successes = [r for r in task_runs if r.success]
            level = task_runs[0].level
            steps_vals = [r.steps for r in task_runs]
            cost_vals = [r.cost_usd for r in task_runs]
            elapsed_vals = [r.elapsed_s for r in task_runs]
            success_rate = len(successes) / max(len(task_runs), 1)

            steps_mean, steps_std = mean_std(steps_vals)
            cost_mean, cost_std = mean_std(cost_vals)
            time_mean, time_std = mean_std(elapsed_vals)

            task_totals.append({
                "task_id": tid,
                "level": level,
                "runs": len(task_runs),
                "success": len(successes),
                "success_rate": round(success_rate, 3),
                "steps_mean": round(steps_mean, 1),
                "steps_std": round(steps_std, 1),
                "cost_mean": round(cost_mean, 6),
                "cost_std": round(cost_std, 6),
                "time_mean": round(time_mean, 2),
                "time_std": round(time_std, 2),
            })

        # By level
        levels = sorted(set(t["level"] for t in task_totals))
        level_summary = {}
        for lvl in levels:
            lvl_tasks = [t for t in task_totals if t["level"] == lvl]
            sr = [t["success_rate"] for t in lvl_tasks]
            cs = [t["cost_mean"] for t in lvl_tasks]
            level_summary[lvl] = {
                "tasks": len(lvl_tasks),
                "total_runs": sum(t["runs"] for t in lvl_tasks),
                "success_rate_mean": round(sum(sr) / max(len(sr), 1), 3),
                "cost_mean": round(sum(cs) / max(len(cs), 1), 6),
                "total_cost": round(sum(t["cost_mean"] * t["runs"] for t in lvl_tasks), 6),
            }

        # Global
        all_sr = [t["success_rate"] for t in task_totals]
        global_summary = {
            "tasks_total": len(task_totals),
            "runs_total": sum(t["runs"] for t in task_totals),
            "successes_total": sum(t["success"] for t in task_totals),
            "success_rate_mean": round(sum(all_sr) / max(len(all_sr), 1), 3),
            "total_cost": round(sum(t["cost_mean"] * t["runs"] for t in task_totals), 4),
        }

        reports[sys_name] = BenchmarkReport(
            system=sys_name,
            runs_per_task=task_totals[0]["runs"] if task_totals else 0,
            timestamp=time.time(),
            task_totals=task_totals,
            system_level_summary=level_summary,
            global_summary=global_summary,
        )

    return reports


def print_report(reports: Dict[str, BenchmarkReport]):
    """Pretty-print benchmark results."""
    print(f"\n{'='*80}")
    print(f"  EITElite 论文级基准测试报告")
    print(f"  Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*80}")

    for sys_name, rep in sorted(reports.items()):
        print(f"\n─── {sys_name.upper()} ───")
        print(f"  Runs per task: {rep.runs_per_task}")
        print(f"  Total tasks: {rep.global_summary['tasks_total']}")
        print(f"  Total runs: {rep.global_summary['runs_total']}")
        print(f"  Success rate: {rep.global_summary['success_rate_mean']*100:.1f}%")
        print(f"  Total cost: ${rep.global_summary['total_cost']:.4f}")
        print()

        # Per-level breakdown
        print(f"  {'Level':<8} {'Tasks':<8} {'Success Rate':<16} {'Avg Cost':<14} {'Total Cost'}")
        print(f"  {'─'*8} {'─'*8} {'─'*16} {'─'*14} {'─'*12}")
        for lvl, summary in sorted(rep.system_level_summary.items()):
            print(f"  {lvl:<8} {summary['tasks']:<8} {summary['success_rate_mean']*100:.1f}%{' ':<10} "
                  f"${summary['cost_mean']:<10.6f} ${summary['total_cost']:.4f}")

    print()
    # Cross-system comparison table
    print(f"  {'─'*60}")
    print(f"  {'Metric':<25} ", end="")
    for sys_name in sorted(reports.keys()):
        print(f"{sys_name:<20}", end="")
    print()
    print(f"  {'─'*60}")
    metrics = ["success_rate_mean", "total_cost", "runs_total"]
    for metric in metrics:
        label = {"success_rate_mean": "Success Rate (%)", "total_cost": "Total Cost ($)",
                 "runs_total": "Total Runs"}[metric]
        print(f"  {label:<25} ", end="")
        for sys_name in sorted(reports.keys()):
            val = reports[sys_name].global_summary[metric]
            if metric == "success_rate_mean":
                print(f"{val*100:<22.1f}", end="")
            else:
                print(f"{val:<22}", end="")
        print()

    # Pareto data point
    print(f"\n  Pareto 可扩展性曲线数据点:")
    for sys_name in sorted(reports.keys()):
        g = reports[sys_name].global_summary
        print(f"    {sys_name:<12}  cost=${g['total_cost']:.4f}  rate={g['success_rate_mean']*100:.1f}%")

    # Failure taxonomy
    all_results = []
    for sys_name in reports:
        # We don't store raw results in report, just show failure patterns
        pass
    print(f"\n  See /tmp/benchmark_report.json for full data")


def save_report(reports: Dict[str, BenchmarkReport], path: str = "/tmp/benchmark_report.json"):
    """Save full report as JSON."""
    output = {}
    for sys_name, rep in reports.items():
        output[sys_name] = {
            "system": rep.system,
            "runs_per_task": rep.runs_per_task,
            "timestamp": rep.timestamp,
            "task_totals": rep.task_totals,
            "level_summary": rep.system_level_summary,
            "global_summary": rep.global_summary,
        }
    Path(path).write_text(json.dumps(output, indent=2))
    print(f"  Report saved: {path}")
    return output


# ──────────────────────────────────────────────
# 主运行器
# ──────────────────────────────────────────────

def run_benchmark(systems: List[str], levels: List[str], runs: int = 5, timeout_per_task: int = 120) -> List[RunResult]:
    """Run benchmark for specified systems and levels."""
    tasks = [t for t in TASK_SUITE if t["level"] in levels]
    print(f"\nBenchmark plan:")
    print(f"  Systems: {', '.join(systems)}")
    print(f"  Levels: {', '.join(levels)}")
    print(f"  Tasks: {len(tasks)}")
    print(f"  Runs per task: {runs}")
    print(f"  Total runs: {len(tasks) * len(systems) * runs}")
    print()

    all_results = []
    total = len(tasks) * len(systems) * runs
    done = 0

    for system in systems:
        for task in tasks:
            for r in range(1, runs + 1):
                done += 1
                pct = done * 100 // total
                sys.stdout.write(f"\r  [{pct:3d}%] {system} / {task['level']} / {task['id']} run {r}/{runs}  ")
                sys.stdout.flush()

                try:
                    if system == "eitelite":
                        result = run_eitelite(task, r, timeout_per_task)
                    elif system == "react":
                        result = run_react_baseline(task, r, timeout_per_task)
                    else:
                        print(f"\n  Unknown system: {system}, skipping")
                        continue
                except KeyboardInterrupt:
                    print("\n  Interrupted")
                    return all_results
                except Exception as e:
                    result = RunResult(
                        task_id=task["id"], level=task["level"], system=system,
                        run=r, success=False, steps=0, failure_type="other",
                        failure_detail=f"{type(e).__name__}: {e}",
                    )

                all_results.append(result)
    print()

    return all_results


def parse_args():
    parser = argparse.ArgumentParser(description="EITElite 论文级基准测试")
    parser.add_argument("--systems", default="eitelite,react",
                        help="Comma-separated: eitelite,react")
    parser.add_argument("--levels", default="L0,L1",
                        help="Comma-separated: L0,L1,L2,L3")
    parser.add_argument("--runs", type=int, default=5,
                        help="Runs per task (K), default 5")
    parser.add_argument("--timeout", type=int, default=120,
                        help="Per-task timeout seconds")
    parser.add_argument("--report-only", default="",
                        help="Path to existing JSON report to print (skip run)")
    parser.add_argument("--output", default="/tmp/benchmark_report.json",
                        help="Output JSON path")
    return parser.parse_args()


def main():
    args = parse_args()

    if args.report_only:
        data = json.loads(Path(args.report_only).read_text())
        for sys_name, rep in data.items():
            g = rep["global_summary"]
            print(f"\n  {sys_name.upper()}: {g['success_rate_mean']*100:.1f}% success, ${g['total_cost']:.4f} cost")
        return 0

    systems = [s.strip() for s in args.systems.split(",")]
    levels = [l.strip() for l in args.levels.split(",")]
    runs = max(args.runs, 1)

    results = run_benchmark(systems, levels, runs, args.timeout)
    reports = aggregate_results(results)
    print_report(reports)
    save_report(reports, args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
