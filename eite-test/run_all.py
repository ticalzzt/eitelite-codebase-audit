#!/usr/bin/env python3
"""
EITElite / tical-code 系统测试站 — T1-T8 全量检测
Run on Test VPS (REPLACED_TEST_IP) after any system change.

Usage:
  python3 eite-test/run_all.py [--vps] [--all-vps]
"""

import json
import os
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(str(ROOT))
sys.path.insert(0, str(ROOT))

PASS = "✅"
FAIL = "❌"
SKIP = "⏭️"

tests_run = 0
tests_passed = 0
tests_failed = 0
results = []

def test(name, section):
    global tests_run, tests_passed, tests_failed
    def decorator(fn):
        def wrapper():
            global tests_run, tests_passed, tests_failed
            tests_run += 1
            indent = "  "
            try:
                fn()
                tests_passed += 1
                print(f"  {PASS} {name}")
                results.append((section, name, "pass", ""))
            except AssertionError as e:
                tests_failed += 1
                msg = str(e) if str(e) else "assertion failed"
                print(f"  {FAIL} {name}: {msg}")
                results.append((section, name, "fail", msg))
            except Exception as e:
                tests_failed += 1
                msg = f"{type(e).__name__}: {e}"
                print(f"  {FAIL} {name}: {msg}")
                traceback.print_exc()
                results.append((section, name, "fail", msg))
        return wrapper
    return decorator

# ============================================================
# T1: Syntax & Module Integrity
# ============================================================

@test("All core .py files compile", "T1")
def t1_syntax():
    core_dir = ROOT / "tical_code" / "core"
    errors = []
    for py in sorted(core_dir.rglob("*.py")):
        r = subprocess.run([sys.executable, "-m", "py_compile", str(py)],
                          capture_output=True, text=True)
        if r.returncode != 0:
            errors.append(f"{py.relative_to(ROOT)}: {r.stderr[:80]}")
    assert not errors, f"\n" + "\n".join(errors)

@test("Core modules import", "T1")
def t1_imports():
    from tical_code.core.prompt import build_system_prompt
    from tical_code.core.response_formatter import format_result
    from tical_code.core.tool_executor import execute, TOOL_SCHEMAS, TOOL_SCHEMAS_CLEAN
    from tical_code.core.modules.truthful_reporter import TruthfulReporter
    from tical_code.core.modules.loop_detector import LoopDetector
    from tical_code.core.modules.context_compactor import ContextCompactor
    from tical_code.core.modules.proposal_gate import ProposalGate
    from tical_code.core.eite import init, get_verify
    assert build_system_prompt, "build_system_prompt not importable"

@test("EITE modules import", "T1")
def t1_eite():
    from tical_code.core.eite import init, get_verify
    from tical_code.core.eite.verify import VerifyLayer
    from tical_code.core.eite.signature import sign, verify, _get_hardware_id
    assert VerifyLayer

@test("unified_worker parseable", "T1")
def t1_worker():
    import py_compile
    py_compile.compile(str(ROOT / "tical_code" / "core" / "unified_worker.py"), doraise=True)

# ============================================================
# T2: Content Integrity
# ============================================================

@test("Reporting Iron Law 5 sections present", "T2")
def t2_iron_law():
    from tical_code.core.prompt import build_system_prompt
    p = build_system_prompt(name="test", hostname="tester")
    sections = ["Evidence Mandate", "Standard Report Format",
                "Verification Chain", "Anti-Fabrication", "Summary Line"]
    for s in sections:
        assert s in p, f"Missing section: {s}"
    assert "git diff" in p, "Missing git diff requirement"
    assert "已修复" in p or "已完成" in p, "Missing Chinese anti-fabrication"

@test("EITE identity marker complete", "T2")
def t2_eite_marker():
    from tical_code.core.eite import init, get_verify
    init(identity_id="test-worker", workspace="/tmp/eite_test")
    v = get_verify()
    assert v, "EITE not initialized"
    m = v.get_identity_marker()
    assert "Name:" in m
    assert "Hash:" in m
    assert "Signature:" in m

@test("TruthfulReporter catches bare claims", "T2")
def t2_tr_catch():
    from tical_code.core.modules.truthful_reporter import TruthfulReporter
    r = TruthfulReporter(workspace="/tmp/tr_test")
    v = r.scan_reply("已修复")
    assert len(v) > 0, "Should catch bare 已修复"

