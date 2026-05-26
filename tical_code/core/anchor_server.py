"""EITElite / tical-code 锚点 HTTP 服务

提供锚点 JSON 给 worker 读取。支持:
  GET /          → 整个锚点数据
  GET /anchor/   → 同上 (nginx 代理路径)
  POST /register → worker 注册
  POST /work     → worker 状态更新
"""
import json
import os
import time
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional

PORT = int(os.getenv("ANCHOR_PORT", "9878"))
ANCHOR_FILE = Path(os.getenv("ANCHOR_FILE", "/home/ubuntu/anchors/ops-anchor.json"))

# 内存 worker 状态
worker_states: dict = {}
state_lock = threading.Lock()


class AnchorHandler(BaseHTTPRequestHandler):
    
    def log_message(self, fmt, *args):
        pass  # 不输出访问日志
    
    # ─── 数据加载 ───
    
    def _load_anchor(self) -> dict:
        """读取锚点 JSON"""
        if ANCHOR_FILE.exists():
            try:
                return json.loads(ANCHOR_FILE.read_text())
            except Exception:
                pass
        return {"version": "unknown", "note": "anchor file not found"}
    
    # ─── 响应辅助 ───
    
    def _send_json(self, data: dict, code: int = 200):
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
    
    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length))
    
    # ─── GET ───
    
    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        
        # 支持 / 和 /anchor
        if path in ("", "/anchor"):
            data = self._load_anchor()
            # 注入 worker 状态
            with state_lock:
                data["_workers"] = dict(worker_states)
            return self._send_json(data)
        
        # worker 列表
        if path == "/workers":
            with state_lock:
                return self._send_json(dict(worker_states))
        
        self._send_json({"error": "not_found"}, 404)
    
    # ─── POST ───
    
    def do_POST(self):
        path = self.path.rstrip("/")
        
        body = self._read_body()
        name = body.get("name", "unknown")
        
        # /register: worker 注册
        if path == "/register":
            with state_lock:
                worker_states[name] = {
                    "hostname": body.get("hostname", ""),
                    "status": body.get("status", "online"),
                    "last_seen": time.time(),
                    "ip": self.client_address[0],
                }
            return self._send_json({"ok": True, "name": name})
        
        # /work: 工作状态更新
        if path == "/work":
            with state_lock:
                if name in worker_states:
                    worker_states[name].update({
                        "status": body.get("status", worker_states[name].get("status", "unknown")),
                        "task": body.get("task", ""),
                        "last_seen": time.time(),
                    })
                else:
                    worker_states[name] = {
                        "hostname": body.get("hostname", ""),
                        "status": body.get("status", "online"),
                        "task": body.get("task", ""),
                        "last_seen": time.time(),
                    }
            return self._send_json({"ok": True, "name": name})
        
        # task/enqueue / task/dequeue / task/complete
        if path == "/task/enqueue":
            return self._send_json({"ok": True, "task_id": str(int(time.time()))})
        if path == "/task/dequeue":
            return self._send_json({"ok": True, "task": None})
        if path == "/task/complete":
            return self._send_json({"ok": True})
        
        self._send_json({"error": "not_found"}, 404)


def main():
    # 确保锚点文件存在
    if not ANCHOR_FILE.exists():
        fallback = Path("/home/ubuntu/tical-code/anchor.json")
        if fallback.exists():
            os.environ["ANCHOR_FILE"] = str(fallback)
    
    print(f"Anchor server -> {ANCHOR_FILE}")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), AnchorHandler)
    print(f"Listening on :{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
