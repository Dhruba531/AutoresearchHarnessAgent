"""Event-driven triggers: things happening on or around the Mac wake the agent.

Each trigger watches a source and, when it fires, sends an event — with the
user's standing instruction ("file receipts into ~/Documents/Receipts") and
the payload — to the router, which decides whether to dispatch a worker,
tell the user, or ignore it. Bursts are cheap: the router batches them.

  folder    new files in a folder (waits until a file stops growing, skips
            partial downloads)                     {path, pattern="*"}
  mail      new unread messages in Mail's inbox    {query?}
  calendar  events about to start                  {minutes_before=15}
  system    battery or disk below a threshold      {battery_below?, disk_free_below_gb?}
  webhook   POST /api/hooks/<id> with the trigger's secret (iOS Shortcuts,
            IFTTT, Home Assistant, GitHub…)         {}

The first poll of a new trigger records a baseline, so existing files and
mail do not fire.
"""

from __future__ import annotations

import asyncio
import fnmatch
import hmac
import json
import logging
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import scripts
from .config import Settings
from .host import Host
from .policy import is_relay_home
from .store import Store

log = logging.getLogger("relay.triggers")
KINDS = {"folder": 15, "mail": 120, "calendar": 60, "system": 300, "webhook": 0, "github": 0}  # default poll seconds
PARTIAL = (".crdownload", ".download", ".part", ".partial", ".tmp", ".icloud")


class TriggerError(ValueError):
    pass


def _subject_key(subject: str) -> str:
    s = re.sub(r"^\s*((re|fwd?|aw|wg)\s*:\s*)+", "", subject or "", flags=re.I)
    return re.sub(r"\s+", " ", s).strip().lower()[:80]


def describe(t: dict, item: Any) -> tuple[str, str | None, str | None]:
    """(text, sender, thread key) for one trigger item."""
    kind = t["kind"]
    if not isinstance(item, dict):
        return str(item)[:2000], None, f"trigger:{t['id']}"
    if kind == "folder":
        return (f"new {'folder' if item.get('folder') else 'file'} {item.get('name')} ({item.get('size')} bytes) in "
                f"{Path(item.get('path', '')).parent}", None, f"folder:{t['id']}")
    if kind == "mail":
        return (f"email from {item.get('from')}: {item.get('subject')}", item.get("from"),
                f"mail:{_subject_key(item.get('subject', ''))}")
    if kind == "calendar":
        return (f"{item.get('title')} starts in {item.get('starts_in_min')} min" +
                (f" at {item['location']}" if item.get("location") else ""), None, f"cal:{item.get('title')}|{item.get('start')}")
    if kind == "system":
        return ("; ".join(f"{k.replace('_', ' ')} {v}" for k, v in item.items()), None, f"system:{t['id']}")
    if kind == "github":
        return str(item.get("text", ""))[:4000], item.get("sender"), item.get("thread") or f"gh:{t['id']}"
    text = item.get("text") or item.get("message") or json.dumps(item, ensure_ascii=False, default=str)
    return str(text)[:4000], item.get("from") or item.get("sender"), \
        (f"hook:{t['id']}:{item['thread']}" if item.get("thread") else f"hook:{t['id']}")


def github_item(event: str, p: dict) -> dict | None:
    """One GitHub webhook delivery → an event (text, sender, thread per issue/PR), or None for noise."""
    repo = (p.get("repository") or {}).get("full_name") or "?"
    who = "@" + ((p.get("sender") or {}).get("login") or "someone")
    action = p.get("action") or ""
    clip = lambda s, n=600: (s or "").strip()[:n]  # noqa: E731
    issue, pr = p.get("issue") or {}, p.get("pull_request") or {}
    number = issue.get("number") or pr.get("number")
    kind = "PR" if pr or issue.get("pull_request") else "issue"
    thread = f"gh:{repo}#{number}" if number else f"gh:{repo}"
    meta = {"repo": repo, "number": number, "url": (issue or pr).get("html_url"), "event": event, "action": action}
    if event == "issues" and action in {"opened", "reopened", "closed", "assigned", "labeled"}:
        text = f"{repo}: issue #{number} {action} by {who}: {issue.get('title')}\n{clip(issue.get('body')) if action == 'opened' else ''}"
    elif event == "issue_comment" and action == "created":
        text = f"{repo}: {who} commented on {kind} #{number} ({issue.get('title')}): {clip((p.get('comment') or {}).get('body'))}"
    elif event == "pull_request" and action in {"opened", "reopened", "closed", "ready_for_review", "review_requested"}:
        merged = " (merged)" if action == "closed" and pr.get("merged") else ""
        text = f"{repo}: PR #{number} {action}{merged} by {who}: {pr.get('title')}\n{clip(pr.get('body')) if action == 'opened' else ''}"
    elif event == "pull_request_review" and action == "submitted":
        rv = p.get("review") or {}
        text = f"{repo}: {who} reviewed PR #{number} ({pr.get('title')}): {rv.get('state')} — {clip(rv.get('body'))}"
    elif event == "pull_request_review_comment" and action == "created":
        c = p.get("comment") or {}
        text = f"{repo}: {who} commented on PR #{number} at {c.get('path')}: {clip(c.get('body'))}"
    elif event == "workflow_run" and action == "completed" and (p.get("workflow_run") or {}).get("conclusion") == "failure":
        w = p["workflow_run"]
        thread = f"gh:{repo}:ci:{w.get('head_branch')}"
        meta.update(number=None, url=w.get("html_url"))
        text = f"{repo}: CI failed — {w.get('name')} on {w.get('head_branch')} ({clip(w.get('display_title'), 120)})"
    elif event == "push" and p.get("commits"):
        branch = str(p.get("ref", "")).removeprefix("refs/heads/")
        thread = f"gh:{repo}:push:{branch}"
        text = f"{repo}: {who} pushed {len(p['commits'])} commit(s) to {branch}: " + "; ".join(
            clip(c.get("message"), 80) for c in p["commits"][:5])
    else:
        return None  # stars, pings, label edits…: not worth an event
    return {"text": text.strip(), "sender": who, "thread": thread, "_github": meta}