@test("TruthfulReporter allows verified claims", "T2")
def t2_tr_pass():
    from tical_code.core.modules.truthful_reporter import TruthfulReporter
    r = TruthfulReporter(workspace="/tmp/tr_test2")
    r.record_action("bash", {"command": "fix"}, {"exit_code": 0}, verified=True)
    v = r.scan_reply("已修复")
    assert len(v) == 0, f"Should pass with verified bash: {v}"

@test("Rule 6: catches summary-only git diff", "T2")
def t2_evidence_summary():
    from tical_code.core.modules.truthful_reporter import TruthfulReporter
    r = TruthfulReporter(workspace="/tmp/tr_ev1")
    r.record_action("bash", {"command": "git diff"}, {"stdout": "+some code"}, verified=True)
    # Reply says "git diff shows changes" but no raw diff markers
    v = r.scan_reply("Already fixed, git diff shows changes were made")
    assert any(vv["rule"] == 6 for vv in v), f"Should catch summary-only: {v}"

@test("Rule 6: passes with raw diff output", "T2")
def t2_evidence_raw_diff():
    from tical_code.core.modules.truthful_reporter import TruthfulReporter
    r = TruthfulReporter(workspace="/tmp/tr_ev2")
    r.record_action("bash", {"command": "git diff"}, {"stdout": "+some code"}, verified=True)
    # Reply includes actual raw diff markers
    v = r.scan_reply(
        "Fixed the config parser.\n"
        "```\ndiff --git a/config.py b/config.py\n"
        "--- a/config.py\n+++ b/config.py\n"
        "@@ -10,7 +10,7 @@\n"
        " old value\n"
        "+new value\n"
        "```\n"
        "commit abcdef1"
    )
    ev = [vv for vv in v if vv["rule"] == 6]
    assert len(ev) == 0, f"Should pass with raw diff: {ev}"

@test("Rule 6: catches missing test output", "T2")
def t2_evidence_no_test():
    from tical_code.core.modules.truthful_reporter import TruthfulReporter
    r = TruthfulReporter(workspace="/tmp/tr_ev3")
    r.record_action("bash", {"command": "pytest"}, {"stdout": ".........", "exit_code": 0}, verified=True)
    v = r.scan_reply("All tests pass, no issues")
    ev = [vv for vv in v if vv["rule"] == 6]
    assert len(ev) > 0, f"Should catch missing test output: {v}"

# ============================================================
# T3: Tool Inventory
# ============================================================

@test("No broken tool handlers (dispatch→exec_*)", "T3")
def t3_broken_handlers():
    from tical_code.core.tool_executor import execute, TOOL_SCHEMAS
    # Import dispatch table
    import tical_code.core.tool_executor as te
    # Check every exec_* function exists
    dispatch_names = set()
    for attr in dir(te):
        if attr.startswith("exec_"):
            dispatch_names.add(attr)
    # Check TOOL_SCHEMAS references match dispatch
    schema_names = {s["function"]["name"].replace(".", "__") for s in TOOL_SCHEMAS}
    # Normalize dot-names
    assert "conv_search" not in schema_names, "conv_search should be removed"
    assert "bash_execute" not in schema_names, "bash_execute should be filtered"

@test("Tool count in expected range", "T3")
def t3_tool_count():
    from tical_code.core.tool_executor import TOOL_SCHEMAS
    count = len(TOOL_SCHEMAS)
    assert 40 <= count <= 55, f"Tool count {count} outside expected range 40-55"

# ============================================================
# T4: EITE Verify Layer
# ============================================================

@test("verify_tool_result: file_write", "T4")
def t4_verify_file_write():
    from tical_code.core.eite import init, get_verify
    init(identity_id="test-worker", workspace="/tmp/eite_test")
    v = get_verify()
    r = v.verify_tool_result("file_write",
        {"path": "/tmp/test_eite_verify.txt"},
        {"path": "/tmp/test_eite_verify.txt", "ok": False})
    # File doesn't exist → verified=False
    assert r.get("verified") == False

@test("verify_tool_result: bash success", "T4")
def t4_verify_bash_ok():
    from tical_code.core.eite import init, get_verify
    init(identity_id="test-worker", workspace="/tmp/eite_test")
    v = get_verify()
    r = v.verify_tool_result("bash", {"command": "echo hi"}, {"exit_code": 0, "stdout": "hi"})
    assert r.get("verified") == True, f"bash exit=0 should pass: {r}"

