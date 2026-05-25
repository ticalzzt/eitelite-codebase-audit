#!/usr/bin/env python3
"""
EITElite 通用基准测试 — 任何 AI Agent 都能跑的标准化 L0-L3 测试
使用方式: 
  1. 导入 TASK_SUITE, EVAL_SCRIPTS
  2. 为每个任务: read_prompt() → agent_work() → run_eval() → record()
  3. 输出 JSON 报告，与 EITElite 数据同格式对比

Hermes Runner 用法:
  python3 eite-test/benchmark_universal.py --agent hermes
  python3 eite-test/benchmark_universal.py --agent hermes --levels L0,L1 --runs 2
"""

import json
import math
import os
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Dict, Tuple

ROOT = Path(__file__).resolve().parent.parent
os.chdir(str(ROOT))
sys.path.insert(0, str(ROOT))

# ──────────────────────────────────────────────
# 从 benchmark.py 导入任务套件
# ──────────────────────────────────────────────
import importlib.util
spec = importlib.util.spec_from_file_location(
    "benchmark_core", str(ROOT / "eite-test" / "benchmark.py"),
    submodule_search_locations=[]
)
bm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bm)

TASK_SUITE = bm.TASK_SUITE
MODEL_PRICING = bm.MODEL_PRICING
compute_cost = bm.compute_cost

# ──────────────────────────────────────────────
# 通用结果数据结构
# ──────────────────────────────────────────────

@dataclass
class RunRecord:
    agent: str           # e.g. "hermes", "eitelite", "react"
    task_id: str
    level: str
    run: int
    success: bool       # eval exit code == 0
    steps: int          # agent tool call count OR manual step count
    elapsed_s: float
    eval_detail: str = ""
    failure_type: str = ""
    tokens_input: int = 0
    tokens_output: int = 0
    cost_usd: float = 0.0

def mean_std(vals: List[float]) -> Tuple[float, float]:
    n = len(vals)
    if n == 0:
        return 0.0, 0.0
    m = sum(vals) / n
    v = sum((x - m) ** 2 for x in vals) / n
    return m, math.sqrt(v)

def aggregate_records(records: List[RunRecord]) -> Dict:
    """Aggregate records into same format as benchmark.py report."""
    agents = set(r.agent for r in records)
    output = {}
    for ag in sorted(agents):
        ag_recs = [r for r in records if r.agent == ag]
        task_ids = sorted(set(r.task_id for r in ag_recs))
        task_totals = []
        for tid in task_ids:
            tr = [r for r in ag_recs if r.task_id == tid]
            succ = [r for r in tr if r.success]
            level = tr[0].level
            sr = len(succ) / max(len(tr), 1)
            sm, ss = mean_std([r.steps for r in tr])
            cm, cs = mean_std([r.cost_usd for r in tr])
            tm, ts = mean_std([r.elapsed_s for r in tr])
            task_totals.append({
                "task_id": tid, "level": level, "runs": len(tr),
                "success": len(succ), "success_rate": round(sr, 3),
                "steps_mean": round(sm, 1), "steps_std": round(ss, 1),
                "cost_mean": round(cm, 6), "cost_std": round(cs, 6),
                "time_mean": round(tm, 2), "time_std": round(ts, 2),
            })
        levels = sorted(set(t["level"] for t in task_totals))
        level_summary = {}
        for lvl in levels:
            lt = [t for t in task_totals if t["level"] == lvl]
            sr_l = [t["success_rate"] for t in lt]
            cs_l = [t["cost_mean"] for t in lt]
            level_summary[lvl] = {
                "tasks": len(lt),
                "total_runs": sum(t["runs"] for t in lt),
                "success_rate_mean": round(sum(sr_l) / max(len(sr_l), 1), 3),
                "cost_mean": round(sum(cs_l) / max(len(cs_l), 1), 6),
            }
        all_sr = [t["success_rate"] for t in task_totals]
        global_summary = {
            "tasks_total": len(task_totals),
            "runs_total": sum(t["runs"] for t in task_totals),
            "success_rate_mean": round(sum(all_sr) / max(len(all_sr), 1), 3),
            "total_cost": round(sum(t["cost_mean"] * t["runs"] for t in task_totals), 4),
        }
        output[ag] = {
            "agent": ag, "runs_per_task": task_totals[0]["runs"] if task_totals else 0,
            "timestamp": time.time(),
            "task_totals": task_totals, "level_summary": level_summary,
            "global_summary": global_summary,
        }
    return output

