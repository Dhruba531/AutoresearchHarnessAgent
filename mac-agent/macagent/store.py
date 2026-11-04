"""SQLite persistence: tasks and their event streams, approvals, devices, memory.

One connection guarded by a lock; every write is small, and the agent's slow
work (model calls, screen actions) happens outside it.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
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
-- every event that reaches the system, with an ID (E<n>) and its filter verdicts
CREATE TABLE IF NOT EXISTS incoming (
  id INTEGER PRIMARY KEY, source TEXT NOT NULL, kind TEXT NOT NULL, sender TEXT, text TEXT NOT NULL,
  payload TEXT NOT NULL DEFAULT '{}', thread TEXT, stage TEXT NOT NULL DEFAULT 'new',
  verdicts TEXT NOT NULL DEFAULT '[]', routed_to TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS incoming_thread ON incoming(thread, id);
CREATE TABLE IF NOT EXISTS subscriptions (
  thread TEXT NOT NULL, task_id INTEGER NOT NULL, created_at REAL NOT NULL, PRIMARY KEY(thread, task_id));
-- spilled content kept outside the context window, read back by ID (O<n>)
CREATE TABLE IF NOT EXISTS objects (
  id INTEGER PRIMARY KEY, task_id INTEGER, kind TEXT NOT NULL, label TEXT, content TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS policies (
  id INTEGER PRIMARY KEY, text TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, created_by TEXT, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS machines (
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, token_hash TEXT UNIQUE NOT NULL, created_at REAL NOT NULL,
  last_seen REAL, info TEXT NOT NULL DEFAULT '{}', revoked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS watchdog (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, data TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS proposals (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, data TEXT NOT NULL, summary TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
  created_at REAL NOT NULL, decided_at REAL);
"""

MIGRATIONS = [
    "ALTER TABLE tasks ADD COLUMN origin TEXT",
    "ALTER TABLE tasks ADD COLUMN mode TEXT NOT NULL DEFAULT 'normal'",
    "ALTER TABLE tasks ADD COLUMN machine TEXT",
    "ALTER TABLE tasks ADD COLUMN resume_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE tasks ADD COLUMN guidance TEXT",
]

