"""EITElite / tical-code 锚点 HTTP 服务

Worker 通过此服务读写共享状态:
  - 锚点数据 (ops-anchor.json + ai_workers)
  - Worker 在线状态注册
  - 工作任务队列
  - 兄弟节点工作状态

所有路径支持 /anchor 前缀 (nginx 透传)。
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

# 内存 worker 状态 (兄弟节点工作状态)
worker_states: dict = {}
state_lock = threading.Lock()

# 任务队列
task_queue: list = []
task_lock = threading.Lock()
task_counter = 0


class AnchorHandler(BaseHTTPRequestHandler):
    
    def log_message(self, fmt, *args):
        pass
    
    # ─── 路径归一化: 去掉 /anchor 前缀 ───
    
    def _normalize(self, raw_path: str) -> str:
        """把 /anchor/work 变成 /work, /anchor/task/dequeue 变成 /task/dequeue"""
        p = raw_path.split("?")[0].rstrip("/")
        # Strip /anchor prefix (nginx 透传)
        if p.startswith("/anchor"):
            p = p[len("/anchor"):] or "/"
        return p
    
    # ─── 数据加载 ───
    
    def _load_anchor(self) -> dict:
        if ANCHOR_FILE.exists():
            try:
                return json.loads(ANCHOR_FILE.read_text())
            except Exception:
                pass
        return {"version": "unknown"}
    
    def _worker_list(self) -> dict:
        """返回兄弟节点工作状态 (用于 _anchor_api('anchor/work'))"""
        with state_lock:
            return dict(worker_states)
    
    # ─── 响应辅助 ───
    
    def _send_json(self, data: dict, code: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
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
        path = self._normalize(self.path)
        
        # 根路径 → 返回完整锚点数据
        if path in ("", "/"):
            data = self._load_anchor()
            with state_lock:
                data["_workers"] = dict(worker_states)
            return self._send_json(data)
        
        # /work 或 /workers → 返回兄弟节点工作状态
        if path in ("/work", "/workers"):
            return self._send_json(self._worker_list())
        
        # /task/list → 返回任务队列
        if path == "/task/list":
            with task_lock:
                return self._send_json({"tasks": list(task_queue)})
        
        self._send_json({"error": "not_found"}, 404)
    
    # ─── POST ───
    
    def do_POST(self):
        path = self._normalize(self.path)
        body = self._read_body()
        name = body.get("name", body.get("worker", "unknown"))
        
        # /register → Worker 注册/心跳
        if path in ("/register", "/", "/anchor"):
            with state_lock:
                worker_states[name] = {
                    "hostname": body.get("hostname", ""),
                    "status": body.get("status", "online"),
                    "task": body.get("task", ""),
                    "progress": body.get("progress", ""),
                    "last_seen": time.time(),
                    "ip": self.client_address[0],
                }
            return self._send_json({"ok": True, "name": name})
        
        # /work → 更新工作状态 (同 /register)
        if path == "/work":
            with state_lock:
                if name in worker_states:
                    worker_states[name].update({
                        "status": body.get("status", worker_states[name].get("status", "unknown")),
                        "task": body.get("task", worker_states[name].get("task", "")),
                        "progress": body.get("progress", worker_states[name].get("progress", "")),
                        "last_seen": time.time(),
                    })
                else:
                    worker_states[name] = {
                        "hostname": body.get("hostname", ""),
                        "status": body.get("status", "online"),
                        "task": body.get("task", ""),
                        "progress": body.get("progress", ""),
                        "last_seen": time.time(),
                    }
            return self._send_json({"ok": True, "name": name})
        
        # /task/enqueue → 入队
        if path == "/task/enqueue":
            global task_counter
            with task_lock:
                task_counter += 1
                task_id = str(int(time.time())) + str(task_counter)
                task_queue.append({
                    "id": task_id,
                    "task": body.get("task", ""),
                    "target": body.get("target", ""),
                    "sender": body.get("sender", ""),
                    "status": "pending",
                    "created_at": time.time(),
                })
            return self._send_json({"ok": True, "task_id": task_id})
        
        # /task/dequeue → 出队
        if path == "/task/dequeue":
            worker = body.get("worker", "")
            with task_lock:
                for i, t in enumerate(task_queue):
                    if t["status"] == "pending" and (not t["target"] or t["target"] == worker):
                        t["status"] = "running"
                        t["worker"] = worker
                        t["started_at"] = time.time()
                        task_queue.pop(i)
                        return self._send_json({"ok": True, "task": t})
            return self._send_json({"ok": True, "task": None})
        
        # /task/complete → 完成
        if path == "/task/complete":
            return self._send_json({"ok": True})
        
        self._send_json({"error": "not_found"}, 404)


def main():
    if not ANCHOR_FILE.exists():
        fallback = Path("/home/ubuntu/tical-code/anchor.json")
        if fallback.exists():
            os.environ["ANCHOR_FILE"] = str(fallback)
    
    print(f"Anchor server v0.2 -> {ANCHOR_FILE}")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), AnchorHandler)
    print(f"Listening on :{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