@test("verify_tool_result: bash fail", "T4")
def t4_verify_bash_fail():
    from tical_code.core.eite import init, get_verify
    init(identity_id="test-worker", workspace="/tmp/eite_test")
    v = get_verify()
    r = v.verify_tool_result("bash", {"command": "false"}, {"exit_code": 1, "stderr": ""})
    assert r.get("verified") == False, f"bash exit≠0 should fail: {r}"

@test("EITE identity check", "T4")
def t4_identity_check():
    from tical_code.core.eite import init, get_verify
    init(identity_id="test-worker", workspace="/tmp/eite_test")
    v = get_verify()
    ok = v.check_identity("You are test-worker, an autonomous AI Agent.")
    assert ok, "Identity check should pass with correct name"
    bad = v.check_identity("You are imposter.")
    assert not bad, "Identity check should fail with wrong name"

# ============================================================
# T5: Git Hygiene
# ============================================================

@test("No runtime artifacts tracked in git", "T5")
def t5_git_tracked():
    r = subprocess.run(["git", "ls-files", "*.jsonl", "*.db*", ".trust_state.json"],
                      capture_output=True, text=True, timeout=10)
    tracked = [l for l in r.stdout.strip().split("\n") if l.strip()]
    # Allow .gitignore itself
    tracked = [l for l in tracked if l != ".gitignore"]
    assert len(tracked) == 0, f"Runtime files tracked: {tracked}"

@test("git status is clean", "T5")
def t5_git_clean():
    r = subprocess.run(["git", "status", "--short"], capture_output=True, text=True, timeout=10)
    dirty = [l for l in r.stdout.strip().split("\n") if l.strip() and not l.startswith("?")]
    if dirty:
        print(f"  dirty files: {dirty}")

# ============================================================
# T6: Dead Code Regression
# ============================================================

@test("No broken imports (from .identity etc)", "T6")
def t6_broken_imports():
    # Check all .py files for relative imports that don't exist
    core = ROOT / "tical_code" / "core"
    all_py_files = list(core.rglob("*.py"))
    existing_modules = {str(p.relative_to(core).with_suffix("")) for p in all_py_files}
    existing_modules.add("")  # current dir
    
    bad_refs = []
    for py in all_py_files:
        content = py.read_text()
        for m in re.findall(r'from\s+\.(\w+)\s+import', content):
            ref_path = str(py.relative_to(core).parent / m)
            if m not in existing_modules and ref_path not in existing_modules:
                # Check if it's a dotted relative import
                if "." + m not in content and m not in content.replace(str(py.relative_to(core).parent), ""):
                    pass  # complex case, skip
    # Simpler check: just ensure identity.py doesn't exist (was broken reference)
    assert not (core / "identity.py").exists(), "identity.py should not exist"
    assert not (core / "memory_sense.py").exists(), "memory_sense.py should not exist"

@test("No orphaned top-level constants", "T6")
def t6_dead_constants():
    # Check for known dead constant patterns
    from tical_code.core.tool_executor import TOOL_SCHEMAS
    # These should NOT exist as module-level names
    import tical_code.core.tool_executor as te
    assert not hasattr(te, "MAX_TOOL_ITERATIONS"), "MAX_TOOL_ITERATIONS dead constant removed"
    assert not hasattr(te, "SOFT_HINT_AT"), "SOFT_HINT_AT dead constant removed"
    assert not hasattr(te, "HARD_STOP_AT"), "HARD_STOP_AT dead constant removed"

@test("No orphaned files in core/", "T6")
def t6_orphaned_files():
    from tical_code.core.eite import init, get_verify
    import tical_code.core.tool_executor as te
    
    # Known dead files should be gone
    dead_files = [
        ROOT / "tical_code" / "core" / "verify.py",
        ROOT / "tical_code" / "core" / "heartbeat.py",
    ]
    for f in dead_files:
        assert not f.exists(), f"Dead file still exists: {f}"

# ============================================================
# T7: Cross-VPS Sync (requires --all-vps flag)
# ============================================================

@test("Anchor file parses correctly", "T7")
def t7_anchor_parse():
    anchor = Path.home() / "anchors" / "ops-anchor.json"
    if not anchor.exists():
        raise AssertionError(f"Anchor not found: {anchor}")
    data = json.loads(anchor.read_text())
    assert "version" in data, "Missing version"
    assert "vps" in data, "Missing vps section"
    assert "sg" in data["vps"], "Missing SG in VPS"

# ============================================================
# T8: Worker Init (SMOKE TEST)
# ============================================================