def print_compare(reports: Dict):
    """Print comparison table across agents."""
    agents = sorted(reports.keys())
    print(f"\n{'='*70}")
    print(f"  Agent Benchmark Comparison — {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*70}")
    print()
    for ag in agents:
        g = reports[ag]["global_summary"]
        print(f"  {ag.upper():<12} Runs={g['runs_total']:<4}  Rate={g['success_rate_mean']*100:5.1f}%  Cost=${g['total_cost']:<8.4f}")
    print()
    # Per-task table
    print(f"  {'Task':<20}", end="")
    for ag in agents:
        print(f"{ag:<20}", end="")
    print()
    print(f"  {'─'*20}", end="")
    for _ in agents:
        print(f"{'─'*20}", end="")
    print()
    first_agent = agents[0]
    for task in reports[first_agent]["task_totals"]:
        tid = task["task_id"]
        print(f"  {tid:<20}", end="")
        for ag in agents:
            ag_tasks = reports[ag]["task_totals"]
            match = [t for t in ag_tasks if t["task_id"] == tid]
            if match:
                t = match[0]
                sr = t["success_rate"] * 100
                print(f"{sr:>5.1f}% {t['steps_mean']:>4.1f}s {'':<5}", end="")
            else:
                print(f"{'N/A':<20}", end="")
        print()
    print()

def save_records(records: List[RunRecord], path: str = "/tmp/benchmark_universal_report.json"):
    """Save records to JSON."""
    reports = aggregate_records(records)
    Path(path).write_text(json.dumps(reports, indent=2))
    # Also save raw records
    raw = []
    for r in records:
        d = {k: v for k, v in r.__dict__.items()}
        d.pop("eval_detail", None)
        raw.append(d)
    raw_path = path.replace(".json", "_raw.json")
    Path(raw_path).write_text(json.dumps(raw, indent=2))
    print(f"  Report: {path}")
    print(f"  Raw:    {raw_path}")
    return reports


# ──────────────────────────────────────────────
# Agent Driver Interface — 任何agent实现此接口
# ──────────────────────────────────────────────

class AgentDriver:
    """Base class for agent benchmark drivers."""
    def name(self) -> str:
        return "abstract"
    def run_task(self, task: Dict, run_num: int) -> RunRecord:
        raise NotImplementedError


# ──────────────────────────────────────────────
# Hermes Driver — 用 Hermes 工具直接执行
# ──────────────────────────────────────────────

class HermesDriver(AgentDriver):
    """
    Hermes 作为被测agent。会调用 file_write/terminal 等工具完成每个任务。
    注意: Hermes driver 的运行由 Hermes 本身负责,
    benchmark_universal.py 只做结果记录和评估。
    """
    def __init__(self):
        self._agent_name = "hermes"
    
    def name(self) -> str:
        return self._agent_name

    def run_task(self, task: Dict, run_num: int) -> RunRecord:
        """
        Hermes 执行单个任务。
        调用方(Hermes agent)会在执行前后封装此调用。
        """
        start = time.time()
        record = RunRecord(
            agent="hermes",
            task_id=task["id"],
            level=task["level"],
            run=run_num,
            success=False,
            steps=0,
            elapsed_s=0,
        )
        try:
            # Hermes 执行任务 (实际由外部agent完成)
            # benchmark 框架只负责 eval
            pass
        except Exception as e:
            record.eval_detail = str(e)
        return record


# ──────────────────────────────────────────────
# Hermes 手工测试 Runner (Hermes 自己调用此函数)
# ──────────────────────────────────────────────

