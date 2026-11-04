"""The Watchdog: a control loop outside the main system (design note §11).

Process checks are not enough for an agent: most failures are not crashes. The
process runs, but a worker is stuck in a loop or drifting from its goal. So the
Watchdog asks two questions on a schedule, from its own thread and its own
event loop (it keeps working when the main loop is wedged):

  alive?     main event loop heartbeat, database, model API errors, machines
             that tasks wait on, triggers that keep failing
  progress?  per running worker: code heuristics first (the same action
             repeated, the same failure repeated, no new step for too long),
             then a light model judges the suspicious ones: moving, stuck, or
             drifting from the goal

It fixes what it can — a nudge into the worker's inbox ("you have re-run the
same test six times with the same failure; stop retrying and read the log"),
and if that does not help, it pauses the worker and tells you. It is itself an
agent: you can ask it "why did #12 stall?", and it can propose changes to the
running system (a filter policy, worker guidance, an automation switched off)
that apply only when you approve them.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import threading
import time
from collections import Counter
from typing import Any

from .context import READ_TOOL, read as read_object, task_line
from .llm import LLMError
from .store import Store

log = logging.getLogger("relay.watchdog")

ASSESS_SYSTEM = """You are the Watchdog of Relay, a personal agent on the user's Mac. You judge whether a worker \
task is making real progress. Given its goal and its recent steps, call assess once: "moving" if it is getting \
somewhere, "stuck" if it repeats itself or waits on something that won't change, "drifting" if it is doing \
something other than its goal. If stuck or drifting, give a one-sentence nudge the worker should follow."""

ASSESS_TOOL = {"name": "assess", "description": "Your judgment.", "input_schema": {"type": "object", "properties": {
    "verdict": {"type": "string", "enum": ["moving", "stuck", "drifting"]},
    "why": {"type": "string"}, "nudge": {"type": "string"}}, "required": ["verdict", "why"]}}

CHAT_SYSTEM = """You are the Watchdog of Relay, talking with the person who maintains it (the user). You watch \
the system from outside: liveness and whether each worker is progressing. Answer their questions about why \
things stalled or failed, using your reports and by reading tasks (read W<n>, E<n>, O<n>). Be brief and \
specific. When a change to the running system would help, call propose_change — it applies only if they \
approve it: add_policy {text} (a filter/router policy), disable_policy {policy_id}, worker_guidance {text} \
(instructions every new worker follows), trigger_enabled {trigger_id, enabled}."""

PROPOSE_TOOL = {"name": "propose_change", "description": "Propose a change; the user approves or rejects it.",
                "input_schema": {"type": "object", "properties": {
                    "kind": {"type": "string", "enum": ["add_policy", "disable_policy", "worker_guidance", "trigger_enabled"]},
                    "data": {"type": "object"}, "summary": {"type": "string"}}, "required": ["kind", "data", "summary"]}}


def _key(e: dict) -> str:
    d = e["data"]
    return hashlib.sha1(f"{d.get('name')}|{d.get('summary')}".encode()).hexdigest()[:12]


class Watchdog:
    def __init__(self, settings, store: Store, agent, adapter_factory, *, interval_s: int = 300, stall_s: int = 900,
                 restart_on_hang: bool = False):
        self.settings, self.store, self.agent = settings, store, agent
        self.adapter_factory = adapter_factory
        self.interval_s, self.stall_s = interval_s, stall_s
        self.restart_on_hang = restart_on_hang
        self.main_loop: asyncio.AbstractEventLoop | None = None
        self.nudged: dict[int, float] = {}
        self.thread: threading.Thread | None = None
        self.alert_cb = None  # set by the server to push alerts to phones

    # -- running outside the main loop ------------------------------------------------
    def start_thread(self, main_loop: asyncio.AbstractEventLoop) -> None:
        self.main_loop = main_loop
        self.thread = threading.Thread(target=lambda: asyncio.run(self._forever()), name="relay-watchdog", daemon=True)
        self.thread.start()

    async def _forever(self) -> None:
        while True:
            await asyncio.sleep(self.interval_s)
            try:
                await self.check()
            except Exception:
                log.exception("watchdog check failed")

    def _on_main(self, fn, *args) -> None:
        """Act on the main system from outside it, safely."""
        if self.main_loop is not None and self.main_loop.is_running():
            try:
                if asyncio.get_running_loop() is self.main_loop:
                    fn(*args)
                    return
            except RuntimeError:
                pass
            self.main_loop.call_soon_threadsafe(fn, *args)
        else:
            fn(*args)

    # -- the check ------------------------------------------------------------------------
    async def check(self) -> dict:
        now = time.time()
        infra: dict[str, Any] = {}
        beat = now - self.agent.heartbeat
        infra["main loop"] = "ok" if beat < 30 else f"no heartbeat for {beat:.0f}s"
        try:
            self.store.one("SELECT 1 AS ok")
            infra["database"] = "ok"
        except Exception as exc:
            infra["database"] = f"error: {exc}"
        errs = self.store.all("SELECT id, error FROM tasks WHERE status='failed' AND ended_at>? AND "
                              "(error LIKE '%API%' OR error LIKE '%reach%' OR error LIKE '%rate limit%')", (now - self.interval_s * 2,))
        infra["model API"] = "ok" if not errs else f"{len(errs)} recent failures: {errs[-1]['error'][:80]}"
        waiting_on = self.store.all("SELECT machine, COUNT(*) n FROM tasks WHERE status IN ('queued','waiting','running') "
                                    "AND machine IS NOT NULL GROUP BY machine")
        offline = [f"{w['machine']} ({w['n']} tasks)" for w in waiting_on if not self.agent.machines.online(w["machine"])]
        infra["machines"] = "ok" if not offline else "offline with waiting tasks: " + ", ".join(offline)
        bad_triggers = [t for t in self.store.triggers(enabled_only=True) if t.get("last_error")]
        infra["triggers"] = "ok" if not bad_triggers else "; ".join(f"#{t['id']} {t['name']}: {t['last_error'][:60]}" for t in bad_triggers)

        progress, actions = [], []
        for tid in list(self.agent.running):
            task = self.store.task(tid)
            if not task:
                continue
            verdict, why, nudge = self._heuristics(task, now)
            if verdict != "moving" and self.adapter_factory is not None:
                verdict, why, nudge = await self._assess(task, verdict, why, nudge)
            progress.append({"task_id": tid, "title": task["title"], "verdict": verdict, "why": why})
            if verdict in {"stuck", "drifting"}:
                actions.append(self._intervene(task, why, nudge, now))
        if infra["main loop"] != "ok" and self.restart_on_hang:
            actions.append("main loop hung: exiting so launchd restarts Relay; workers resume from the database")
        report = {"infra": infra, "progress": progress, "actions": [a for a in actions if a]}
        row = self.store.add_watchdog("report", report)
        self._on_main(self.agent.bus.publish, {"type": "watchdog", "report": row})
        if self.restart_on_hang and infra["main loop"] != "ok":
            os._exit(3)
        return report

    def _heuristics(self, task: dict, now: float) -> tuple[str, str, str | None]:
        evs = self.store.events(task["id"])
        tools = [e for e in evs if e["kind"] == "tool"][-12:]
        if task["status"] == "waiting":
            return "moving", "waiting on the user or a job", None
        counts = Counter(_key(e) for e in tools)
        if counts:
            k, n = counts.most_common(1)[0]
            sample = next(e for e in tools if _key(e) == k)
            fails = sum(1 for e in tools if _key(e) == k and not e["data"].get("ok"))
            if n >= 6 or (n >= 4 and fails >= 3):
                return ("stuck", f"repeated “{sample['data'].get('summary', '')[:80]}” {n} times"
                        + (f", failing {fails}×" if fails else ""),
                        "You have repeated the same action with the same result several times. Stop retrying; "
                        "read the error output carefully, and try a different approach or report what blocks you.")
        last = evs[-1]["created_at"] if evs else task["started_at"] or now
        if now - last > self.stall_s and not self.agent.live_jobs(task["id"]):
            return "stuck", f"no new step for {int((now - last) / 60)} min", None
        return "moving", f"{len(tools)} recent steps", None

    async def _assess(self, task: dict, verdict: str, why: str, nudge: str | None) -> tuple[str, str, str | None]:
        evs = [e for e in self.store.events(task["id"]) if e["kind"] in {"tool", "note", "message", "event_in", "job"}][-15:]
        steps = "\n".join(f"- {e['kind']}: {(e['data'].get('summary') or e['data'].get('text') or '')[:160]}"
                          + ("" if e["data"].get("ok", True) else " (failed)") for e in evs)
        prompt = (f"Goal of worker W{task['id']}: {task['prompt'][:800]}\n\nCode check says: {verdict} — {why}\n\n"
                  f"Recent steps:\n{steps}")
        try:
            turn = await self.adapter_factory().step(ASSESS_SYSTEM, [{"role": "user", "content": prompt}], [ASSESS_TOOL])
        except LLMError:
            return verdict, why, nudge
        self.store.add_spend(None, turn.cost_usd)
        for b in turn.content:
            if b.get("type") == "tool_use" and b.get("name") == "assess":
                i = b.get("input") or {}
                return i.get("verdict", verdict), i.get("why", why), i.get("nudge") or nudge
        return verdict, why, nudge

    def _intervene(self, task: dict, why: str, nudge: str | None, now: float) -> str:
        tid = task["id"]
        last = self.nudged.get(tid)
        if last is None or now - last > 15 * 60:
            self.nudged[tid] = now
            text = f"Watchdog: {nudge or 'You seem stuck: ' + why + '. Step back and change approach, or report what blocks you.'}"
            self._on_main(self.agent.deliver, tid, text, "watchdog")
            return f"nudged W{tid}: {why}"
        # nudged recently and still stuck: pause it and tell the user
        self._on_main(self.agent.pause, tid)
        msg = self.store.add_watchdog("chat_watchdog", {"text": f"I paused #{tid} “{task['title']}”: still stuck after a nudge "
                                                                f"({why}). Ask me why, or resume it from the task."})
        self._on_main(self.agent.bus.publish, {"type": "watchdog_chat", "message": msg})
        if self.alert_cb:
            self._on_main(self.alert_cb, msg["data"]["text"])
        return f"paused W{tid}: still stuck after a nudge"

    # -- talking with the maintainer -----------------------------------------------------
    async def chat(self, text: str) -> dict:
        self.store.add_watchdog("chat_user", {"text": text})
        reports = self.store.watchdog_log(("report",), limit=3)
        convo = self.store.watchdog_log(("chat_user", "chat_watchdog"), limit=12)
        running = [self.store.task(t) for t in self.agent.running]
        recent = self.store.all("SELECT * FROM tasks ORDER BY id DESC LIMIT 10")
        ctx = ("<latest reports>\n" + "\n".join(json.dumps(r["data"], default=str)[:1500] for r in reports) + "\n</latest reports>\n"
               "<workers>\n" + "\n".join(task_line(t) for t in running if t) + "\nrecent:\n" +
               "\n".join(task_line(t) for t in recent) + "\n</workers>\n<policies>\n" +
               "\n".join(f"#{p['id']} {'on' if p['enabled'] else 'off'}: {p['text']}" for p in self.store.policies()) +
               f"\n</policies>\n<worker guidance>\n{self.store.get('worker_guidance') or '(none)'}\n</worker guidance>\n"
               "<conversation>\n" + "\n".join(f"{'user' if c['kind'] == 'chat_user' else 'watchdog'}: {c['data']['text']}" for c in convo)
               + "\n</conversation>")
        history: list[dict] = [{"role": "user", "content": ctx}]
        reply_text = ""
        proposals = []
        try:
            adapter = self.adapter_factory()
            for _ in range(4):
                turn = await adapter.step(CHAT_SYSTEM, history, [READ_TOOL, PROPOSE_TOOL])
                self.store.add_spend(None, turn.cost_usd)
                calls = [b for b in turn.content if b.get("type") == "tool_use"]
                reply_text = " ".join(b.get("text", "") for b in turn.content if b.get("type") == "text").strip() or reply_text
                if not calls:
                    break
                results = []
                for b in calls:
                    inp = b.get("input") or {}
                    if b["name"] == "read":
                        out = read_object(self.store, str(inp.get("id", "")))
                    else:
                        pid = self.store.add_proposal(str(inp.get("kind")), inp.get("data") or {}, str(inp.get("summary", ""))[:300])
                        proposals.append(pid)
                        out = f"proposal #{pid} recorded; the user will approve or reject it"
                    results.append({"type": "tool_result", "tool_use_id": b["id"], "content": out[:6000]})
                history += [{"role": "assistant", "content": turn.content}, {"role": "user", "content": results}]
        except LLMError as exc:
            reply_text = f"I can't reach my model right now ({exc}). Latest report: " + \
                         (json.dumps(reports[-1]["data"], default=str)[:600] if reports else "none yet")
        msg = self.store.add_watchdog("chat_watchdog", {"text": reply_text or "Done.", "proposals": proposals})
        return msg

    def apply(self, proposal_id: int, approve: bool) -> dict:
        p = next((x for x in self.store.proposals() if x["id"] == proposal_id), None)
        if p is None or p["status"] != "pending":
            raise ValueError("no such pending proposal")
        if approve:
            d = p["data"]
            if p["kind"] == "add_policy":
                self.store.add_policy(str(d["text"]), by="watchdog")
            elif p["kind"] == "disable_policy":
                self.store.x("UPDATE policies SET enabled=0 WHERE id=?", (int(d["policy_id"]),))
            elif p["kind"] == "worker_guidance":
                self.store.put("worker_guidance", str(d["text"])[:4000])
            elif p["kind"] == "trigger_enabled":
                self.store.x("UPDATE triggers SET enabled=? WHERE id=?", (1 if d.get("enabled") else 0, int(d["trigger_id"])))
            else:
                raise ValueError(f"unknown proposal kind {p['kind']}")
        self.store.x("UPDATE proposals SET status=?, decided_at=? WHERE id=?",
                     ("applied" if approve else "rejected", time.time(), proposal_id))
        self.store.add_watchdog("chat_watchdog", {"text": f"Proposal #{proposal_id} {'applied' if approve else 'rejected'}: {p['summary']}"})
        return next(x for x in self.store.proposals() if x["id"] == proposal_id)