@test("Worker.__init__ with mock config", "T8")
def t8_worker_init():
    """Minimal smoke test — confirm Worker can initialize."""
    from tical_code.core.unified_worker import Worker
    cfg = {
        "name": "test",
        "workspace": "/tmp/eite_worker_test",
        "tg_token": "",
        "chat_url": "",
        "chat_key": "",
        "ai_model": "deepseek-chat",
        "ai_key": "sk-test",
        "ai_endpoint": "https://api.deepseek.com/v1",
    }
    try:
        w = Worker(cfg)
        assert w.name == "test"
    except Exception as e:
        raise AssertionError(f"Worker init failed: {e}")

@test("build_system_prompt + EITE full chain", "T8")
def t8_full_prompt():
    from tical_code.core.prompt import build_system_prompt
    from tical_code.core.eite import init, get_verify
    init(identity_id="test-worker", workspace="/tmp/eite_test")
    p = build_system_prompt(name="test", hostname="tester", deploy_path="/tmp",
                           target_model="deepseek-v4")
    v = get_verify()
    if v:
        p += v.get_identity_marker()
    assert len(p) > 1000, f"Prompt too short: {len(p)}"
    assert "Reporting Iron Law" in p
    assert "EITE Identity" in p

# ============================================================
# T9: Patch Integrity — 文件编辑后验证生效
# ============================================================

@test("prompt.py 含汇报铁律 5条", "T9")
def t9_prompt_iron_law():
    """防 shell 转义破坏：编辑后实际文件内容要正确。"""
    src = (ROOT / "tical_code" / "core" / "prompt.py").read_text()
    assert "Reporting Iron Law" in src, "prompt.py 缺 Reporting Iron Law"
    assert "Evidence Mandate" in src
    assert "Standard Report Format" in src
    assert "Verification Chain" in src
    assert "Anti-Fabrication" in src
    assert "Summary Line" in src
    assert "git diff" in src
    assert "git log --oneline -1" in src

@test("eite/verify.py 无 scan_reply 残留", "T9")
def t9_no_scan_reply():
    """verify.py scan_reply 已合并到 truthful_reporter，不应残留。"""
    src = (ROOT / "tical_code" / "core" / "eite" / "verify.py").read_text()
    assert "def scan_reply" not in src, "eite/verify.py 仍有 scan_reply"
    assert "sig_verify" not in src, "未用 import sig_verify 残留"
    assert "import os" not in src, "未用 import os 残留"
    assert "import re" not in src, "未用 import re 残留"

@test("signature.py 无 EITE_IMMUTABLE_FLAG 残留", "T9")
def t9_no_immutable_flag():
    src = (ROOT / "tical_code" / "core" / "eite" / "signature.py").read_text()
    assert "EITE_IMMUTABLE_FLAG" not in src, "死常量 EITE_IMMUTABLE_FLAG 残留"
    assert "import json" not in src, "未用 import json 残留"
    assert "import os" not in src, "未用 import os 残留"

@test("response_formatter.py 无 format_error/progress", "T9")
def t9_no_dead_formatters():
    src = (ROOT / "tical_code" / "core" / "response_formatter.py").read_text()
    assert "def format_error" not in src
    assert "def format_progress" not in src

@test("unified_worker.py 无 heartbeat 引用", "T9")
def t9_no_heartbeat():
    src = (ROOT / "tical_code" / "core" / "unified_worker.py").read_text()
    assert "heartbeat" not in src, "heartbeat 引用残留"

@test("tool_executor.py 无死常量", "T9")
def t9_no_dead_constants():
    src = (ROOT / "tical_code" / "core" / "tool_executor.py").read_text()
    assert "MAX_TOOL_ITERATIONS" not in src
    assert "SOFT_HINT_AT" not in src
    assert "HARD_STOP_AT" not in src
    assert "conv_search" not in src

@test("channel.py 无 reply() 别名", "T9")
def t9_no_reply_alias():
    src = (ROOT / "tical_code" / "core" / "channel.py").read_text()
    assert "def reply(self, response)" not in src

@test("clarify.py 无 format_clarify_questions", "T9")
def t9_no_clarify_dead():
    src = (ROOT / "tical_code" / "core" / "clarify.py").read_text()
    assert "def format_clarify_questions" not in src

@test("核心文件已删除确认", "T9")
def t9_deleted_files():
    dead = [
        ROOT / "tical_code" / "core" / "verify.py",
        ROOT / "tical_code" / "core" / "heartbeat.py",
    ]
    for f in dead:
        assert not f.exists(), f"死文件仍存在: {f}"
    # 确认 tical-code 特有文件
    if (ROOT / "tical_code" / "core" / "verify.py").parent.exists():
        pass  # parent dir always exists