def run_hermes_benchmark(levels: List[str], runs: int = 3):
    """
    Hermes 基准测试入口。
    Hermes agent 调用此函数，对每个任务：
    1. 读取 prompt
    2. 用自身工具完成
    3. 运行 eval
    4. 记录结果
    """
    tasks = [t for t in TASK_SUITE if t["level"] in levels]
    print(f"\nHermes Benchmark Plan:")
    print(f"  Levels: {', '.join(levels)}")
    print(f"  Tasks: {len(tasks)}")
    print(f"  Runs: {runs}")
    print(f"  Total: {len(tasks) * runs} iterations")
    
    records = []
    
    for task in tasks:
        prompt = task["prompt"]
        eval_cmd = task["eval"]
        level = task["level"]
        task_id = task["id"]
        timeout = task.get("timeout", 60)

        for r in range(1, runs + 1):
            print(f"\n{'─'*60}")
            print(f"  [{level}/{task_id}] Run {r}/{runs}")
            print(f"  Prompt: {prompt[:120]}...")
            print(f"{'─'*60}")
            
            start = time.time()
            steps = 0
            success = False
            detail = ""
            failure = ""

            try:
                # === Hermes 执行阶段 ===
                # 1. 读取 prompt
                # 2. 使用 Hermes 工具完成任务
                # 3. 运行 eval
                
                steps += 1  # At minimum 1 step = reading the prompt
                
                # 运行 eval (使用临时脚本避免shell引号问题)
                import tempfile
                with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False, prefix="hermes_eval_") as ef:
                    ef.write("#!/bin/bash\nset -e\n" + eval_cmd + "\n")
                    ef_path = ef.name
                os.chmod(ef_path, 0o755)
                eval_result = subprocess.run(
                    ["bash", ef_path],
                    capture_output=True, text=True, timeout=timeout
                )
                os.unlink(ef_path)
                
                success = (eval_result.returncode == 0)
                if not success:
                    failure = "other"
                    detail = (eval_result.stderr or eval_result.stdout or "")[:200]

            except subprocess.TimeoutExpired:
                failure = "timeout"
                detail = f"Exceeded {timeout}s"
            except Exception as e:
                failure = "other"
                detail = f"{type(e).__name__}: {e}"

            elapsed = round(time.time() - start, 2)
            
            record = RunRecord(
                agent="hermes",
                task_id=task_id,
                level=level,
                run=r,
                success=success,
                steps=steps,
                elapsed_s=elapsed,
                eval_detail=detail,
                failure_type=failure,
            )
            records.append(record)
            
            status = "✅ PASS" if success else "❌ FAIL"
            print(f"  [{r}/{runs}] {status} ({elapsed}s, {steps} steps)")
            if not success and detail:
                print(f"  Detail: {detail[:100]}")

    # 聚合报告
    reports = aggregate_records(records)
    save_records(records)
    print_compare(reports)
    
    return records


# ──────────────────────────────────────────────
# Hermes 步骤追踪辅助 (Hermes 在每次工具调用后调用)
# ──────────────────────────────────────────────

_hermes_step_count = 0
_hermes_start_time = 0
_current_task = None

def hermes_task_begin(task_id: str):
    """Hermes 在开始执行任务前调用."""
    global _hermes_step_count, _hermes_start_time, _current_task
    _hermes_step_count = 0
    _hermes_start_time = time.time()
    _current_task = task_id

def hermes_step():
    """Hermes 每次工具调用后调用."""
    global _hermes_step_count
    _hermes_step_count += 1

def hermes_task_end(success: bool) -> Dict:
    """Hermes 完成任务后调用. 返回记录数据."""
    global _hermes_step_count, _hermes_start_time, _current_task
    elapsed = round(time.time() - _hermes_start_time, 2)
    result = {
        "task_id": _current_task,
        "steps": _hermes_step_count,
        "elapsed_s": elapsed,
        "success": success,
    }
    _current_task = None
    return result


# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Universal Agent Benchmark")
    parser.add_argument("--agent", default="hermes", help="Agent to benchmark")
    parser.add_argument("--levels", default="L0,L1", help="Levels to run")
    parser.add_argument("--runs", type=int, default=3, help="Runs per task")
    args = parser.parse_args()

    levels = [l.strip() for l in args.levels.split(",")]

    if args.agent == "hermes":
        run_hermes_benchmark(levels, args.runs)
    else:
        print(f"Unknown agent: {args.agent}")
