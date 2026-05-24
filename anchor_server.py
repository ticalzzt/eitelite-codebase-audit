#!/usr/bin/env python3
"""Live Anchor Server v2 — heartbeat + work state sync.

POST /anchor — Worker reports: alive status + current task info
GET  /anchor — Full state: who's alive, who's doing what
GET  /anchor/work — Work-only view (for task orchestration)
"""

import json, os, time, subprocess
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone

ANCHOR_PATH = os.path.expanduser("~/anchors/ops-anchor.json")
GIT_REPO = os.path.expanduser("~/anchors")  # git init this dir for persistence
HOSTNAME = os.uname().nodename
STALE_THRESHOLD = 120  # 2 min without ping = assumed dead


class AnchorHandler(BaseHTTPRequestHandler):
    def _send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False, indent=2).encode())

    def _read(self):
        if os.path.exists(ANCHOR_PATH):
            try:
                with open(ANCHOR_PATH) as f:
                    return json.load(f)
            except json.JSONDecodeError:
                pass
        return {"version": "live", "vps": {}, "anchor_live": {"agents": {}}}

    def _save(self, data):
        os.makedirs(os.path.dirname(ANCHOR_PATH), exist_ok=True)
        with open(ANCHOR_PATH, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        # Git sync for persistence (best-effort)
        try:
            subprocess.run(
                ["git", "-C", os.path.dirname(ANCHOR_PATH), "add", "-A"],
                capture_output=True, timeout=5,
            )
            subprocess.run(
                ["git", "-C", os.path.dirname(ANCHOR_PATH), "commit",
                 "-m", f"anchor sync {datetime.now(timezone.utc).strftime('%H:%M:%S')}"],
                capture_output=True, timeout=5,
            )
        except Exception:
            pass

    def do_GET(self):
        now = time.time()
        data = self._read()

        if self.path in ("/anchor", "/"):
            agents = data.setdefault("anchor_live", {}).setdefault("agents", {})
            for name, info in agents.items():
                info["alive"] = (now - info.get("last_seen", 0)) < STALE_THRESHOLD
                # Merge into vps for convenience
                if name in data["vps"]:
                    data["vps"][name]["alive"] = info["alive"]
                    data["vps"][name]["last_seen_human"] = info.get("last_seen_human", "")
                    if info.get("current_task"):
                        data["vps"][name]["current_task"] = info["current_task"]
                    if info.get("progress"):
                        data["vps"][name]["progress"] = info["progress"]
                    if info.get("result"):
                        data["vps"][name]["result"] = info["result"]
            data["_live"] = {
                "server_time": datetime.now(timezone.utc).isoformat(),
                "total_agents": len(agents),
                "alive_count": sum(1 for a in agents.values() if a.get("alive")),
            }
            self._send_json(data)

        elif self.path == "/anchor/work":
            agents = data.setdefault("anchor_live", {}).setdefault("agents", {})
            work_view = {}
            for name, info in agents.items():
                alive = (now - info.get("last_seen", 0)) < STALE_THRESHOLD
                task = info.get("current_task")
                if task and alive:
                    work_view[name] = {
                        "task": task,
                        "task_type": info.get("task_type", ""),
                        "progress": info.get("progress", ""),
                        "status": info.get("status", "idle"),
                        "result": info.get("result"),
                        "updated": info.get("last_seen_human", ""),
                    }
                elif alive:
                    work_view[name] = {"status": "idle", "task": None}
            self._send_json(work_view)

        elif self.path == "/anchor/health":
            self._send_json({"status": "ok", "hostname": HOSTNAME})

        elif self.path == "/anchor/task/list":
            self._task_list()

        else:
            self._send_json({"error": "not_found"}, 404)

    def do_POST(self):
        if self.path != "/anchor":
            self._send_json({"error": "not_found"}, 404)
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
        except Exception:
            self._send_json({"error": "bad_request"}, 400)
            return

        name = body.get("name", "")
        if not name:
            self._send_json({"error": "name required"}, 400)

        now = time.time()
        ts = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        data = self._read()
        agents = data.setdefault("anchor_live", {}).setdefault("agents", {})

        agent = agents.setdefault(name, {})
        agent.update({
            "name": name,
            "hostname": body.get("hostname", ""),
            "ip": body.get("ip", ""),
            "version": body.get("version", ""),
            "status": body.get("status", "online"),
            "last_seen": now,
            "last_seen_human": ts,
        })

        # Work state fields
        for field in ("current_task", "task_type", "progress", "result"):
            if field in body:
                agent[field] = body[field]

        agents[name] = agent
        self._save(data)
        self._send_json({"ok": True, "name": name, "ttl": STALE_THRESHOLD})

    def log_message(self, *args):
        pass

    # ============ Task Queue ============

    def do_TASK(self, method):
        """Handle /anchor/task/* endpoints with correct HTTP method."""
        if self.path == "/anchor/task/enqueue" and method == "POST":
            self._task_enqueue()
        elif self.path == "/anchor/task/dequeue" and method == "POST":
            self._task_dequeue()
        elif self.path == "/anchor/task/complete" and method == "POST":
            self._task_complete()
        elif self.path == "/anchor/task/list" and method == "GET":
            self._task_list()
        else:
            self._send_json({"error": "not_found"}, 404)

    def _task_enqueue(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
        except Exception:
            self._send_json({"error": "bad_request"}, 400)
            return
        target = body.get("target", "")
        task = body.get("task", "")
        if not target or not task:
            self._send_json({"error": "target and task required"}, 400)
            return
        data = self._read()
        tasks = data.setdefault("anchor_tasks", [])
        entry = {
            "id": int(time.time() * 1000) % 1000000,
            "target": target,
            "task": task,
            "sender": body.get("sender", "unknown"),
            "status": "queued",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "assigned_to": None,
            "result": None,
        }
        tasks.insert(0, entry)  # newest first
        self._save(data)
        self._send_json({"ok": True, "task_id": entry["id"]})

    def _task_dequeue(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
        except Exception:
            self._send_json({"error": "bad_request"}, 400)
            return
        worker = body.get("worker", "")
        if not worker:
            self._send_json({"error": "worker required"}, 400)
            return
        data = self._read()
        tasks = data.get("anchor_tasks", [])
        # Find first queued task for this worker OR unassigned
        for t in tasks:
            if t["status"] == "queued" and (t["target"] == worker or t["target"] == "any"):
                t["status"] = "running"
                t["assigned_to"] = worker
                t["started_at"] = datetime.now(timezone.utc).isoformat()
                self._save(data)
                self._send_json({"ok": True, "task": t})
                return
        self._send_json({"ok": True, "task": None})  # no task available

    def _task_complete(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
        except Exception:
            self._send_json({"error": "bad_request"}, 400)
            return
        task_id = body.get("task_id", 0)
        result = body.get("result", "")
        status = body.get("status", "done")
        data = self._read()
        for t in data.get("anchor_tasks", []):
            if t["id"] == task_id:
                t["status"] = status
                t["result"] = result
                t["completed_at"] = datetime.now(timezone.utc).isoformat()
                self._save(data)
                self._send_json({"ok": True, "task_id": task_id})
                return
        self._send_json({"error": "task not found"}, 404)

    def _task_list(self):
        data = self._read()
        tasks = data.get("anchor_tasks", [])
        tasks.sort(key=lambda t: t.get("created_at", ""), reverse=True)
        self._send_json({"ok": True, "tasks": tasks[:50]})



if __name__ == "__main__":
    # Init git for persistence
    anchor_dir = os.path.dirname(ANCHOR_PATH)
    if not os.path.exists(os.path.join(anchor_dir, ".git")):
        try:
            subprocess.run(["git", "-C", anchor_dir, "init"], capture_output=True)
            subprocess.run(["git", "-C", anchor_dir, "config", "user.email", "anchor@ticalasi.com"],
                          capture_output=True)
            subprocess.run(["git", "-C", anchor_dir, "config", "user.name", "Live Anchor"],
                          capture_output=True)
        except Exception:
            pass

    port = 9878
    server = HTTPServer(("0.0.0.0", port), AnchorHandler)
    print(f"Live Anchor v2 on port {port}")
    print(f"  POST /anchor — heartbeat + work state")
    print(f"  GET  /anchor — full state")
    print(f"  GET  /anchor/work — work-only view")
    print(f"  GET  /anchor/health — health")
    print(f"  File: {ANCHOR_PATH} (git-backed)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
