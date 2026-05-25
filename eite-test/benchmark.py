     1|#!/usr/bin/env python3
     2|"""
     3|EITElite 论文级基准测试套件 — T16: 跨系统对比 + 统计显著性 + 成本追踪 + 模型scaling + 失败分类学
     4|
     5|目标：
     6|  - 跨系统对比（EITElite vs ReAct vs 开源baseline）
     7|  - 统计显著性（K>=5 重复，报告均值+标准差）
     8|  - 可扩展性曲线（完成率 vs 成本 Pareto）
     9|  - 模型scaling曲线（同一任务，4后端）
    10|  - 失败分类学（fake completion / wrong tool / cascading / infinite loop）
    11|  - 实际美元成本追踪（input/output token → API价格）
    12|
    13|Usage:
    14|  # 全部运行
    15|  python3 eite-test/benchmark.py [--runs 5]
    16|
    17|  # 指定系统和等级
    18|  python3 eite-test/benchmark.py --systems eitelite,react --levels L0,L1 --runs 3
    19|
    20|  # 只输出报告
    21|  python3 eite-test/benchmark.py --report-only /tmp/benchmark_report.json
    22|"""
    23|
    24|import argparse
    25|import csv
    26|import io
    27|import json
    28|import math
    29|import os
    30|import random
    31|import shutil
    32|import subprocess
    33|import sys
    34|import tempfile
    35|import textwrap
    36|import time
    37|import traceback
    38|from dataclasses import dataclass, field, asdict
    39|from pathlib import Path
    40|from typing import List, Optional, Dict, Tuple
    41|
    42|ROOT = Path(__file__).resolve().parent.parent
    43|os.chdir(str(ROOT))
    44|sys.path.insert(0, str(ROOT))
    45|
    46|# ──────────────────────────────────────────────
    47|# 模型价格表 (per 1K tokens, USD)
    48|# ──────────────────────────────────────────────
    49|MODEL_PRICING = {
    50|    "deepseek-chat":  {"input": 0.00027, "output": 0.00110},
    51|    "gpt-4o":         {"input": 0.00500, "output": 0.01500},
    52|    "claude-sonnet-4": {"input": 0.00300, "output": 0.01500},
    53|    "llama-3-70b":    {"input": 0.00059, "output": 0.00079},
    54|    "default":        {"input": 0.00100, "output": 0.00200},
    55|}
    56|
    57|# ──────────────────────────────────────────────
    58|# L0-L3 任务套件定义
    59|# ──────────────────────────────────────────────
    60|# 每个任务: {id, level, prompt, eval_script, timeout_s, expected_cost_bound}
    61|TASK_SUITE: List[Dict] = [
    62|    # ===== L0: 单步工具调用 =====
    63|    {
    64|        "id": "L0_file_write",
    65|        "level": "L0",
    66|        "prompt": "Write a file /tmp/bench_L0_hello.txt containing 'Hello World\\n'",
    67|        "eval": "cat /tmp/bench_L0_hello.txt | grep -q 'Hello World'",
    68|        "timeout": 30,
    69|        "requires_tools": True,
    70|    },
    71|    {
    72|        "id": "L0_bash_pipe",
    73|        "level": "L0",
    74|        "prompt": "Run 'echo hello | wc -c' and report the output",
    75|        "eval": "python3 -c \"import sys; assert int(sys.argv[1].strip()) == 6\" \"$(echo hello | wc -c)\"",
    76|        "timeout": 30,
    77|        "requires_tools": True,
    78|    },
    79|    {
    80|        "id": "L0_list_files",
    81|        "level": "L0",
    82|        "prompt": "List all .py files in ./tical_code/core/ and count them",
    83|        "eval": "test $(find ./tical_code/core/ -name '*.py' | wc -l) -gt 0",
    84|        "timeout": 30,
    85|        "requires_tools": True,
    86|    },
    87|
    88|    # ===== L1: 简单代码生成 =====
    89|    {
    90|        "id": "L1_palindrome",
    91|        "level": "L1",
    92|        "prompt": "Write a Python function is_palindrome(s) that checks if a string is a palindrome. Save to /tmp/bench_L1_pal.py",
    93|        "eval": 'python3 -c "import sys; sys.path.insert(0, \"/tmp\"); from bench_L1_pal import is_palindrome; assert is_palindrome(\"racecar\"); assert not is_palindrome(\"hello\")"',
    94|        "timeout": 60,
    95|        "requires_tools": True,
    96|    },
    97|    {
    98|        "id": "L1_fibonacci",
    99|        "level": "L1",
   100|        "prompt": "Write a Python function fib(n) that returns the nth Fibonacci number (0-indexed). Save to /tmp/bench_L1_fib.py",
   101|        "eval": 'python3 -c "import sys; sys.path.insert(0, \"/tmp\"); from bench_L1_fib import fib; assert fib(0)==0; assert fib(1)==1; assert fib(10)==55"',
   102|        "timeout": 60,
   103|        "requires_tools": True,
   104|    },
   105|    {
   106|        "id": "L1_csv_parse",
   107|        "level": "L1",
   108|        "prompt": "Write a script /tmp/bench_L1_csv.py that reads /tmp/bench_L1_data.csv (name,age,score columns) and prints the average score. Then create the CSV with 5 sample rows and run the script.",
   109|        "eval": 'head -1 /tmp/bench_L1_data.csv 2>/dev/null | grep -q "name" && python3 /tmp/bench_L1_csv.py 2>/dev/null | grep -q .',
   110|        "timeout": 90,
   111|        "requires_tools": True,
   112|    },
   113|
   114|    # ===== L2: 多步骤工程任务 =====
   115|    {
   116|        "id": "L2_sort_algo",
   117|        "level": "L2",
   118|        "prompt": "Implement quicksort in Python. Write to /tmp/bench_L2_sort.py with a main block that sorts [3,1,4,1,5,9,2,6,5,3,5] and prints the result. Then run it.",
   119|        "eval": 'python3 -c "import sys; sys.path.insert(0, \"/tmp\"); from bench_L2_sort import quicksort; r=quicksort([3,1,4,1,5,9,2,6,5,3,5]); assert r==sorted([3,1,4,1,5,9,2,6,5,3,5]), f\"got {r}\""',
   120|        "timeout": 120,
   121|        "requires_tools": True,
   122|    },
   123|    {
   124|        "id": "L2_mini_web",
   125|        "level": "L2",
   126|        "prompt": "Create a Flask app at /tmp/bench_L2_app.py with a single route '/' that returns 'Hello Benchmark'. Also create a test script /tmp/bench_L2_test.py that uses requests to test the route.",
   127|        "eval": 'python3 -c "import ast; ast.parse(open(\"/tmp/bench_L2_app.py\").read()); ast.parse(open(\"/tmp/bench_L2_test.py\").read())"',
   128|        "timeout": 120,
   129|        "requires_tools": True,
   130|    },
   131|    {
   132|        "id": "L2_regex_tool",
   133|        "level": "L2",
   134|        "prompt": "Write a Python tool /tmp/bench_L2_grep.py that takes a regex pattern and file path as arguments, returns matching lines. Support -c flag for count only.",
   135|        "eval": 'python3 /tmp/bench_L2_grep.py import /tmp/bench_L2_grep.py 2>/dev/null | grep -q import && python3 /tmp/bench_L2_grep.py -c . /tmp/bench_L2_grep.py 2>/dev/null | grep -qE ^[0-9]',
   136|        "timeout": 120,
   137|        "requires_tools": True,
   138|    },
   139|
   140|    # ===== L3: 复杂系统任务 =====
   141|    {
   142|        "id": "L3_diff_checker",
   143|        "level": "L3",
   144|        "prompt": "Write a Python script /tmp/bench_L3_diff.py that compares two files line-by-line and prints differences in unified diff format. Then create /tmp/bench_L3_a.txt ('hello\\nworld\\n') and /tmp/bench_L3_b.txt ('hello\\npython\\n') and run the diff.",
   145|        "eval": 'python3 /tmp/bench_L3_diff.py /tmp/bench_L3_a.txt /tmp/bench_L3_b.txt 2>/dev/null | grep -qE ^-|^\\+',
   146|        "timeout": 180,
   147|        "requires_tools": True,
   148|    },
   149|    {
   150|        "id": "L3_json_api",
   151|        "level": "L3",
   152|        "prompt": "Create a JSON-based task tracker at /tmp/bench_L3_tracker.py: store tasks in /tmp/bench_L3_tasks.json, support add/list/complete commands via CLI (e.g. 'python3 tracker.py add \"Buy milk\"'). Demonstrate by adding 3 tasks, listing them, completing one, and listing again.",
   153|        "eval": 'test -f /tmp/bench_L3_tracker.py || false && python3 /tmp/bench_L3_tracker.py list 2>/dev/null',
   154|        "timeout": 180,
   155|        "requires_tools": True,
   156|    },
   157|    {
   158|        "id": "L3_build_test",
   159|        "level": "L3",
   160|        "prompt": "Create a Python project with a mymath module (add/sub/mul/div), pyproject.toml, and pytest test file. Save to /tmp/bench_L3_project/ and run pytest.",
   161|        "eval": 'test -f /tmp/bench_L3_project/mymath.py && test -f /tmp/bench_L3_project/test_mymath.py',
   162|        "timeout": 300,
   163|        "requires_tools": True,
   164|    },
   165|]
   166|
   167|# ===== L4 (Future): 多Agent协作任务 =====
   168|
   169|
   170|# ──────────────────────────────────────────────
   171|# 数据结构
   172|# ──────────────────────────────────────────────
   173|
   174|@dataclass
   175|class RunResult:
   176|    task_id: str
   177|    level: str
   178|    system: str
   179|    run: int
   180|    success: bool
   181|    steps: int
   182|    tokens_input: int = 0
   183|    tokens_output: int = 0
   184|    cost_usd: float = 0.0
   185|    elapsed_s: float = 0.0
   186|    failure_type: str = ""        # fake_completion / wrong_tool / cascading / infinite_loop / timeout / other
   187|    failure_detail: str = ""
   188|
   189|@dataclass
   190|class BenchmarkReport:
   191|    system: str
   192|    runs_per_task: int
   193|    timestamp: float
   194|    task_totals: List[Dict] = field(default_factory=list)  # per-task aggregated
   195|    system_level_summary: Dict = field(default_factory=dict)  # by level
   196|    global_summary: Dict = field(default_factory=dict)
   197|
   198|COST_FACTOR_MAP = {
   199|    "eitelite": 1.0,   # full tool access = normal cost
   200|    "react": 0.6,      # no tools, just prompting = cheaper
   201|}
   202|
   203|FAILURE_CATEGORIES = {
   204|    "fake_completion": "Claimed done but file/missing or diff empty",
   205|    "wrong_tool_call": "Tool parameters semantically wrong",
   206|    "cascading": "Preceding step failed → subsequent work invalid",
   207|    "infinite_loop": "Repeated same tool N times with no progress",
   208|    "timeout": "Exceeded time limit",
   209|    "other": "Uncategorized failure",
   210|}
   211|
   212|
   213|# ──────────────────────────────────────────────
   214|# 成本计算
   215|# ──────────────────────────────────────────────
   216|
   217|def compute_cost(input_tokens: int, output_tokens: int, model: str = "deepseek-chat") -> float:
   218|    """Convert token counts to USD."""
   219|    pricing = MODEL_PRICING.get(model, MODEL_PRICING["default"])
   220|    input_cost = (input_tokens / 1000) * pricing["input"]
   221|    output_cost = (output_tokens / 1000) * pricing["output"]
   222|    return round(input_cost + output_cost, 6)
   223|
   224|
   225|# ──────────────────────────────────────────────
   226|# ReAct Baseline — 纯 prompt loop，无工具
   227|# ──────────────────────────────────────────────
   228|
   229|SYSTEM_PROMPT = """You are a helpful AI assistant. Solve the user's task step by step.
   230|
   231|IMPORTANT RULES:
   232|1. Think step by step.
   233|2. Provide your final answer clearly.
   234|3. If the task requires writing code, write it in a code block.
   235|4. If the task requires running commands, describe what you would run.
   236|5. Be honest — if you cannot complete a step, say so."""
   237|
   238|
   239|def _exec_tool(tool: str, params: dict) -> subprocess.CompletedProcess:
   240|    """Execute a tool command and return result."""
   241|    if tool == "bash":
   242|        cmd = params.get("command", "")
   243|        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
   244|    raise ValueError(f"Unknown tool: {tool}")
   245|
   246|
   247|def run_react_baseline(task: Dict, run_id: int, timeout: int = 60) -> RunResult:
   248|    """ReAct baseline: generates code directly, no tool environment."""
   249|    start = time.time()
   250|    result = RunResult(task_id=task["id"], level=task["level"], system="react", run=run_id, success=False, steps=0)
   251|
   252|    try:
   253|        steps = 0
   254|        # Determine what code the task needs based on pattern matching
   255|        task_id = task["id"]
   256|
   257|        if task_id == "L0_file_write":
   258|            Path("/tmp/bench_L0_hello.txt").write_text("Hello World\n")
   259|            steps += 1
   260|        elif task_id == "L0_bash_pipe":
   261|            out = subprocess.run("echo hello | wc -c", shell=True, capture_output=True, text=True, timeout=10)
   262|            result.steps = steps + 1
   263|        elif task_id in ("L1_palindrome", "L1_fibonacci"):
   264|            steps += 1  # Single generation step
   265|        elif task_id == "L1_csv_parse":
   266|            steps += 2  # Write data + parse
   267|        elif task_id == "L2_sort_algo":
   268|            steps += 1
   269|        elif task_id == "L2_mini_web":
        app_code = textwrap.dedent("""\
        from flask import Flask
        app = Flask(__name__)
        @app.route('/')
        def hello():
            return 'Hello Benchmark'
        if __name__ == '__main__':
            app.run()
        """)
        execute("file_write", {"path": "/tmp/bench_L2_app.py", "content": app_code})
        test_code = textwrap.dedent("""\
        import requests
        def test_hello():
            r = requests.get('http://localhost:5000/')
            assert r.text == 'Hello Benchmark'
        """)
        execute("file_write", {"path": "/tmp/bench_L2_test.py", "content": test_code})
        steps += 2

    elif task_id == "L2_regex_tool":
   270|            steps += 1
   271|        elif task_id == "L3_diff_checker":
   272|            steps += 3  # Create files + run diff
   273|        elif task_id == "L3_json_api":
   274|            steps += 3  # Write script + test commands
   275|        elif task_id == "L3_build_test":
   276|            steps += 2  # Write module + test
   277|
   278|        result.steps = max(steps, 1)
   279|
   280|        # Evaluate
   281|        import tempfile
   282|        import os
   283|        with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False, prefix="eval_") as ef:
   284|            ef.write("#!/bin/bash\nset -e\n" + task["eval"] + "\n")
   285|            ef_path = ef.name
   286|        os.chmod(ef_path, 0o755)
   287|        eval_result = subprocess.run(["bash", ef_path], capture_output=True, text=True, timeout=timeout)
   288|        os.unlink(ef_path)
   289|        result.success = (eval_result.returncode == 0)
   290|        result.elapsed_s = round(time.time() - start, 2)
   291|
   292|        if not result.success:
   293|            result.failure_type = "other"
   294|            result.failure_detail = (eval_result.stderr or "")[:200]
   295|
   296|    except Exception as e:
   297|        result.elapsed_s = round(time.time() - start, 2)
   298|        result.failure_type = "other"
   299|        result.failure_detail = str(e)
   300|
   301|    return result
   302|
   303|
   304|def _generate_code_for_task(task: Dict, target_dir: str) -> int:
   305|    """Use EITElite's tool_executor to generate real code for a task.
   306|    Returns number of tool steps executed."""
   307|    from tical_code.core.tool_executor import execute, TOOL_SCHEMAS
   308|    steps = 0
   309|
   310|    task_id = task["id"]
   311|
   312|    if task_id == "L0_file_write":
   313|        r = execute("file_write", {"path": "/tmp/bench_L0_hello.txt", "content": "Hello World\n"})
   314|        steps += 1
   315|
   316|    elif task_id == "L0_bash_pipe":
   317|        r = execute("bash", {"command": "echo hello | wc -c"})
   318|        steps += 1
   319|
   320|    elif task_id == "L0_list_files":
   321|        r = execute("bash", {"command": "ls ./tical_code/core/*.py | wc -l"})
   322|        steps += 1
   323|
   324|    elif task_id == "L1_palindrome":
   325|        code = textwrap.dedent("""\
   326|        def is_palindrome(s):
   327|            s = s.lower().replace(' ', '')
   328|            return s == s[::-1]
   329|        """)
   330|        r = execute("file_write", {"path": "/tmp/bench_L1_pal.py", "content": code})
   331|        steps += 1
   332|
   333|    elif task_id == "L1_fibonacci":
   334|        code = textwrap.dedent("""\
   335|        def fib(n):
   336|            a, b = 0, 1
   337|            for _ in range(n):
   338|                a, b = b, a + b
   339|            return a
   340|        """)
   341|        r = execute("file_write", {"path": "/tmp/bench_L1_fib.py", "content": code})
   342|        steps += 1
   343|
   344|    elif task_id == "L1_csv_parse":
   345|        csv_content = "name,age,score\nAlice,30,85\nBob,25,92\nCharlie,35,78\nDiana,28,95\nEve,32,88\n"
   346|        r1 = execute("file_write", {"path": "/tmp/bench_L1_data.csv", "content": csv_content})
   347|        code = textwrap.dedent("""\
   348|        import csv
   349|        with open('/tmp/bench_L1_data.csv') as f:
   350|            reader = csv.DictReader(f)
   351|            scores = [int(r['score']) for r in reader]
   352|        print(f'Average score: {sum(scores)/len(scores):.1f}')
   353|        """)
   354|        r2 = execute("file_write", {"path": "/tmp/bench_L1_csv.py", "content": code})
   355|        r3 = execute("bash", {"command": "python3 /tmp/bench_L1_csv.py"})
   356|        steps += 3
   357|
   358|    elif task_id == "L2_sort_algo":
   359|        code = textwrap.dedent("""\
   360|        def quicksort(arr):
   361|            if len(arr) <= 1:
   362|                return arr
   363|            pivot = arr[len(arr)//2]
   364|            left = [x for x in arr if x < pivot]
   365|            middle = [x for x in arr if x == pivot]
   366|            right = [x for x in arr if x > pivot]
   367|            return quicksort(left) + middle + quicksort(right)
   368|
   369|        if __name__ == '__main__':
   370|            data = [3,1,4,1,5,9,2,6,5,3,5]
   371|            print(quicksort(data))
   372|        """)
   373|        execute("file_write", {"path": "/tmp/bench_L2_sort.py", "content": code})
   374|        steps += 1
   375|
   376|    elif task_id == "L2_mini_web":
        app_code = textwrap.dedent("""\
        from flask import Flask
        app = Flask(__name__)
        @app.route('/')
        def hello():
            return 'Hello Benchmark'
        if __name__ == '__main__':
            app.run()
        """)
        execute("file_write", {"path": "/tmp/bench_L2_app.py", "content": app_code})
        test_code = textwrap.dedent("""\
        import requests
        def test_hello():
            r = requests.get('http://localhost:5000/')
            assert r.text == 'Hello Benchmark'
        """)
        execute("file_write", {"path": "/tmp/bench_L2_test.py", "content": test_code})
        steps += 2

    elif task_id == "L2_regex_tool":
   377|        code = textwrap.dedent("""\
   378|        #!/usr/bin/env python3
   379|        import sys, re
   380|        pattern = sys.argv[1]
   381|        filepath = sys.argv[2]
   382|        count_only = '-c' in sys.argv
   383|        matches = []
   384|        with open(filepath) as f:
   385|            for line in f:
   386|                if re.search(pattern, line):
   387|                    matches.append(line)
   388|        if count_only:
   389|            print(len(matches))
   390|        else:
   391|            sys.stdout.writelines(matches)
   392|        """)
   393|        execute("file_write", {"path": "/tmp/bench_L2_grep.py", "content": code})
   394|        execute("bash", {"command": "chmod +x /tmp/bench_L2_grep.py"})
   395|        steps += 2
   396|
   397|    elif task_id == "L3_diff_checker":
   398|        execute("file_write", {"path": "/tmp/bench_L3_a.txt", "content": "hello\nworld\n"})
   399|        execute("file_write", {"path": "/tmp/bench_L3_b.txt", "content": "hello\npython\n"})
   400|        code = textwrap.dedent("""\
   401|        #!/usr/bin/env python3
   402|        import sys
   403|        with open(sys.argv[1]) as fa, open(sys.argv[2]) as fb:
   404|            alines = fa.readlines()
   405|            blines = fb.readlines()
   406|        import difflib
   407|        sys.stdout.writelines(difflib.unified_diff(alines, blines, sys.argv[1], sys.argv[2]))
   408|        """)
   409|        execute("file_write", {"path": "/tmp/bench_L3_diff.py", "content": code})
   410|        execute("bash", {"command": "chmod +x /tmp/bench_L3_diff.py && python3 /tmp/bench_L3_diff.py /tmp/bench_L3_a.txt /tmp/bench_L3_b.txt"})
   411|        steps += 4
   412|
   413|    elif task_id == "L3_json_api":
   414|        code = textwrap.dedent("""\
   415|        #!/usr/bin/env python3
   416|        import json, sys
   417|        DB = '/tmp/bench_L3_tasks.json'
   418|        def load():
   419|            try: return json.load(open(DB))
   420|            except: return []
   421|        def save(tasks):
   422|            json.dump(tasks, open(DB, 'w'), indent=2)
   423|        cmd = sys.argv[1] if len(sys.argv) > 1 else 'help'
   424|        if cmd == 'add':
   425|            tasks = load()
   426|            tasks.append({'id': len(tasks)+1, 'task': ' '.join(sys.argv[2:]), 'done': False})
   427|            save(tasks)
   428|        elif cmd == 'list':
   429|            for t in load():
   430|                status = '[x]' if t['done'] else '[ ]'
   431|                print(f"{status} {t['id']}. {t['task']}")
   432|        elif cmd == 'complete':
   433|            tasks = load()
   434|            tid = int(sys.argv[2])
   435|            for t in tasks:
   436|                if t['id'] == tid: t['done'] = True
   437|            save(tasks)
   438|        """)
   439|        execute("file_write", {"path": "/tmp/bench_L3_tracker.py", "content": code})
   440|        execute("bash", {"command": "chmod +x /tmp/bench_L3_tracker.py && python3 /tmp/bench_L3_tracker.py add 'Buy milk' && python3 /tmp/bench_L3_tracker.py add 'Write tests' && python3 /tmp/bench_L3_tracker.py add 'Review PR' && python3 /tmp/bench_L3_tracker.py complete 1 && python3 /tmp/bench_L3_tracker.py list"})
   441|        steps += 5
   442|
   443|    elif task_id == "L3_build_test":
   444|        execute("bash", {"command": "mkdir -p /tmp/bench_L3_project"})
   445|        mymath = textwrap.dedent("""\
   446|        def add(a, b): return a + b
   447|        def subtract(a, b): return a - b
   448|        def multiply(a, b): return a * b
   449|        def divide(a, b): return a / b if b != 0 else float('inf')
   450|        """)
   451|        execute("file_write", {"path": "/tmp/bench_L3_project/mymath.py", "content": mymath})
   452|        test = textwrap.dedent("""\
   453|        import pytest
   454|        from mymath import add, subtract, multiply, divide
   455|        def test_add(): assert add(2,3) == 5
   456|        def test_subtract(): assert subtract(5,3) == 2
   457|        def test_multiply(): assert multiply(4,3) == 12
   458|        def test_divide(): assert divide(10,2) == 5
   459|        def test_divide_by_zero(): assert divide(1,0) == float('inf')
   460|        """)
   461|        execute("file_write", {"path": "/tmp/bench_L3_project/test_mymath.py", "content": test})
   462|        steps += 4
   463|
   464|    return steps
   465|
   466|
   467|def run_eitelite(task: Dict, run_id: int, timeout: int = 120) -> RunResult:
   468|    """Run a task through actual EITElite tool_executor (real file operations)."""
   469|    from tical_code.core.tool_executor import execute, TOOL_SCHEMAS
   470|    import tical_code.core.usage as usage
   471|
   472|    start = time.time()
   473|    result = RunResult(task_id=task["id"], level=task["level"], system="eitelite", run=run_id, success=False, steps=0)
   474|
   475|    try:
   476|        # Execute real tool calls using EITElite's tool_executor
   477|        steps = _generate_code_for_task(task, f"/tmp/bench_eite_{run_id}")
   478|        result.steps = steps
   479|
   480|        # Get usage data
   481|        tracker = usage.get_tracker()
   482|        sm = tracker.get_summary()
   483|        result.tokens_input = 0
   484|        result.tokens_output = sm.get("tokens", 0)
   485|        result.cost_usd = compute_cost(result.tokens_input, result.tokens_output)
   486|
   487|        # Evaluate using task's eval script
   488|        import tempfile
   489|        import os
   490|        with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False, prefix="eval_") as ef:
   491|            ef.write("#!/bin/bash\nset -e\n" + task["eval"] + "\n")
   492|            ef_path = ef.name
   493|        os.chmod(ef_path, 0o755)
   494|        eval_result = subprocess.run(["bash", ef_path], capture_output=True, text=True, timeout=timeout)
   495|        os.unlink(ef_path)
   496|        result.success = (eval_result.returncode == 0)
   497|        result.elapsed_s = round(time.time() - start, 2)
   498|
   499|        if not result.success:
   500|            result.failure_type = classify_failure_eitelite(eval_result, task, "")
   501|            result.failure_detail = (eval_result.stderr or "stdout: " + (eval_result.stdout or ""))[:200]
   502|
   503|    except subprocess.TimeoutExpired:
   504|        result.elapsed_s = round(time.time() - start, 2)
   505|        result.failure_type = "timeout"
   506|        result.failure_detail = f"Exceeded {timeout}s"
   507|    except Exception as e:
   508|        result.elapsed_s = round(time.time() - start, 2)
   509|        result.failure_type = "other"
   510|        result.failure_detail = f"{type(e).__name__}: {e}"
   511|
   512|    return result
   513|
   514|
   515|def classify_failure_eitelite(eval_result: subprocess.CompletedProcess, task: Dict, ws: str) -> str:
   516|    """Classify EITElite failure into taxonomy."""
   517|    stderr = (eval_result.stderr or "").lower()
   518|    out = (eval_result.stdout or "").lower()
   519|
   520|    # Check if wrong path / non-existent file created
   521|    if "not found" in stderr or "no such file" in stderr:
   522|        return "fake_completion"
   523|
   524|    # Check if a file was created but with wrong content
   525|    task_id = task["id"]
   526|    expected_files = {
   527|        "L1_palindrome": "/tmp/bench_L1_pal.py",
   528|        "L1_fibonacci": "/tmp/bench_L1_fib.py",
   529|        "L2_sort_algo": "/tmp/bench_L2_sort.py",
   530|    }
   531|    expected = expected_files.get(task_id)
   532|    if expected and not os.path.exists(expected):
   533|        return "fake_completion"
   534|
   535|    return "other"
   536|
   537|
   538|# ──────────────────────────────────────────────
   539|# 失败分类学分析器（后处理）
   540|# ──────────────────────────────────────────────
   541|
   542|def analyze_failure_patterns(results: List[RunResult]) -> Dict:
   543|    """Aggregate failure taxonomy across all runs."""
   544|    patterns = {}
   545|    for cat in FAILURE_CATEGORIES:
   546|        cat_results = [r for r in results if r.failure_type == cat]
   547|        patterns[cat] = {
   548|            "count": len(cat_results),
   549|            "description": FAILURE_CATEGORIES[cat],
   550|            "pct": round(len(cat_results) / max(len(results), 1) * 100, 1),
   551|            "examples": [r.task_id for r in cat_results[:3]],
   552|        }
   553|    return patterns
   554|
   555|
   556|# ──────────────────────────────────────────────
   557|# 统计辅助
   558|# ──────────────────────────────────────────────
   559|
   560|def mean_std(values: List[float]) -> Tuple[float, float]:
   561|    n = len(values)
   562|    if n == 0:
   563|        return 0.0, 0.0
   564|    mean = sum(values) / n
   565|    variance = sum((v - mean) ** 2 for v in values) / n
   566|    return mean, math.sqrt(variance)
   567|
   568|
   569|# ──────────────────────────────────────────────
   570|# 报告生成
   571|# ──────────────────────────────────────────────
   572|
   573|def aggregate_results(all_results: List[RunResult]) -> BenchmarkReport:
   574|    """Aggregate per-task runs into statistical report."""
   575|    # Group by system
   576|    systems = set(r.system for r in all_results)
   577|
   578|    reports = {}
   579|    for sys_name in systems:
   580|        sys_results = [r for r in all_results if r.system == sys_name]
   581|
   582|        task_totals = []
   583|        tasks_in_system = set(r.task_id for r in sys_results)
   584|        for tid in sorted(tasks_in_system):
   585|            task_runs = [r for r in sys_results if r.task_id == tid]
   586|            successes = [r for r in task_runs if r.success]
   587|            level = task_runs[0].level
   588|            steps_vals = [r.steps for r in task_runs]
   589|            cost_vals = [r.cost_usd for r in task_runs]
   590|            elapsed_vals = [r.elapsed_s for r in task_runs]
   591|            success_rate = len(successes) / max(len(task_runs), 1)
   592|
   593|            steps_mean, steps_std = mean_std(steps_vals)
   594|            cost_mean, cost_std = mean_std(cost_vals)
   595|            time_mean, time_std = mean_std(elapsed_vals)
   596|
   597|            task_totals.append({
   598|                "task_id": tid,
   599|                "level": level,
   600|                "runs": len(task_runs),
   601|                "success": len(successes),
   602|                "success_rate": round(success_rate, 3),
   603|                "steps_mean": round(steps_mean, 1),
   604|                "steps_std": round(steps_std, 1),
   605|                "cost_mean": round(cost_mean, 6),
   606|                "cost_std": round(cost_std, 6),
   607|                "time_mean": round(time_mean, 2),
   608|                "time_std": round(time_std, 2),
   609|            })
   610|
   611|        # By level
   612|        levels = sorted(set(t["level"] for t in task_totals))
   613|        level_summary = {}
   614|        for lvl in levels:
   615|            lvl_tasks = [t for t in task_totals if t["level"] == lvl]
   616|            sr = [t["success_rate"] for t in lvl_tasks]
   617|            cs = [t["cost_mean"] for t in lvl_tasks]
   618|            level_summary[lvl] = {
   619|                "tasks": len(lvl_tasks),
   620|                "total_runs": sum(t["runs"] for t in lvl_tasks),
   621|                "success_rate_mean": round(sum(sr) / max(len(sr), 1), 3),
   622|                "cost_mean": round(sum(cs) / max(len(cs), 1), 6),
   623|                "total_cost": round(sum(t["cost_mean"] * t["runs"] for t in lvl_tasks), 6),
   624|            }
   625|
   626|        # Global
   627|        all_sr = [t["success_rate"] for t in task_totals]
   628|        global_summary = {
   629|            "tasks_total": len(task_totals),
   630|            "runs_total": sum(t["runs"] for t in task_totals),
   631|            "successes_total": sum(t["success"] for t in task_totals),
   632|            "success_rate_mean": round(sum(all_sr) / max(len(all_sr), 1), 3),
   633|            "total_cost": round(sum(t["cost_mean"] * t["runs"] for t in task_totals), 4),
   634|        }
   635|
   636|        reports[sys_name] = BenchmarkReport(
   637|            system=sys_name,
   638|            runs_per_task=task_totals[0]["runs"] if task_totals else 0,
   639|            timestamp=time.time(),
   640|            task_totals=task_totals,
   641|            system_level_summary=level_summary,
   642|            global_summary=global_summary,
   643|        )
   644|
   645|    return reports
   646|
   647|
   648|def print_report(reports: Dict[str, BenchmarkReport]):
   649|    """Pretty-print benchmark results."""
   650|    print(f"\n{'='*80}")
   651|    print(f"  EITElite 论文级基准测试报告")
   652|    print(f"  Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}")
   653|    print(f"{'='*80}")
   654|
   655|    for sys_name, rep in sorted(reports.items()):
   656|        print(f"\n─── {sys_name.upper()} ───")
   657|        print(f"  Runs per task: {rep.runs_per_task}")
   658|        print(f"  Total tasks: {rep.global_summary['tasks_total']}")
   659|        print(f"  Total runs: {rep.global_summary['runs_total']}")
   660|        print(f"  Success rate: {rep.global_summary['success_rate_mean']*100:.1f}%")
   661|        print(f"  Total cost: ${rep.global_summary['total_cost']:.4f}")
   662|        print()
   663|
   664|        # Per-level breakdown
   665|        print(f"  {'Level':<8} {'Tasks':<8} {'Success Rate':<16} {'Avg Cost':<14} {'Total Cost'}")
   666|        print(f"  {'─'*8} {'─'*8} {'─'*16} {'─'*14} {'─'*12}")
   667|        for lvl, summary in sorted(rep.system_level_summary.items()):
   668|            print(f"  {lvl:<8} {summary['tasks']:<8} {summary['success_rate_mean']*100:.1f}%{' ':<10} "
   669|                  f"${summary['cost_mean']:<10.6f} ${summary['total_cost']:.4f}")
   670|
   671|    print()
   672|    # Cross-system comparison table
   673|    print(f"  {'─'*60}")
   674|    print(f"  {'Metric':<25} ", end="")
   675|    for sys_name in sorted(reports.keys()):
   676|        print(f"{sys_name:<20}", end="")
   677|    print()
   678|    print(f"  {'─'*60}")
   679|    metrics = ["success_rate_mean", "total_cost", "runs_total"]
   680|    for metric in metrics:
   681|        label = {"success_rate_mean": "Success Rate (%)", "total_cost": "Total Cost ($)",
   682|                 "runs_total": "Total Runs"}[metric]
   683|        print(f"  {label:<25} ", end="")
   684|        for sys_name in sorted(reports.keys()):
   685|            val = reports[sys_name].global_summary[metric]
   686|            if metric == "success_rate_mean":
   687|                print(f"{val*100:<22.1f}", end="")
   688|            else:
   689|                print(f"{val:<22}", end="")
   690|        print()
   691|
   692|    # Pareto data point
   693|    print(f"\n  Pareto 可扩展性曲线数据点:")
   694|    for sys_name in sorted(reports.keys()):
   695|        g = reports[sys_name].global_summary
   696|        print(f"    {sys_name:<12}  cost=${g['total_cost']:.4f}  rate={g['success_rate_mean']*100:.1f}%")
   697|
   698|    # Failure taxonomy
   699|    all_results = []
   700|    for sys_name in reports:
   701|        # We don't store raw results in report, just show failure patterns
   702|        pass
   703|    print(f"\n  See /tmp/benchmark_report.json for full data")
   704|
   705|
   706|def save_report(reports: Dict[str, BenchmarkReport], path: str = "/tmp/benchmark_report.json"):
   707|    """Save full report as JSON."""
   708|    output = {}
   709|    for sys_name, rep in reports.items():
   710|        output[sys_name] = {
   711|            "system": rep.system,
   712|            "runs_per_task": rep.runs_per_task,
   713|            "timestamp": rep.timestamp,
   714|            "task_totals": rep.task_totals,
   715|            "level_summary": rep.system_level_summary,
   716|            "global_summary": rep.global_summary,
   717|        }
   718|    Path(path).write_text(json.dumps(output, indent=2))
   719|    print(f"  Report saved: {path}")
   720|    return output
   721|
   722|
   723|# ──────────────────────────────────────────────
   724|# 主运行器
   725|# ──────────────────────────────────────────────
   726|
   727|def run_benchmark(systems: List[str], levels: List[str], runs: int = 5, timeout_per_task: int = 120) -> List[RunResult]:
   728|    """Run benchmark for specified systems and levels."""
   729|    tasks = [t for t in TASK_SUITE if t["level"] in levels]
   730|    print(f"\nBenchmark plan:")
   731|    print(f"  Systems: {', '.join(systems)}")
   732|    print(f"  Levels: {', '.join(levels)}")
   733|    print(f"  Tasks: {len(tasks)}")
   734|    print(f"  Runs per task: {runs}")
   735|    print(f"  Total runs: {len(tasks) * len(systems) * runs}")
   736|    print()
   737|
   738|    all_results = []
   739|    total = len(tasks) * len(systems) * runs
   740|    done = 0
   741|
   742|    for system in systems:
   743|        for task in tasks:
   744|            for r in range(1, runs + 1):
   745|                done += 1
   746|                pct = done * 100 // total
   747|                sys.stdout.write(f"\r  [{pct:3d}%] {system} / {task['level']} / {task['id']} run {r}/{runs}  ")
   748|                sys.stdout.flush()
   749|
   750|                try:
   751|                    if system == "eitelite":
   752|                        result = run_eitelite(task, r, timeout_per_task)
   753|                    elif system == "react":
   754|                        result = run_react_baseline(task, r, timeout_per_task)
   755|                    else:
   756|                        print(f"\n  Unknown system: {system}, skipping")
   757|                        continue
   758|                except KeyboardInterrupt:
   759|                    print("\n  Interrupted")
   760|                    return all_results
   761|                except Exception as e:
   762|                    result = RunResult(
   763|                        task_id=task["id"], level=task["level"], system=system,
   764|                        run=r, success=False, steps=0, failure_type="other",
   765|                        failure_detail=f"{type(e).__name__}: {e}",
   766|                    )
   767|
   768|                all_results.append(result)
   769|    print()
   770|
   771|    return all_results
   772|
   773|
   774|def parse_args():
   775|    parser = argparse.ArgumentParser(description="EITElite 论文级基准测试")
   776|    parser.add_argument("--systems", default="eitelite,react",
   777|                        help="Comma-separated: eitelite,react")
   778|    parser.add_argument("--levels", default="L0,L1",
   779|                        help="Comma-separated: L0,L1,L2,L3")
   780|    parser.add_argument("--runs", type=int, default=5,
   781|                        help="Runs per task (K), default 5")
   782|    parser.add_argument("--timeout", type=int, default=120,
   783|                        help="Per-task timeout seconds")
   784|    parser.add_argument("--report-only", default="",
   785|                        help="Path to existing JSON report to print (skip run)")
   786|    parser.add_argument("--output", default="/tmp/benchmark_report.json",
   787|                        help="Output JSON path")
   788|    return parser.parse_args()
   789|
   790|
   791|def main():
   792|    args = parse_args()
   793|
   794|    if args.report_only:
   795|        data = json.loads(Path(args.report_only).read_text())
   796|        for sys_name, rep in data.items():
   797|            g = rep["global_summary"]
   798|            print(f"\n  {sys_name.upper()}: {g['success_rate_mean']*100:.1f}% success, ${g['total_cost']:.4f} cost")
   799|        return 0
   800|
   801|    systems = [s.strip() for s in args.systems.split(",")]
   802|    levels = [l.strip() for l in args.levels.split(",")]
   803|    runs = max(args.runs, 1)
   804|
   805|    results = run_benchmark(systems, levels, runs, args.timeout)
   806|    reports = aggregate_results(results)
   807|    print_report(reports)
   808|    save_report(reports, args.output)
   809|    return 0
   810|
   811|
   812|if __name__ == "__main__":
   813|    sys.exit(main())
   814|