class Triggers:
    def __init__(self, store: Store, host: Host, settings: Settings, router=None):
        self.store, self.host, self.settings = store, host, settings
        self.router = router

    # -- management ------------------------------------------------------------
    def create(self, name: str, kind: str, config: dict, instruction: str) -> dict:
        if kind not in KINDS:
            raise TriggerError(f"kind must be one of {', '.join(KINDS)}")
        config = dict(config or {})
        if kind == "folder":
            if not config.get("path"):
                raise TriggerError("a folder trigger needs a path")
            p = Path(config["path"]).expanduser().resolve()
            if is_relay_home(p, self.settings.home):
                raise TriggerError("Relay's own data folder cannot be watched")
            config["path"] = str(p)
            config.setdefault("pattern", "*")
        if kind == "calendar":
            config["minutes_before"] = int(config.get("minutes_before") or 15)
        config["interval_s"] = max(5, int(config.get("interval_s") or KINDS[kind] or 0)) if KINDS[kind] else 0
        secret = "hk_" + secrets.token_urlsafe(24) if kind in {"webhook", "github"} else None
        cur = self.store.x(
            "INSERT INTO triggers(name,kind,config,instruction,secret,created_at) VALUES(?,?,?,?,?,?)",
            (name.strip()[:80] or kind, kind, json.dumps(config), instruction.strip()[:2000], secret, time.time()),
        )
        return self.store.trigger(int(cur.lastrowid))

    def delete(self, trigger_id: int) -> None:
        self.store.x("DELETE FROM triggers WHERE id=?", (trigger_id,))

    def set_enabled(self, trigger_id: int, enabled: bool) -> None:
        self.store.x("UPDATE triggers SET enabled=? WHERE id=?", (1 if enabled else 0, trigger_id))

    def _save(self, t: dict, **fields: Any) -> None:
        if "state" in fields:
            fields["state"] = json.dumps(fields["state"])
        sets = ",".join(f"{k}=?" for k in fields)
        self.store.x(f"UPDATE triggers SET {sets} WHERE id=?", (*fields.values(), t["id"]))

    def fire(self, t: dict, items: list[dict] | dict) -> None:
        """One event per item (each email, file, calendar event), so the filter judges each on its own and
        follow-ups join the right Thread. The router batches them anyway."""
        items = items if isinstance(items, list) else [items]
        self.store.x("UPDATE triggers SET last_fired=?, fire_count=fire_count+1, last_error=NULL WHERE id=?",
                     (time.time(), t["id"]))
        if self.router is None:
            return
        meta = {k: t[k] for k in ("id", "name", "kind", "instruction")}
        for item in items:
            text, sender, thread = describe(t, item)
            self.router.submit({"kind": "trigger", "source": f"trigger:{t['id']}", "reply": "all", "sender": sender,
                                "text": text, "payload": item if isinstance(item, dict) else {"value": item},
                                "thread": thread, "trigger": meta})

    def github_hook(self, trigger_id: int, signature: str | None, event: str, raw: bytes) -> bool:
        """GitHub signs each delivery with the trigger's secret (X-Hub-Signature-256); unsigned or wrong: refused."""
        t = self.store.trigger(trigger_id)
        if not t or t["kind"] != "github" or not t["enabled"] or not t["secret"] or not signature:
            return False
        want = "sha256=" + hmac.new(t["secret"].encode(), raw, "sha256").hexdigest()
        if not hmac.compare_digest(want, signature):
            return False
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            return False
        item = github_item(event, payload)
        if item is not None:
            self.fire(t, item)
        return True  # accepted (a ping or an event Relay ignores still gets 200, so GitHub doesn't retry)

    def webhook(self, trigger_id: int, secret: str | None, payload: Any) -> bool:
        t = self.store.trigger(trigger_id)
        if not t or t["kind"] != "webhook" or not t["enabled"] or not secret or not t["secret"]:
            return False
        if not hmac.compare_digest(t["secret"], secret):
            return False
        self.fire(t, payload)
        return True

    # -- polling -----------------------------------------------------------------
    async def run(self) -> None:
        while True:
            for t in self.store.triggers(enabled_only=True):
                if not t["config"].get("interval_s"):
                    continue
                if time.time() - t["state"].get("polled", 0) < t["config"]["interval_s"]:
                    continue
                try:
                    await self.poll(t)
                except Exception as exc:  # a broken trigger must not stop the others
                    log.warning("trigger %s failed: %s", t["id"], exc)
                    self._save(t, last_error=str(exc)[:500], state={**t["state"], "polled": time.time()})
            await asyncio.sleep(2)

    async def poll(self, t: dict) -> None:
        state = dict(t["state"])
        first = "polled" not in state
        fire_with = await getattr(self, f"_poll_{t['kind']}")(t["config"], state, first)
        state["polled"] = time.time()
        self._save(t, state=state)
        if fire_with and not first:
            self.fire(self.store.trigger(t["id"]), fire_with)

    async def _poll_folder(self, cfg: dict, state: dict, first: bool) -> Any:
        root = Path(cfg["path"])
        if not root.is_dir():
            raise TriggerError(f"folder not found: {root}")
        seen: dict = state.get("seen", {})
        now: dict = {}
        ready = []
        for p in root.iterdir():
            if p.name.startswith(".") or p.name.lower().endswith(PARTIAL) or not fnmatch.fnmatch(p.name, cfg.get("pattern", "*")):
                continue
            try:
                size = p.stat().st_size if p.is_file() else -1
            except OSError:
                continue
            prev = seen.get(p.name)
            entry = {"size": size, "fired": True if first else bool(prev and prev["fired"])}
            if not entry["fired"] and prev is not None and prev["size"] == size:  # stopped growing since last poll
                entry["fired"] = True
                ready.append({"name": p.name, "path": str(p), "size": size, "folder": p.is_dir()})
            now[p.name] = entry
        state["seen"] = now
        return ready or None

    async def _poll_mail(self, cfg: dict, state: dict, first: bool) -> Any:
        proc = await self.host.osascript(scripts.MAIL_RECENT, [json.dumps({"query": cfg.get("query"), "limit": 25})],
                                         language=scripts.JXA, timeout=120)
        if proc.code != 0:
            raise TriggerError(proc.err.strip() or "Mail could not be read")
        msgs = json.loads(proc.out or "[]")
        seen = set(state.get("seen", []))
        new = [m for m in msgs if m.get("unread", True) and m.get("id") not in seen]
        state["seen"] = (list(seen) + [m.get("id") for m in msgs])[-500:]
        return [{k: m.get(k) for k in ("id", "from", "subject", "date")} for m in new] or None

    async def _poll_calendar(self, cfg: dict, state: dict, first: bool) -> Any:
        proc = await self.host.osascript(scripts.CALENDAR_EVENTS, [json.dumps({"days": 1})], language=scripts.JXA, timeout=120)
        if proc.code != 0:
            raise TriggerError(proc.err.strip() or "Calendar could not be read")
        window = int(cfg.get("minutes_before", 15)) * 60
        fired = set(state.get("fired", []))
        soon = []
        for ev in json.loads(proc.out or "[]"):
            try:
                start = datetime.fromisoformat(ev["start"].replace("Z", "+00:00"))
            except (KeyError, ValueError):
                continue
            secs = (start - datetime.now(timezone.utc)).total_seconds()
            key = f"{ev.get('title')}|{ev['start']}"
            if 0 <= secs <= window and key not in fired:
                soon.append({**ev, "starts_in_min": round(secs / 60)})
                fired.add(key)
        state["fired"] = list(fired)[-200:]
        return soon or None

    async def _poll_system(self, cfg: dict, state: dict, first: bool) -> Any:
        alerts = []
        if cfg.get("battery_below") is not None:
            out = (await self.host.run(["pmset", "-g", "batt"], timeout=10)).out
            m = re.search(r"(\d+)%", out)
            if m:  # a Mac mini has no battery; this is for laptops and UPS-backed setups
                pct, thr = int(m.group(1)), int(cfg["battery_below"])
                prev = state.get("battery_low", False)
                low = pct < (thr + 5 if prev else thr)  # 5-point hysteresis so it fires once per drop
                if low and not prev and "discharging" in out:
                    alerts.append({"battery_percent": pct})
                state["battery_low"] = low
        if cfg.get("disk_free_below_gb") is not None:
            out = (await self.host.run(["df", "-k", "/"], timeout=10)).out.splitlines()
            if len(out) > 1:
                free_gb = int(out[1].split()[3]) / 1024 / 1024
                low = free_gb < float(cfg["disk_free_below_gb"])
                if low and not state.get("disk_low"):
                    alerts.append({"disk_free_gb": round(free_gb, 1)})
                state["disk_low"] = low
        return alerts or None
