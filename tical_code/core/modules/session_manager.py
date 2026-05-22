"""Module 1: Session Persistence - SQLite-backed conversation history."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("tical-code.session_manager")

class SessionManager:
    """Thread-safe SQLite session store with archival support."""

    MAX_TOOL_CONTENT = 2048

    def __init__(self, db_path: str, max_active: int = 100):
        self.db_path = Path(db_path)
        self.max_active = max_active
        self.lock = threading.Lock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}'
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_msg_sid ON messages(session_id, id)")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS archived_sessions (
                    session_id TEXT PRIMARY KEY,
                    created_at REAL,
                    updated_at REAL,
                    metadata TEXT,
                    archived_at REAL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS archived_messages (
                    id INTEGER PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    metadata TEXT
                )
            """)
            self.conn.commit()

    def get_session_id(self, channel: str, chat_id: str) -> str:
        raw = f"{channel}:{chat_id}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def save_messages(self, session_id: str, messages: list[dict]) -> bool:
        if not messages:
            return True
        try:
            now = time.time()
            with self.lock:
                cur = self.conn.cursor()
                cur.execute("SELECT 1 FROM sessions WHERE session_id = ?", (session_id,))
                if cur.fetchone():
                    cur.execute("UPDATE sessions SET updated_at = ? WHERE session_id = ?", (now, session_id))
                else:
                    cur.execute(
                        "INSERT INTO sessions (session_id, created_at, updated_at, metadata) VALUES (?, ?, ?, ?)",
                        (session_id, now, now, "{}"),
                    )
                for msg in messages:
                    role = msg.get("role", "unknown")
                    content = msg.get("content") or ""
                    meta: dict[str, Any] = {}
                    if role == "assistant" and msg.get("tool_calls"):
                        meta["tool_calls"] = msg["tool_calls"]
                    if role == "tool":
                        meta["tool_call_id"] = msg.get("tool_call_id")
                    if role == "tool" and len(content) > self.MAX_TOOL_CONTENT:
                        content = content[:self.MAX_TOOL_CONTENT] + "\n[truncated]"
                    cur.execute(
                        "INSERT INTO messages (session_id, role, content, timestamp, metadata) VALUES (?, ?, ?, ?, ?)",
                        (session_id, role, content, now, json.dumps(meta, ensure_ascii=False)),
                    )
                self.conn.commit()
                self._enforce_active_limit()
            return True
        except Exception:
            logger.exception("save_messages failed")
            return False

    def load_session(self, session_id: str, max_messages: int = 20) -> list[dict]:
        try:
            with self.lock:
                cur = self.conn.cursor()
                cur.execute(
                    "SELECT role, content, metadata FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                    (session_id, max_messages),
                )
                rows = list(reversed(cur.fetchall()))
            out: list[dict] = []
            for row in rows:
                msg: dict[str, Any] = {"role": row["role"], "content": row["content"]}
                try:
                    meta = json.loads(row["metadata"] or "{}")
                except Exception:
                    meta = {}
                if row["role"] == "assistant" and "tool_calls" in meta:
                    msg["tool_calls"] = meta["tool_calls"]
                if row["role"] == "tool" and "tool_call_id" in meta:
                    msg["tool_call_id"] = meta["tool_call_id"]
                out.append(msg)
            return out
        except Exception:
            logger.exception("load_session failed")
            return []

    def archive_old(self, days: int = 7) -> int:
        try:
            cutoff = time.time() - days * 86400
            with self.lock:
                cur = self.conn.cursor()
                cur.execute("SELECT session_id FROM sessions WHERE updated_at < ?", (cutoff,))
                expired = [r["session_id"] for r in cur.fetchall()]
                moved = 0
                for sid in expired:
                    self._archive_one(cur, sid)
                    moved += 1
                self.conn.commit()
            return moved
        except Exception:
            logger.exception("archive_old failed")
            return 0

    def reset(self) -> None:
        return

    def _archive_one(self, cur, session_id: str) -> None:
        row = cur.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        if not row:
            return
        now = time.time()
        cur.execute(
            "INSERT OR REPLACE INTO archived_sessions (session_id, created_at, updated_at, metadata, archived_at) VALUES (?, ?, ?, ?, ?)",
            (row["session_id"], row["created_at"], row["updated_at"], row["metadata"], now),
        )
        cur.execute(
            "INSERT OR REPLACE INTO archived_messages SELECT * FROM messages WHERE session_id = ?",
            (session_id,),
        )
        cur.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        cur.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))

    def _enforce_active_limit(self) -> None:
        try:
            cur = self.conn.cursor()
            cur.execute("SELECT session_id FROM sessions ORDER BY updated_at DESC")
            rows = cur.fetchall()
            if len(rows) <= self.max_active:
                return
            for row in rows[self.max_active:]:
                self._archive_one(cur, row["session_id"])
            self.conn.commit()
        except Exception:
            logger.exception("_enforce_active_limit failed")