TASK_FIELDS = ("id", "title", "prompt", "status", "source", "created_at", "updated_at", "started_at", "ended_at",
               "run_at", "repeat", "result", "error", "cost_usd", "provider", "model", "steps", "origin", "mode",
               "machine", "resume_count")


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
                    title: str | None = None, mode: str = "normal", machine: str | None = None,
                    guidance: str | None = None) -> int:
        now = time.time()
        title = (title or (prompt.strip().splitlines()[0] if prompt.strip() else "Task"))[:80]
        status = "scheduled" if run_at and run_at > now + 1 else "queued"
        cur = self.x(
            "INSERT INTO tasks(title,prompt,status,source,created_at,updated_at,run_at,repeat,provider,model,origin,mode,"
            "machine,guidance) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (title, prompt, status, source, now, now, run_at, repeat, provider, model, origin, mode, machine, guidance),
        )
        return int(cur.lastrowid)

    def task(self, task_id: int, with_history: bool = False) -> dict | None:
        cols = ",".join(TASK_FIELDS) + (",history,guidance" if with_history else "")
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

    # -- incoming events (E<n>) ----------------------------------------------------
    def add_incoming(self, ev: dict) -> dict:
        cur = self.x(
            "INSERT INTO incoming(source,kind,sender,text,payload,thread,created_at) VALUES(?,?,?,?,?,?,?)",
            (ev["source"], ev["kind"], ev.get("sender"), ev.get("text") or "", json.dumps(ev.get("payload") or {}, default=str),
             ev.get("thread"), ev.get("at") or time.time()),
        )
        return self.incoming(int(cur.lastrowid))

    def incoming(self, eid: int) -> dict | None:
        r = self.one("SELECT * FROM incoming WHERE id=?", (eid,))
        if r:
            for k in ("payload", "verdicts", "routed_to"):
                r[k] = json.loads(r[k])
        return r

    def incoming_list(self, limit: int = 100, where: str = "", args: tuple = ()) -> list[dict]:
        rows = self.all(f"SELECT * FROM incoming {where} ORDER BY id DESC LIMIT ?", (*args, limit))
        for r in rows:
            for k in ("payload", "verdicts", "routed_to"):
                r[k] = json.loads(r[k])
        return rows

    def verdict(self, eid: int, by: str, verdict: str, why: str, stage: str | None = None,
                routed_to: list[int] | None = None) -> None:
        r = self.incoming(eid)
        if r is None:
            return
        verdicts = r["verdicts"] + [{"by": by, "verdict": verdict, "why": why[:300]}]
        routed = sorted(set(r["routed_to"]) | set(routed_to or []))
        self.x("UPDATE incoming SET verdicts=?, stage=?, routed_to=? WHERE id=?",
               (json.dumps(verdicts), stage or r["stage"], json.dumps(routed), eid))

    # -- threads --------------------------------------------------------------------
    def subscribe(self, thread: str, task_id: int) -> None:
        self.x("INSERT OR IGNORE INTO subscriptions(thread,task_id,created_at) VALUES(?,?,?)", (thread, task_id, time.time()))

    def unsubscribe(self, thread: str, task_id: int) -> None:
        self.x("DELETE FROM subscriptions WHERE thread=? AND task_id=?", (thread, task_id))

    def subscribers(self, thread: str, live_only: bool = True) -> list[int]:
        sql = "SELECT s.task_id FROM subscriptions s JOIN tasks t ON t.id=s.task_id WHERE s.thread=?"
        if live_only:
            sql += " AND t.status IN ('queued','running','waiting','paused')"
        return [r["task_id"] for r in self.all(sql + " ORDER BY s.task_id", (thread,))]

    def threads_of(self, task_id: int) -> list[str]:
        return [r["thread"] for r in self.all("SELECT thread FROM subscriptions WHERE task_id=? ORDER BY created_at", (task_id,))]

    # -- spilled objects (O<n>) ---------------------------------------------------------
    def add_object(self, task_id: int | None, kind: str, content: str, label: str = "") -> int:
        return int(self.x("INSERT INTO objects(task_id,kind,label,content,created_at) VALUES(?,?,?,?,?)",
                          (task_id, kind, label, content, time.time())).lastrowid)

    def object(self, oid: int) -> dict | None:
        return self.one("SELECT * FROM objects WHERE id=?", (oid,))

    # -- policies (prompts people can edit) -----------------------------------------------
    def policies(self, enabled_only: bool = False) -> list[dict]:
        return self.all("SELECT * FROM policies" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY id")

    def add_policy(self, text: str, by: str = "user", enabled: bool = True) -> int:
        return int(self.x("INSERT INTO policies(text,enabled,created_by,created_at) VALUES(?,?,?,?)",
                          (text.strip(), 1 if enabled else 0, by, time.time())).lastrowid)

    # -- watchdog & proposals ------------------------------------------------------------
    def add_watchdog(self, kind: str, data: dict) -> dict:
        now = time.time()
        wid = int(self.x("INSERT INTO watchdog(kind,data,created_at) VALUES(?,?,?)", (kind, json.dumps(data, default=str), now)).lastrowid)
        return {"id": wid, "kind": kind, "data": data, "created_at": now}

    def system_stats(self, since: float) -> dict:
        """Counts for the one-page system map: what came in, what each stage did, what the database holds."""
        def base(src: str) -> str:
            head = src.split(":", 1)[0]
            return head if head != "trigger" else src  # keep trigger:<kind> apart
        sources: dict[str, int] = {}
        flow = {"events": 0, "code_admit": 0, "candidates": 0, "model_admit": 0, "dropped": 0, "passed": 0,
                "routed": 0, "dispatched": 0, "ignored": 0, "waiting": 0}
        for r in self.all("SELECT source, kind, payload, stage, verdicts FROM incoming WHERE created_at>?", (since,)):
            outside = r["kind"] in ("message", "trigger")  # worker reports and task ends skip the filter
            stage = r["stage"]
            flow["routed"] += stage == "routed"
            flow["waiting"] += stage == "new"
            if outside:
                src = r["source"]
                if src.startswith("trigger"):
                    kind = (json.loads(r["payload"] or "{}") or {}).get("_trigger_kind")
                    src = f"trigger:{kind}" if kind else "trigger"
                sources[base(src)] = sources.get(base(src), 0) + 1
                flow["events"] += 1
                flow["dropped"] += stage == "dropped"
                flow["passed"] += stage == "passed"
            for v in json.loads(r["verdicts"] or "[]"):
                by, verdict = v.get("by"), v.get("verdict")
                if by == "code" and not outside:
                    continue
                if by == "code" and verdict == "admit":
                    flow["code_admit"] += 1
                elif by == "code" and verdict == "candidate":
                    flow["candidates"] += 1
                elif by == "model" and verdict == "admit":
                    flow["model_admit"] += 1
                elif by == "router" and verdict == "dispatch":
                    flow["dispatched"] += 1
                elif by == "router" and verdict == "ignore":
                    flow["ignored"] += 1
        replies: dict[str, int] = {}
        for r in self.all("SELECT channel, COUNT(*) AS n FROM chat WHERE role='relay' AND created_at>? GROUP BY channel",
                          (since,)):
            ch = r["channel"].split(":", 1)[0]
            replies[ch] = replies.get(ch, 0) + r["n"]
        reports = self.one("SELECT COUNT(*) AS n FROM events WHERE kind='report' AND created_at>?", (since,))["n"]
        count = lambda t: self.one(f"SELECT COUNT(*) AS n FROM {t}")["n"]  # noqa: E731 - fixed table names only
        size = sum(f.stat().st_size for f in (Path(self.path), Path(self.path + "-wal")) if f.exists())
        return {"since": since, "sources": sources, "flow": flow, "replies": replies, "reports": reports,
                "db": {"tasks": count("tasks"), "events": count("events"), "incoming": count("incoming"),
                       "objects": count("objects"), "memories": count("memories"), "bytes": size,
                       "resumed": self.one("SELECT COUNT(*) AS n FROM tasks WHERE resume_count>0")["n"]},
                "threads": self.one("SELECT COUNT(DISTINCT thread) AS n FROM subscriptions")["n"]}

    def watchdog_log(self, kinds: tuple[str, ...], limit: int = 50) -> list[dict]:
        marks = ",".join("?" * len(kinds))
        rows = self.all(f"SELECT * FROM watchdog WHERE kind IN ({marks}) ORDER BY id DESC LIMIT ?", (*kinds, limit))
        for r in rows:
            r["data"] = json.loads(r["data"])
        return list(reversed(rows))

    def add_proposal(self, kind: str, data: dict, summary: str) -> int:
        return int(self.x("INSERT INTO proposals(kind,data,summary,created_at) VALUES(?,?,?,?)",
                          (kind, json.dumps(data), summary, time.time())).lastrowid)

    def proposals(self, status: str | None = None) -> list[dict]:
        rows = self.all("SELECT * FROM proposals" + (" WHERE status=?" if status else "") + " ORDER BY id DESC LIMIT 50",
                        (status,) if status else ())
        for r in rows:
            r["data"] = json.loads(r["data"])
        return rows
