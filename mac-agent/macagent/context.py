"""The context window as a cache: Inline & Spill, and Chain of Recall.

Every object has an ID that a model can follow with read(ID):

  E12            an incoming event (message, trigger, report)
  W7             a worker task
  O31            spilled content (a long tool output, a finished job's log)
  thread:<key>   a Thread — consecutive related events (an email chain, a chat
                 reply chain, one automation's stream)
  chat:<channel> earlier conversation on a channel

Injection follows three steps (DeepSeek's design note, §7):
  1. start minimal — the object's ID and a short summary;
  2. inline what is near — in space (same thread) and in time (just before),
     as one line each with their IDs;
  3. spill the rest — cut long content and leave a pointer, so the full
     version is one read() away instead of filling the window.

Nothing is thrown away: spilled content stays in the database.
Chain of Recall: every read result inlines the IDs of its closest
relatives, so a model can hop from the one line it kept to the details.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from typing import Any

from .store import Store

EVENT_CAP = 600          # chars of an event's text inlined before spilling
SPILL_AT = 6000          # tool outputs longer than this are spilled
SPILL_HEAD, SPILL_TAIL = 3000, 800
READ_PAGE = 6000
NEAR_TIME_S = 15 * 60


def _t(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M") if ts else "--:--"


def _one(text: str, n: int = 90) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def ev_line(ev: dict) -> str:
    who = f" {ev['sender']}" if ev.get("sender") else ""
    return f"[E{ev['id']}] {_t(ev['created_at'])} {ev['source']}{who}: \"{_one(ev['text'])}\""


def task_line(t: dict) -> str:
    when = _t(t.get("ended_at") or t.get("updated_at"))
    tail = f" — {_one(t.get('result') or t.get('error') or '', 70)}" if t.get("result") or t.get("error") else ""
    mode = " (silent)" if t.get("mode") == "silent" else ""
    return f"[W{t['id']}] \"{_one(t['title'], 50)}\"{mode} {t['status']} {when}{tail}"


def render_event(store: Store, ev: dict, *, cap: int = EVENT_CAP, near: bool = True) -> str:
    """One event, the way a model should first see it."""
    lines = [f"EVENT [E{ev['id']}] {ev['source']}{' ' + ev['sender'] if ev.get('sender') else ''} {_t(ev['created_at'])}"
             + (f"  thread [{ev['thread']}]" if ev.get("thread") else "")]
    text = ev.get("text") or ""
    if len(text) > cap:
        lines.append(f'  "{text[:cap]}… [cut at {cap} chars: rest → read(E{ev["id"]})]"')
    else:
        lines.append(f'  "{text}"')
    payload = ev.get("payload") or {}
    extra = {k: v for k, v in payload.items() if k != "text" and not k.startswith("_")} if isinstance(payload, dict) else payload
    if extra:
        blob = json.dumps(extra, ensure_ascii=False, default=str)
        lines.append(f"  data: {blob[:300]}" + (f"… [full → read(E{ev['id']})]" if len(blob) > 300 else ""))
    if near:
        if ev.get("thread"):
            before = store.all("SELECT * FROM incoming WHERE thread=? AND id<? ORDER BY id DESC LIMIT 3",
                               (ev["thread"], ev["id"]))
            if before:
                lines.append("NEAR IN SPACE (same thread)")
                lines += ["  " + ev_line(b) for b in reversed(before)]
        recent = store.all("SELECT * FROM incoming WHERE id<? AND created_at>? AND (thread IS NULL OR thread!=?) "
                           "AND stage!='dropped' AND kind IN ('message','trigger') ORDER BY id DESC LIMIT 3",
                           (ev["id"], ev["created_at"] - NEAR_TIME_S, ev.get("thread") or ""))
        if recent:
            lines.append("NEAR IN TIME")
            lines += ["  " + ev_line(r) for r in reversed(recent)]
        if ev.get("thread"):
            subs = store.subscribers(ev["thread"])
            lines.append(f"THREAD [{ev['thread']}] subscribers " + (", ".join(f"[W{s}]" for s in subs) or "none"))
    return "\n".join(lines)


def spill(store: Store, task_id: int | None, text: str, label: str = "output") -> str:
    """Keep long text outside the window behind a pointer."""
    if len(text) <= SPILL_AT:
        return text
    oid = store.add_object(task_id, label, text, label)
    hidden = len(text) - SPILL_HEAD - SPILL_TAIL
    return (f"{text[:SPILL_HEAD]}\n…[cut {hidden} chars: full {label} → read(O{oid})]…\n{text[-SPILL_TAIL:]}")


def _page(text: str, ref: str, offset: int, limit: int) -> str:
    chunk = text[offset: offset + limit]
    end = offset + len(chunk)
    more = f"\n[chars {offset}–{end} of {len(text)}; more → read({ref}, offset={end})]" if end < len(text) else ""
    return chunk + more


def read(store: Store, ref: str, offset: int = 0, limit: int = READ_PAGE) -> str:
    """Resolve an ID. Each result inlines the IDs of the next hop."""
    ref = ref.strip().strip("[]")
    limit = max(500, min(int(limit or READ_PAGE), 20_000))
    offset = max(0, int(offset or 0))
    m = re.fullmatch(r"([EWO])(\d+)", ref, re.I)
    if m:
        kind, n = m.group(1).upper(), int(m.group(2))
        if kind == "E":
            ev = store.incoming(n)
            if not ev:
                return f"No event {ref}."
            body = ev["text"]
            data = {k: v for k, v in ev["payload"].items() if not k.startswith("_")}
            if data:
                body += "\n\ndata:\n" + json.dumps(data, ensure_ascii=False, indent=1, default=str)
            head = (f"[E{n}] {ev['kind']} from {ev['source']}{' ' + ev['sender'] if ev['sender'] else ''} at "
                    f"{_t(ev['created_at'])}" + (f", thread [{ev['thread']}]" if ev["thread"] else "")
                    + (f", routed to {', '.join(f'[W{w}]' for w in ev['routed_to'])}" if ev["routed_to"] else "")
                    + f", verdicts: {'; '.join(v['by'] + ' ' + v['verdict'] for v in ev['verdicts']) or 'none'}\n")
            return head + _page(body, ref, offset, limit)
        if kind == "W":
            t = store.task(n)
            if not t:
                return f"No worker task {ref}."
            threads = store.threads_of(n)
            evs = store.all("SELECT id FROM incoming WHERE routed_to LIKE ? ORDER BY id DESC LIMIT 10", (f"%{n}%",))
            evs = [e["id"] for e in evs if n in json.loads(store.one("SELECT routed_to FROM incoming WHERE id=?", (e["id"],))["routed_to"])]
            objs = store.all("SELECT id,label FROM objects WHERE task_id=? ORDER BY id DESC LIMIT 8", (n,))
            tools = [e for e in store.events(n) if e["kind"] == "tool"][-8:]
            lines = [f"[W{n}] \"{t['title']}\" status {t['status']}, mode {t['mode']}, machine {t['machine'] or 'default'}, "
                     f"origin {t['origin'] or '-'}, {t['steps']} steps, ${t['cost_usd']:.3f}, ended {_t(t['ended_at'])}",
                     f"instructions: {_one(t['prompt'], 600)}"]
            if t["result"] or t["error"]:
                lines.append(f"result: {_one(t['result'] or t['error'], 1500)}")
            if threads:
                lines.append("threads: " + " ".join(f"[thread:{x}]" for x in threads))
            if evs:
                lines.append("events handled: " + " ".join(f"[E{e}]" for e in evs))
            if tools:
                lines.append("last steps: " + "; ".join(_one(e["data"].get("summary", ""), 60) for e in tools))
            if objs:
                lines.append("spilled: " + " ".join(f"[O{o['id']}] {o['label']}" for o in objs))
            return "\n".join(lines)
        obj = store.object(n)
        if not obj:
            return f"No object {ref}."
        return f"[O{n}] {obj['label']} (task [W{obj['task_id']}])\n" + _page(obj["content"], ref, offset, limit)
    if ref.startswith("thread:"):
        key = ref[len("thread:"):]
        evs = store.all("SELECT * FROM incoming WHERE thread=? ORDER BY id DESC LIMIT 20", (key,))
        subs = store.subscribers(key, live_only=False)
        out = [f"[thread:{key}] subscribers " + (", ".join(f"[W{s}]" for s in subs) or "none")]
        out += ["  " + ev_line(e) for e in reversed(evs)]
        return "\n".join(out)
    if ref.startswith("chat"):
        channel = ref.split(":", 1)[1] if ":" in ref else None
        msgs = store.chat(channel, limit=60)
        return "\n".join(f"[{_t(m['created_at'])}] {m['role']}@{m['channel']}: {_one(m['text'], 300)}" for m in msgs) or "No chat."
    return f"Unknown ID '{ref}'. IDs look like E12, W7, O31, thread:<key>, chat:<channel>."


READ_TOOL = {
    "name": "read",
    "description": "Read an object by its ID: E<n> (event), W<n> (worker task), O<n> (spilled output), thread:<key>, "
                   "chat:<channel>. Results include the IDs of related objects you can read next.",
    "input_schema": {"type": "object", "properties": {
        "id": {"type": "string"}, "offset": {"type": "integer", "description": "for long content"},
        "limit": {"type": "integer"}}, "required": ["id"]},
}

__all__ = ["render_event", "spill", "read", "ev_line", "task_line", "READ_TOOL", "time", "Any"]