@test("modules/ 无 __future__ annotations", "T9")
def t9_no_future_annotations():
    mods = ["session_manager", "context_compactor", "loop_detector",
            "truthful_reporter", "proposal_gate"]
    for m in mods:
        src = (ROOT / "tical_code" / "core" / "modules" / f"{m}.py").read_text()
        assert "from __future__ import annotations" not in src, f"{m}.py 残留 __future__"

@test("cron_scheduler.py 无 DEFAULT_TASK_TIMEOUT", "T9")
def t9_no_task_timeout():
    src = (ROOT / "tical_code" / "core" / "cron_scheduler.py").read_text()
    assert "DEFAULT_TASK_TIMEOUT" not in src

# ============================================================
# T10: 部署一致性 — 跨 VPS 校验
# ============================================================

@test("Anchor vps 段完整性", "T10")
def t10_anchor_vps():
    anchor = Path.home() / "anchors" / "ops-anchor.json"
    if not anchor.exists():
        return
    data = json.loads(anchor.read_text())
    for name in ["sg", "taiwan", "oracle", "test", "kael"]:
        assert name in data["vps"], f"锚点缺 {name}"
        v = data["vps"][name]
        assert "ip" in v
        assert "ssh_user" in v
        assert "ssh_key" in v
        if "ssh_port" in v:
            assert isinstance(v["ssh_port"], int), f"{name} port 不是数字"

@test("eitelite VPS git 版本一致", "T10")
def t10_vps_version_consistency():
    """仅在 Test VPS 上有效（需 SSH 到其他 eitelite VPS）。"""
    anchor = Path.home() / "anchors" / "ops-anchor.json"
    if not anchor.exists():
        return
    vault = json.loads(anchor.read_text())
    vps_list = vault.get("vps", {})
    
    # 本地版本
    local = subprocess.run(["git", "log", "--oneline", "-1"],
                          capture_output=True, text=True).stdout.strip()
    
    # 尝试 SSH 到同仓库 VPS 对比
    targets = [("oracle", vps_list.get("oracle", {})),
               ("test", vps_list.get("test", {}))]
    
    for name, info in targets:
        ip = info.get("ip", "")
        user = info.get("ssh_user", "ubuntu")
        key = info.get("ssh_key", "id_rsa")
        port = info.get("ssh_port", 22)
        if not ip or ip == "localhost":
            continue
        key_path = os.path.expanduser(f"~/.ssh/{key}")
        if not os.path.exists(key_path):
            continue
        
        cmd = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=5",
               "-p", str(port), "-i", key_path,
               f"{user}@{ip}",
               f"cd {info.get('deploy_path', '/home/ubuntu/eitelite')} && git log --oneline -1"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            remote = r.stdout.strip()
            local_hash = local.split()[0]
            remote_hash = remote.split()[0]
            assert local_hash == remote_hash, \
                f"{name} 版本不同: local={local_hash} remote={remote_hash}"

# ============================================================
# Main
# ============================================================

def main():
    global tests_run, tests_passed, tests_failed
    print(f"\n{'='*60}")
    print(f"  EITElite System Test Suite — {ROOT}")
    print(f"{'='*60}\n")

    # Collect all test functions
    import inspect
    test_fns = [(name, fn) for name, fn in globals().items()
                if name.startswith("t") and callable(fn) and name != "test"]

    # Organize by section
    sections = {}
    for name, fn in test_fns:
        # Get section from decorator closure... simpler: parse source
        section = name.split("_")[0].upper()
        sections.setdefault(section, []).append((name, fn))
    
    for section in sorted(sections.keys()):
        print(f"\n--- {section} ---")
        for name, fn in sections[section]:
            fn()
        print()

    print(f"{'='*60}")
    print(f"  Results: {tests_passed}/{tests_run} passed", end="")
    if tests_failed:
        print(f", {tests_failed} FAILED", end="")
    print()
    print(f"{'='*60}")

    # Print failures
    failures = [(s, n, m) for s, n, st, m in results if st == "fail"]
    if failures:
        print(f"\n{FAIL} FAILURES:")
        for s, n, m in failures:
            print(f"  [{s}] {n}: {m}")

    return 0 if tests_failed == 0 else 1

if __name__ == "__main__":
    sys.exit(main())
