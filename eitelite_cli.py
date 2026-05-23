#!/usr/bin/env python3
"""EITElite CLI — worker operations interface.

Usage:
    eitelite-cli status              Show worker health & capabilities
    eitelite-cli log [-n 50]         Tail recent worker logs
    eitelite-cli prompt "..."        Send one-shot prompt via tical-chat
    eitelite-cli restart             Restart the worker service
    eitelite-cli version             Show version info
    eitelite-cli help                Show this help
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


# ── constants ──────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parent
WORKER_SCRIPT = REPO_ROOT / "tical_code" / "core" / "unified_worker.py"

# Auto-detect service name from hostname
_HOSTNAME = os.uname().nodename
if "oracle" in _HOSTNAME or "oracle" in os.environ.get("WORKER_NAME", ""):
    SERVICE = "unified-worker-oracle"
    WORKER_NAME = "tico-oracle"
elif "test" in _HOSTNAME or os.environ.get("WORKER_NAME", "") == "test":
    SERVICE = "unified-worker-test"
    WORKER_NAME = "test"
else:
    SERVICE = "unified-worker-ani"
    WORKER_NAME = os.environ.get("WORKER_NAME", "ani")


# ── helpers ────────────────────────────────────────────────────────────────

def _run(cmd: list[str], timeout: int = 10) -> tuple[int, str]:
    """Run a subprocess, return (exit_code, stdout+stderr)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = r.stdout + r.stderr
        return r.returncode, out.strip()
    except subprocess.TimeoutExpired:
        return -1, "(timeout)"
    except FileNotFoundError:
        return -2, f"command not found: {cmd[0]}"


def _detect_service() -> str:
    """Detect the running unified-worker service name."""
    # Try exact name first
    for name in [SERVICE, "unified-worker-ani"]:
        rc, _ = _run(["systemctl", "is-active", name], timeout=3)
        if rc == 0:
            return name
    # Fallback: list active services and find matching
    rc, out = _run(["systemctl", "list-units", "--type=service", "--state=active", "--no-legend"], timeout=5)
    if rc == 0:
        for line in out.split("\n"):
            if "unified-worker" in line:
                return line.split()[0]
    return SERVICE


def _get_worker_info() -> dict:
    """Gather worker status without importing anything heavy."""
    info = {"service": _detect_service(), "worker_name": WORKER_NAME, "hostname": _HOSTNAME}

    # systemd service status
    rc, out = _run(["systemctl", "is-active", info["service"]])
    info["service_active"] = out if rc == 0 else "inactive"

    if info["service_active"] != "active":
        # Try last few journal lines
        rc, out = _run(["journalctl", "-u", info["service"], "-n", "5", "--no-pager", "-o", "cat"], timeout=5)
        info["last_log"] = out[:300] if out else ""
        info["uptime"] = "—"
        info["model"] = "—"
        info["tools"] = 0
        return info

    # Worker PID + uptime
    rc, out = _run(["systemctl", "show", "-p", "MainPID,ActiveEnterTimestamp", info["service"]], timeout=5)
    if rc == 0:
        for line in out.split("\n"):
            if line.startswith("MainPID="):
                info["pid"] = line.split("=", 1)[1]
            elif line.startswith("ActiveEnterTimestamp="):
                info["since"] = line.split("=", 1)[1]

    # Uptime via ps
    if info.get("pid"):
        rc, out = _run(["ps", "-o", "etime=", "-p", info["pid"]], timeout=3)
        if rc == 0:
            info["uptime"] = out.strip()

    # Read latest log for model info
    rc, out = _run(["journalctl", "-u", info["service"], "-n", "30", "--no-pager", "-o", "cat"], timeout=5)
    if rc == 0:
        for line in out.split("\n"):
            if "model=" in line and "backend=" in line:
                m = line.split("model=")
                if len(m) > 1:
                    info["model"] = m[1].split()[0].strip("?,")
                b = line.split("backend=")
                if len(b) > 1:
                    info["backend"] = b[1].split()[0].strip()
            if "channels=" in line:
                c = line.split("channels=")
                if len(c) > 1:
                    info["channels"] = c[1].split()[0].strip()
            if "prompt_len=" in line:
                p = line.split("prompt_len=")
                if len(p) > 1:
                    info["prompt_len"] = p[1].split()[0].strip()

    # Tool count via direct import (lightweight)
    try:
        sys.path.insert(0, str(REPO_ROOT))
        from tical_code.core.tool_executor import TOOL_SCHEMAS
        info["tools"] = len(TOOL_SCHEMAS)
    except Exception:
        info["tools"] = "?"

    # System resources
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    kb = int(line.split()[1])
                    info["memory_gb"] = f"{kb / 1024 / 1024:.1f}GB"
                    break
        rc, out = _run(["nproc"])
        if rc == 0:
            info["cpus"] = out.strip()
        rc, out = _run(["uptime", "-p"])
        if rc == 0:
            info["system_uptime"] = out.strip()
    except Exception:
        pass

    # Git info
    rc, out = _run(["git", "-C", str(REPO_ROOT), "log", "--oneline", "-1"])
    if rc == 0:
        info["commit"] = out.split()[0]
        info["commit_msg"] = " ".join(out.split()[1:])

    return info


