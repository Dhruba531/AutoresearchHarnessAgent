"""SQLite persistence: tasks and their event streams, approvals, devices, memory.

One connection guarded by a lock; every write is small, and the agent's slow
work (model calls, screen actions) happens outside it.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT UNIQUE NOT NULL,
  created_at REAL NOT NULL, last_seen REAL, revoked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS pairing_codes (
  code_hash TEXT PRIMARY KEY, expires_at REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY, title TEXT NOT NULL, prompt TEXT NOT NULL, status TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'app', created_at REAL NOT NULL, updated_at REAL NOT NULL,
  started_at REAL, ended_at REAL, run_at REAL, repeat TEXT, result TEXT, error TEXT,
  cost_usd REAL NOT NULL DEFAULT 0, provider TEXT, model TEXT, history TEXT NOT NULL DEFAULT '[]',
  steps INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL,
  created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS events_task ON events(task_id, id);
CREATE TABLE IF NOT EXISTS approvals (
  id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL, kind TEXT NOT NULL, tool TEXT NOT NULL,
  input TEXT NOT NULL, risk TEXT NOT NULL, summary TEXT NOT NULL, status TEXT NOT NULL,
  answer TEXT, created_at REAL NOT NULL, decided_at REAL, decided_via TEXT);
CREATE TABLE IF NOT EXISTS memories (id INTEGER PRIMARY KEY, text TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS spend (id INTEGER PRIMARY KEY, task_id INTEGER, usd REAL NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS telegram_chats (chat_id INTEGER PRIMARY KEY, name TEXT, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS chat (
  id INTEGER PRIMARY KEY, channel TEXT NOT NULL, role TEXT NOT NULL, text TEXT NOT NULL,
  task_id INTEGER, sender TEXT, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS chat_channel ON chat(channel, id);
CREATE TABLE IF NOT EXISTS triggers (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, config TEXT NOT NULL, instruction TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1, secret TEXT, state TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL,
  last_fired REAL, fire_count INTEGER NOT NULL DEFAULT 0, last_error TEXT);
"""

MIGRATIONS = ["ALTER TABLE tasks ADD COLUMN origin TEXT"]

TASK_FIELDS = ("id", "title", "prompt", "status", "source", "created_at", "updated_at", "started_at", "ended_at",
               "run_at", "repeat", "result", "error", "cost_usd", "provider", "model", "steps", "origin")


