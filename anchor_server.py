"""Live Anchor Server — Agent-aware ops-anchor.json.

Each Worker calls POST /anchor on startup + periodically.
Others can GET /anchor to see who's alive.

Run on Taiwan alongside tical-chat:
  python3 anchor_server.py &
"""

import json
import os
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

ANCHOR_PATH = os.path.expanduser("~/anchors/ops-anchor.json")
AGENT_HOME = os.path.expanduser("~")
HOSTNAME = os.uname().nodename

# How long without a ping before a VPS is marked offline (seconds)
STALE_THRESHOLD = 90  # 90s = 3 missed pings at 30s interval


class AnchorHandler(BaseHTTPRequestHandler):
    def _send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False, indent=2).encode())

    def _read_anchor(self):
        if os.path.exists(ANCHOR_PATH):
            try:
                with open(ANCHOR_PATH) as f:
                    return json.load(f)
            except json.JSONDecodeError:
                return {"version": "live", "vps": {}, "anchor_live": {"server": HOSTNAME}}
        return {"version": "live", "vps": {}, "anchor_live": {"server": HOSTNAME}}

    def _save_anchor(self, data):
        os.makedirs(os.path.dirname(ANCHOR_PATH), exist_ok=True)
        with open(ANCHOR_PATH, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def do_GET(self):
        if self.path == "/anchor" or self.path == "/":
            data = self._read_anchor()

            # Compute live status for each VPS
            live = data.get("anchor_live", {})
            now = time.time()
            for name, info in live.get("agents", {}).items():
                last_seen = info.get("last_seen", 0)
                info["alive"] = (now - last_seen) < STALE_THRESHOLD

            # Merge live status into VPS section
            for name, linfo in live.get("agents", {}).items():
                if name in data.get("vps", {}):
                    data["vps"][name]["alive"] = linfo.get("alive", False)
                    data["vps"][name]["last_seen"] = linfo.get("last_seen", 0)
                    data["vps"][name]["last_seen_human"] = linfo.get("last_seen_human", "")

            data["_live"] = {
                "server_time": datetime.utcnow().isoformat() + "Z",
                "total_agents": len(live.get("agents", {})),
                "alive_count": sum(1 for a in live.get("agents", {}).values() if a.get("alive")),
            }
            self._send_json(data)

        elif self.path == "/anchor/health":
            self._send_json({"status": "ok", "hostname": HOSTNAME, "time": time.time()})

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
        ts_human = datetime.utcfromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S UTC")

        data = self._read_anchor()
        live = data.setdefault("anchor_live", {})
        live.setdefault("agents", {})

        agent_info = {
            "name": name,
            "hostname": body.get("hostname", ""),
            "ip": body.get("ip", ""),
            "version": body.get("version", ""),
            "last_seen": now,
            "last_seen_human": ts_human,
            "status": body.get("status", "online"),
        }
        live["agents"][name] = agent_info

        # Also update the static VPS section with dynamic fields
        if name in data.get("vps", {}):
            vps = data["vps"][name]
            if body.get("ip"):
                vps["ip"] = body["ip"]
            if body.get("version"):
                vps["system"] = body["version"]
            vps["alive"] = True
            vps["last_seen_human"] = ts_human

        live["_last_update"] = now
        self._save_anchor(data)

        self._send_json({"ok": True, "name": name, "ttl": STALE_THRESHOLD})

    def log_message(self, format, *args):
        pass  # quiet


if __name__ == "__main__":
    port = 9878
    server = HTTPServer(("0.0.0.0", port), AnchorHandler)
    print(f"Live Anchor Server on port {port}")
    print(f"  POST /anchor  — Workers report in")
    print(f"  GET  /anchor  — See all agents + live status")
    print(f"  Anchor file: {ANCHOR_PATH}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
