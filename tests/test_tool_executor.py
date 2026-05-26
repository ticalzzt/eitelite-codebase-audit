"""Tests for tool_executor safety checks."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

def test_bash_safety_check_exists():
    from tical_code.core.tool_executor import _bash_safety_check
    assert callable(_bash_safety_check)

def test_safe_command_passes():
    from tical_code.core.tool_executor import _bash_safety_check
    r = _bash_safety_check("echo hello world")
    assert r is None, f"safe command blocked: {r}"

def test_dangerous_command_blocked():
    from tical_code.core.tool_executor import _bash_safety_check
    r = _bash_safety_check("rm -rf /")
    assert r is not None, f"dangerous command not blocked"

def test_dev_null_not_blocked():
    from tical_code.core.tool_executor import _bash_safety_check
    r = _bash_safety_check("find /tmp -name test 2>/dev/null")
    assert r is None, f"2>/dev/null blocked: {r}"