# ── commands ───────────────────────────────────────────────────────────────

def cmd_status(args):
    """Show worker status."""
    info = _get_worker_info()

    # Header
    print(f"EITElite Worker  —  {info['hostname']}")
    print(f"{'─' * 50}")

    # Service
    active = info["service_active"]
    icon = "✅" if active == "active" else "❌"
    print(f"  Service:  {icon} {info['service']} ({active})")

    if active == "active":
        print(f"  PID:      {info.get('pid', '?')}")
        print(f"  Uptime:   {info.get('uptime', '?')}")

    # Model
    print(f"  Model:    {info.get('model', '?')}")
    print(f"  Backend:  {info.get('backend', '?')}")

    # Tools
    print(f"  Tools:    {info.get('tools', '?')}")
    print(f"  Channels: {info.get('channels', '?')}")

    # Resources
    print(f"  RAM:      {info.get('memory_gb', '?')}")
    print(f"  CPUs:     {info.get('cpus', '?')}")
    print(f"  System:   {info.get('system_uptime', '?')}")

    # Git
    print(f"  Commit:   {info.get('commit', '?')}")
    msg = info.get("commit_msg", "")
    if msg:
        print(f"           {msg[:60]}")

    # Last log on failure
    if active != "active" and info.get("last_log"):
        print(f"\n  Last log snippet:")
        for line in info["last_log"].split("\n")[-3:]:
            if line.strip():
                print(f"    {line.strip()[:100]}")

    return 0 if active == "active" else 1


def cmd_log(args):
    """Tail worker logs."""
    n = args.n if hasattr(args, "n") and args.n else 50
    rc, out = _run(["journalctl", "-u", SERVICE, "-n", str(n), "--no-pager", "-o", "cat"], timeout=5)
    if rc != 0 and "oracle" not in SERVICE:
        # Try ani as fallback
        rc, out = _run(["journalctl", "-u", "unified-worker-ani", "-n", str(n), "--no-pager", "-o", "cat"], timeout=5)
    if rc == 0 and out:
        print(out)
        return 0
    print(f"(no logs for {SERVICE})")
    return 1


def cmd_prompt(args):
    """Send a one-shot prompt via tical-chat."""
    text = args.text
    if not text:
        print("Error: prompt text required")
        return 1

    # Find tical-chat endpoint
    urls = ["http://REPLACED_TAIWAN_IP:8080", "http://REPLACED_SG_IP:8080"]
    key = os.environ.get("TICAL_CHAT_KEY", "REPLACED_SHARED_KEY")

    import urllib.request
    payload = json.dumps({
        "sender": WORKER_NAME,
        "target": "seoul",
        "content": text,
    }).encode()

    for url in urls:
        try:
            req = urllib.request.Request(
                f"{url}/v1/messages",
                data=payload,
                headers={
                    "X-AI-Key": key,
                    "X-AI-Identity": WORKER_NAME,
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read())
            if result.get("ok"):
                print(f"✅ Prompt sent via {url}")
                return 0
        except Exception as e:
            continue

    print(f"❌ Failed to send prompt (all endpoints unreachable)")
    return 1


def cmd_restart(args):
    """Restart the worker service."""
    svc = _detect_service()
    print(f"Restarting {svc}...")
    rc, out = _run(["sudo", "systemctl", "restart", svc], timeout=15)
    if rc != 0:
        print(f"❌ Restart failed: {out[:200]}")
        return 1

    time.sleep(2)
    rc, out = _run(["systemctl", "is-active", svc], timeout=5)
    if rc == 0:
        print(f"✅ {svc} is {out}")
        return 0
    print(f"⚠️  {svc} is {out}")
    return 1


def cmd_version(args):
    """Show version info."""
    rc, out = _run(["git", "-C", str(REPO_ROOT), "log", "--oneline", "-1"])
    if rc == 0:
        print(f"EITElite {out}")
    rc, out = _run(["git", "-C", str(REPO_ROOT), "describe", "--tags", "--always", "--dirty"], timeout=5)
    if rc == 0:
        print(f"Tag: {out}")
    print(f"Python: {sys.version.split()[0]}")
    print(f"Host: {_HOSTNAME}")

    try:
        sys.path.insert(0, str(REPO_ROOT))
        from tical_code.core.tool_executor import TOOL_SCHEMAS
        print(f"Tools: {len(TOOL_SCHEMAS)}")
    except Exception:
        pass

    return 0


# ── main ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="eitelite-cli",
        description="EITElite worker operations",
        add_help=False,
    )
    parser.add_argument("command", nargs="?", default="help",
                        choices=["status", "log", "prompt", "restart", "version", "help"])
    parser.add_argument("text", nargs="?", default="", help="prompt text (for 'prompt' command)")
    parser.add_argument("-n", type=int, default=50, help="log lines (for 'log' command)")

    args = parser.parse_args()

    commands = {
        "status": cmd_status,
        "log": cmd_log,
        "prompt": cmd_prompt,
        "restart": cmd_restart,
        "version": cmd_version,
        "help": lambda a: print(parser.format_help()) or 0,
    }

    cmd = commands.get(args.command)
    if cmd:
        sys.exit(cmd(args))
    parser.print_help()
    sys.exit(1)


if __name__ == "__main__":
    main()