class Store:
    def __init__(self, path: str):
        self.path = path
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(SCHEMA)
            for sql in MIGRATIONS:  # additive columns for databases made by older versions
                try:
                    self._db.execute(sql)
                except sqlite3.OperationalError:
                    pass

    # -- primitives ---------------------------------------------------------
    def x(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, args)

    def one(self, sql: str, args: tuple = ()) -> dict | None:
        with self._lock:
            row = self._db.execute(sql, args).fetchone()
        return dict(row) if row else None

    def all(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    # -- kv -----------------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def put(self, key: str, value: Any) -> None:
        self.x("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, json.dumps(value)))

    # -- tasks --------------------------------------------------------------
    def create_task(self, prompt: str, *, source: str = "app", run_at: float | None = None,
                    repeat: str | None = None, provider: str = "", model: str = "", origin: str | None = None,
                    title: str | None = None) -> int:
        now = time.time()
        title = (title or (prompt.strip().splitlines()[0] if prompt.strip() else "Task"))[:80]
        status = "scheduled" if run_at and run_at > now + 1 else "queued"
        cur = self.x(
            "INSERT INTO tasks(title,prompt,status,source,created_at,updated_at,run_at,repeat,provider,model,origin)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (title, prompt, status, source, now, now, run_at, repeat, provider, model, origin),
        )
        return int(cur.lastrowid)

    def task(self, task_id: int, with_history: bool = False) -> dict | None:
        cols = ",".join(TASK_FIELDS) + (",history" if with_history else "")
        row = self.one(f"SELECT {cols} FROM tasks WHERE id=?", (task_id,))
        if row and with_history:
            row["history"] = json.loads(row["history"])
        return row

    def tasks(self, limit: int = 50) -> list[dict]:
        return self.all(f"SELECT {','.join(TASK_FIELDS)} FROM tasks ORDER BY id DESC LIMIT ?", (limit,))

    def update_task(self, task_id: int, **fields: Any) -> None:
        if "history" in fields:
            fields["history"] = json.dumps(fields["history"])
        fields["updated_at"] = time.time()
        sets = ",".join(f"{k}=?" for k in fields)
        self.x(f"UPDATE tasks SET {sets} WHERE id=?", (*fields.values(), task_id))

    def next_runnable(self) -> dict | None:
        now = time.time()
        self.x("UPDATE tasks SET status='queued', updated_at=? WHERE status='scheduled' AND run_at<=?", (now, now))
        return self.one(f"SELECT {','.join(TASK_FIELDS)} FROM tasks WHERE status='queued' ORDER BY id LIMIT 1")

    # -- events -------------------------------------------------------------
    def add_event(self, task_id: int, kind: str, data: dict) -> dict:
        now = time.time()
        cur = self.x("INSERT INTO events(task_id,kind,data,created_at) VALUES(?,?,?,?)",
                     (task_id, kind, json.dumps(data), now))
        return {"id": int(cur.lastrowid), "task_id": task_id, "kind": kind, "data": data, "created_at": now}

    def events(self, task_id: int, after: int = 0) -> list[dict]:
        rows = self.all("SELECT id,task_id,kind,data,created_at FROM events WHERE task_id=? AND id>? ORDER BY id",
                        (task_id, after))
        for r in rows:
            r["data"] = json.loads(r["data"])
        return rows

    # -- approvals ----------------------------------------------------------
    def add_approval(self, task_id: int, kind: str, tool: str, inp: dict, risk: str, summary: str) -> int:
        cur = self.x(
            "INSERT INTO approvals(task_id,kind,tool,input,risk,summary,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (task_id, kind, tool, json.dumps(inp), risk, summary, "pending", time.time()),
        )
        return int(cur.lastrowid)

    def approval(self, approval_id: int) -> dict | None:
        row = self.one("SELECT * FROM approvals WHERE id=?", (approval_id,))
        if row:
            row["input"] = json.loads(row["input"])
        return row

    def pending_approvals(self) -> list[dict]:
        rows = self.all("SELECT * FROM approvals WHERE status='pending' ORDER BY id")
        for r in rows:
            r["input"] = json.loads(r["input"])
        return rows

    def decide(self, approval_id: int, status: str, answer: str | None, via: str) -> bool:
        cur = self.x("UPDATE approvals SET status=?, answer=?, decided_at=?, decided_via=? WHERE id=? AND status='pending'",
                     (status, answer, time.time(), via, approval_id))
        return cur.rowcount == 1

    # -- spend & memory ------------------------------------------------------
    def add_spend(self, task_id: int | None, usd: float) -> None:
        if usd > 0:
            self.x("INSERT INTO spend(task_id,usd,created_at) VALUES(?,?,?)", (task_id, usd, time.time()))
            if task_id:
                self.x("UPDATE tasks SET cost_usd=cost_usd+? WHERE id=?", (usd, task_id))

    def spend_since(self, since: float) -> float:
        row = self.one("SELECT COALESCE(SUM(usd),0) AS s FROM spend WHERE created_at>=?", (since,))
        return float(row["s"]) if row else 0.0

    def memories(self) -> list[dict]:
        return self.all("SELECT id,text,created_at FROM memories ORDER BY id")

    def remember(self, text: str) -> int:
        return int(self.x("INSERT INTO memories(text,created_at) VALUES(?,?)", (text, time.time())).lastrowid)

    def forget(self, memory_id: int) -> None:
        self.x("DELETE FROM memories WHERE id=?", (memory_id,))

    # -- router chat log ------------------------------------------------------
    def add_chat(self, channel: str, role: str, text: str, task_id: int | None = None, sender: str | None = None) -> dict:
        now = time.time()
        cur = self.x("INSERT INTO chat(channel,role,text,task_id,sender,created_at) VALUES(?,?,?,?,?,?)",
                     (channel, role, text, task_id, sender, now))
        return {"id": int(cur.lastrowid), "channel": channel, "role": role, "text": text, "task_id": task_id,
                "sender": sender, "created_at": now}

    def chat(self, channel: str | None = None, limit: int = 60) -> list[dict]:
        if channel:
            rows = self.all("SELECT * FROM chat WHERE channel=? ORDER BY id DESC LIMIT ?", (channel, limit))
        else:
            rows = self.all("SELECT * FROM chat ORDER BY id DESC LIMIT ?", (limit,))
        return list(reversed(rows))

    # -- triggers -----------------------------------------------------------------
    def triggers(self, enabled_only: bool = False) -> list[dict]:
        rows = self.all("SELECT * FROM triggers" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY id")
        for r in rows:
            r["config"], r["state"] = json.loads(r["config"]), json.loads(r["state"])
        return rows

    def trigger(self, trigger_id: int) -> dict | None:
        r = self.one("SELECT * FROM triggers WHERE id=?", (trigger_id,))
        if r:
            r["config"], r["state"] = json.loads(r["config"]), json.loads(r["state"])
        return r
