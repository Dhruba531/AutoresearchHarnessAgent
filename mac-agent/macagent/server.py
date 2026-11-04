"""The API the phone talks to, plus the phone web app itself.

Unauthenticated: the app shell, /api/health, and /api/pair (rate-limited).
Everything else needs a device token from pairing, as a bearer header or,
for the WebSocket and <img> URLs, a `token` query parameter.

Manual control (/api/control) refuses while a task is driving the screen,
unless the phone first takes over (which pauses that task).
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .agent import Agent, local_midnight, shot_path
from .auth import RateLimiter, device_for_token, list_devices, new_pairing_code, redeem_pairing_code, revoke_device
from .config import Settings
from .policy import POLICIES
from .store import Store

WEB = Path(__file__).resolve().parent / "web"


class PairIn(BaseModel):
    code: str = Field(min_length=4, max_length=20)
    name: str = Field(default="phone", max_length=60)


class TaskIn(BaseModel):
    prompt: str = Field(min_length=1, max_length=20_000)
    run_at: float | None = None
    repeat: str | None = None


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)


class DecisionIn(BaseModel):
    approve: bool = True
    answer: str | None = Field(default=None, max_length=5_000)


class ControlIn(BaseModel):
    action: str
    x: int | None = None
    y: int | None = None
    x2: int | None = None
    y2: int | None = None
    text: str | None = Field(default=None, max_length=5_000)
    direction: str | None = None
    amount: int | None = None
    force: bool = False
    machine: str | None = None


class PolicyIn(BaseModel):
    text: str = Field(min_length=3, max_length=2000)
    enabled: bool = True


class PolicyPatch(BaseModel):
    text: str | None = Field(default=None, max_length=2000)
    enabled: bool | None = None


class MachineRegisterIn(BaseModel):
    code: str = Field(min_length=4, max_length=20)
    name: str = Field(min_length=1, max_length=60)


class ProposalIn(BaseModel):
    approve: bool


class ChatIn(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)


class TriggerIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    kind: str
    config: dict = Field(default_factory=dict)
    instruction: str = Field(min_length=3, max_length=2000)


class TriggerPatch(BaseModel):
    enabled: bool


class SettingsIn(BaseModel):
    policy: str | None = None
    effort: str | None = None


def token_of(request: Request) -> str | None:
    h = request.headers.get("authorization", "")
    return h[7:].strip() if h.lower().startswith("bearer ") else request.query_params.get("token")


def create_app(settings: Settings, store: Store, agent: Agent, extra_startup=None, *, router=None,
               triggers=None, watchdog=None) -> FastAPI:
    limiter = RateLimiter(limit=8, window_s=60)  # pairing
    hook_limiter = RateLimiter(limit=60, window_s=60)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        saved = store.get("settings", {})
        if saved.get("policy") in POLICIES:
            settings.policy = saved["policy"]
        if saved.get("effort"):
            settings.effort = saved["effort"]
        agent.start()
        jobs = list(extra_startup() if extra_startup else [])
        if router is not None:
            jobs.append(router.run())
        if triggers is not None:
            jobs.append(triggers.run())
        tasks = [asyncio.create_task(c) for c in jobs]
        if watchdog is not None and watchdog.interval_s > 0:
            watchdog.start_thread(asyncio.get_running_loop())
        yield
        for t in tasks:
            t.cancel()
        await agent.stop()

    app = FastAPI(title="Relay", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)

    def device(request: Request) -> dict:
        d = device_for_token(store, token_of(request))
        if d is None:
            raise HTTPException(401, "Pair this device first.")
        return d

    def state() -> dict:
        return {
            "tasks": store.tasks(40),
            "approvals": store.pending_approvals(),
            "host": agent.host.info() if agent.host else {"host": "none"},
            "machines": agent.machines.status(),
            "current": agent.current,
            "running": sorted(agent.running),
            "screen_owner": agent.screen_owner,
            "screen_busy": agent.screen_busy(),
            "router": {"enabled": router is not None, "busy": bool(router and router.busy),
                       "model": settings.router_model},
            "workers": settings.max_workers,
            "watchdog": (store.watchdog_log(("report",), 1) or [None])[-1],
            "settings": {"policy": settings.policy, "provider": settings.provider, "model": settings.model,
                         "effort": settings.effort, "can_see_screen": settings.can_see_screen,
                         "daily_cap_usd": settings.daily_cap_usd, "telegram": bool(settings.telegram_token),
                         "ntfy": bool(settings.ntfy_topic)},
            "spend_today": round(store.spend_since(local_midnight()), 4),
            "version": __version__,
        }

    # -- public --------------------------------------------------------------------
    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "version": __version__, "host": agent.host.name}

    @app.post("/api/pair")
    def pair(body: PairIn, request: Request) -> dict:
        ip = request.client.host if request.client else "?"
        if not limiter.allow(ip):
            raise HTTPException(429, "Too many attempts; wait a minute.")
        token = redeem_pairing_code(store, body.code, body.name)
        if token is None:
            raise HTTPException(403, "That code is wrong, used, or expired. Run `python -m macagent pair` on the Mac.")
        return {"token": token}

    # -- state & tasks ------------------------------------------------------------------
    @app.get("/api/state")
    def get_state(_d: dict = Depends(device)) -> dict:
        return state()

    @app.post("/api/tasks")
    def create_task(body: TaskIn, d: dict = Depends(device)) -> dict:
        try:
            tid = agent.submit(body.prompt, source=f"app:{d['name']}", run_at=body.run_at, repeat=body.repeat)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return store.task(tid)

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: int, after: int = 0, _d: dict = Depends(device)) -> dict:
        task = store.task(task_id)
        if task is None:
            raise HTTPException(404, "No such task.")
        return {"task": task, "events": store.events(task_id, after)}

    @app.post("/api/tasks/{task_id}/message")
    def message(task_id: int, body: MessageIn, _d: dict = Depends(device)) -> dict:
        try:
            return {"result": agent.message(task_id, body.text)}
        except KeyError as exc:
            raise HTTPException(404, "No such task.") from exc

    @app.post("/api/tasks/{task_id}/{action}")
    def task_action(task_id: int, action: str, _d: dict = Depends(device)) -> dict:
        if store.task(task_id) is None:
            raise HTTPException(404, "No such task.")
        if action == "cancel":
            agent.cancel(task_id)
        elif action == "pause":
            if task_id not in agent.running:
                raise HTTPException(409, "Only a running task can be paused.")
            agent.pause(task_id)
        elif action == "resume":
            agent.resume(task_id)
        else:
            raise HTTPException(404, "Unknown action.")
        return store.task(task_id)

    @app.post("/api/approvals/{approval_id}")
    def decide(approval_id: int, body: DecisionIn, d: dict = Depends(device)) -> dict:
        if not agent.decide(approval_id, body.approve, body.answer, f"app:{d['name']}"):
            raise HTTPException(409, "Already decided or expired.")
        return store.approval(approval_id)

    @app.get("/api/shots/{task_id}/{name}")
    def shot(task_id: int, name: str, _d: dict = Depends(device)) -> FileResponse:
        p = shot_path(settings, task_id, name)
        if p is None:
            raise HTTPException(404, "No such screenshot.")
        return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})

    # -- live screen & manual control -------------------------------------------------
    def machine_host(name: str | None):
        h = agent.machines.host(name)
        if h is None:
            raise HTTPException(409, f"{name or 'that machine'} is offline.")
        return h

    @app.get("/api/screen")
    async def screen(machine: str | None = None, _d: dict = Depends(device)) -> Response:
        s = await machine_host(machine).screenshot()
        return Response(s.data, media_type=s.media_type, headers={"Cache-Control": "no-store",
                                                                   "X-Width": str(s.width), "X-Height": str(s.height)})

    @app.post("/api/control")
    async def control(body: ControlIn, d: dict = Depends(device)) -> dict:
        mname = body.machine or agent.machines.default
        if agent.screen_busy(mname):
            if not body.force:
                raise HTTPException(409, "A task is using the screen. Take over (pauses it) to control manually.")
            agent.pause(agent.screen_owners[mname])
        h = machine_host(mname)
        a = body.action
        if a in {"click", "double_click", "right_click"}:
            await h.click(body.x, body.y, "right" if a == "right_click" else "left", 2 if a == "double_click" else 1,
                          body.text or "")
        elif a == "move":
            await h.move(body.x, body.y)
        elif a == "drag":
            await h.drag([body.x, body.y], [body.x2, body.y2])
        elif a == "type":
            await h.type_text(body.text or "")
        elif a == "key":
            try:
                await h.key(body.text or "", 1)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
        elif a == "scroll":
            await h.scroll(body.x, body.y, body.direction or "down", body.amount or 3)
        else:
            raise HTTPException(400, f"Unknown action {a}.")
        store.add_event(0, "manual", {"action": a, "x": body.x, "y": body.y,
                                      "text": (body.text or "")[:80] if a == "key" else None, "device": d["name"]})
        return {"ok": True}

    # -- the router (chat) -----------------------------------------------------------
    @app.get("/api/chat")
    def chat(limit: int = 80, _d: dict = Depends(device)) -> list[dict]:
        return store.chat("web", min(limit, 300))

    @app.post("/api/chat")
    def say(body: ChatIn, d: dict = Depends(device)) -> dict:
        if router is None:  # no routing layer: straight to a worker
            tid = agent.submit(body.text, source=f"app:{d['name']}", origin="web")
            return {"task_id": tid}
        return router.human("web", body.text, sender=d["name"])

    # -- triggers -----------------------------------------------------------------------
    def _trigger_out(t: dict) -> dict:
        out = {k: v for k, v in t.items() if k not in {"state"}}
        out["hook_path"] = f"/api/hooks/{t['id']}" if t["kind"] == "webhook" else None
        return out

    @app.get("/api/triggers")
    def list_triggers(_d: dict = Depends(device)) -> list[dict]:
        return [_trigger_out(t) for t in store.triggers()]

    @app.post("/api/triggers")
    def create_trigger(body: TriggerIn, _d: dict = Depends(device)) -> dict:
        if triggers is None:
            raise HTTPException(400, "Triggers are not enabled.")
        try:
            return _trigger_out(triggers.create(body.name, body.kind, body.config, body.instruction))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.patch("/api/triggers/{trigger_id}")
    def patch_trigger(trigger_id: int, body: TriggerPatch, _d: dict = Depends(device)) -> dict:
        if triggers is None or store.trigger(trigger_id) is None:
            raise HTTPException(404, "No such trigger.")
        triggers.set_enabled(trigger_id, body.enabled)
        return _trigger_out(store.trigger(trigger_id))

    @app.delete("/api/triggers/{trigger_id}")
    def delete_trigger(trigger_id: int, _d: dict = Depends(device)) -> dict:
        if triggers is not None:
            triggers.delete(trigger_id)
        return {"ok": True}

    @app.post("/api/hooks/{trigger_id}")
    async def hook(trigger_id: int, request: Request) -> dict:
        """Public, but only with the trigger's secret (header X-Relay-Secret or ?secret=)."""
        ip = request.client.host if request.client else "?"
        if not hook_limiter.allow(ip):
            raise HTTPException(429, "Too many requests.")
        raw = (await request.body())[:20_000]
        try:
            payload: Any = json.loads(raw) if raw else {}
        except ValueError:
            payload = raw.decode("utf-8", errors="replace")
        secret = request.headers.get("x-relay-secret") or request.query_params.get("secret")
        if triggers is None or not triggers.webhook(trigger_id, secret, payload):
            raise HTTPException(404, "Unknown hook or wrong secret.")
        return {"ok": True}

    # -- the filter: incoming events, policies, code rules --------------------------------
    @app.get("/api/incoming")
    def incoming(limit: int = 60, _d: dict = Depends(device)) -> list[dict]:
        rows = store.incoming_list(min(limit, 300))
        for r in rows:
            r["payload"] = {k: v for k, v in r["payload"].items() if not k.startswith("_") or k in {"_reply", "_task_id"}}
        return rows

    @app.get("/api/policies")
    def policies(_d: dict = Depends(device)) -> list[dict]:
        return store.policies()

    @app.post("/api/policies")
    def add_policy(body: PolicyIn, d: dict = Depends(device)) -> dict:
        pid = store.add_policy(body.text, by=d["name"], enabled=body.enabled)
        return store.one("SELECT * FROM policies WHERE id=?", (pid,))

    @app.patch("/api/policies/{policy_id}")
    def patch_policy(policy_id: int, body: PolicyPatch, _d: dict = Depends(device)) -> dict:
        if body.text is not None:
            store.x("UPDATE policies SET text=? WHERE id=?", (body.text.strip(), policy_id))
        if body.enabled is not None:
            store.x("UPDATE policies SET enabled=? WHERE id=?", (1 if body.enabled else 0, policy_id))
        row = store.one("SELECT * FROM policies WHERE id=?", (policy_id,))
        if row is None:
            raise HTTPException(404, "No such policy.")
        return row

    @app.delete("/api/policies/{policy_id}")
    def delete_policy(policy_id: int, _d: dict = Depends(device)) -> dict:
        store.x("DELETE FROM policies WHERE id=?", (policy_id,))
        return {"ok": True}

    @app.get("/api/filter/rules")
    def filter_rules(_d: dict = Depends(device)) -> dict:
        if router is None:
            raise HTTPException(404, "No router.")
        return router.filter.rules()

    @app.put("/api/filter/rules")
    def put_filter_rules(body: dict, _d: dict = Depends(device)) -> dict:
        if router is None:
            raise HTTPException(404, "No router.")
        return router.filter.set_rules(body)

    # -- machines (bring your own machine) -------------------------------------------------
    @app.get("/api/machines")
    def machines(_d: dict = Depends(device)) -> list[dict]:
        return agent.machines.status()

    @app.post("/api/machines/pairing-code")
    def machine_code(request: Request, _d: dict = Depends(device)) -> dict:
        code = new_pairing_code(store)
        base = f"{request.url.scheme}://{request.url.netloc}"
        return {"code": code, "expires_in": 600,
                "command": f"python -m macagent daemon --server {base} --name <machine-name> --code {code}"}

    @app.post("/api/machines/register")
    def machine_register(body: MachineRegisterIn, request: Request) -> dict:
        ip = request.client.host if request.client else "?"
        if not limiter.allow(ip):
            raise HTTPException(429, "Too many attempts; wait a minute.")
        from .auth import _h

        code = body.code.strip().upper().replace("-", "")
        row = store.one("SELECT * FROM pairing_codes WHERE code_hash=?", (_h(code),))
        if not row or row["used"] or row["expires_at"] < time.time() or \
                store.x("UPDATE pairing_codes SET used=1 WHERE code_hash=? AND used=0", (_h(code),)).rowcount != 1:
            raise HTTPException(403, "That code is wrong, used, or expired.")
        try:
            return {"token": agent.machines.register(body.name), "name": body.name.strip()}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.delete("/api/machines/{name}")
    def revoke_machine(name: str, _d: dict = Depends(device)) -> dict:
        agent.machines.revoke(name)
        return {"ok": True}

    @app.websocket("/api/machines/ws")
    async def machine_ws(sock: WebSocket) -> None:
        name = agent.machines.machine_for_token(sock.query_params.get("token"))
        if name is None:
            await sock.close(code=4401)
            return
        await sock.accept()
        lock = asyncio.Lock()

        async def send(msg: dict) -> None:
            async with lock:
                await sock.send_json(msg)

        host = None
        try:
            hello = await sock.receive_json()
            host = agent.machines.attach(name, send, (hello or {}).get("info") or {})
            agent.bus.publish({"type": "machine", "name": name, "online": True})
            agent.wake.set()
            while True:
                host.resolve(await sock.receive_json())
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            if host is not None:
                agent.machines.detach(name, host)
                agent.bus.publish({"type": "machine", "name": name, "online": False})

    # -- the one-page system map ---------------------------------------------------------
    @app.get("/api/system")
    def system_map(hours: float = 24, _d: dict = Depends(device)) -> dict:
        """Every module of the design on one page, with live counts (the console's System view)."""
        since = time.time() - max(0.25, min(hours, 24 * 30)) * 3600
        stats = store.system_stats(since)
        trig: dict[str, int] = {}
        for t in store.triggers(enabled_only=True):
            trig[t["kind"]] = trig.get(t["kind"], 0) + 1
        workers = []
        for tid in sorted(agent.running):
            t = store.task(tid)
            if t:
                workers.append({"id": tid, "title": t["title"], "status": t["status"], "mode": t.get("mode") or "normal",
                                "machine": t.get("machine") or agent.machines.default, "steps": t["steps"],
                                "threads": store.threads_of(tid), "jobs": len(agent.live_jobs(tid)),
                                "on_screen": agent.screen_owner == tid})
        report = (store.watchdog_log(("report",), 1) or [None])[-1]
        return {
            **stats,
            "channels": {"web": True, "telegram": bool(settings.telegram_token)},
            "modes": router.filter.rules()["sources"] if router else {},
            "triggers": trig,
            "policies": len(store.policies(enabled_only=True)),
            "router": {"enabled": router is not None, "busy": bool(router and router.busy),
                       "model": settings.router_model},
            "workers": {"live": workers, "max": settings.max_workers},
            "machines": agent.machines.status(),
            "watchdog": {"enabled": watchdog is not None, "interval_s": watchdog.interval_s if watchdog else 0,
                         "report": report},
        }

    # -- the Watchdog ---------------------------------------------------------------------
    @app.get("/api/watchdog")
    def watchdog_state(_d: dict = Depends(device)) -> dict:
        return {"enabled": watchdog is not None, "reports": store.watchdog_log(("report",), 5),
                "chat": store.watchdog_log(("chat_user", "chat_watchdog"), 40),
                "proposals": store.proposals(), "guidance": store.get("worker_guidance")}

    @app.post("/api/watchdog/check")
    async def watchdog_check(_d: dict = Depends(device)) -> dict:
        if watchdog is None:
            raise HTTPException(404, "Watchdog is off.")
        return await watchdog.check()

    @app.post("/api/watchdog/chat")
    async def watchdog_chat(body: ChatIn, _d: dict = Depends(device)) -> dict:
        if watchdog is None:
            raise HTTPException(404, "Watchdog is off.")
        return await watchdog.chat(body.text)

    @app.post("/api/watchdog/proposals/{proposal_id}")
    def decide_proposal(proposal_id: int, body: ProposalIn, _d: dict = Depends(device)) -> dict:
        if watchdog is None:
            raise HTTPException(404, "Watchdog is off.")
        try:
            return watchdog.apply(proposal_id, body.approve)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    # -- settings, devices, memory ---------------------------------------------------
    @app.put("/api/settings")
    def put_settings(body: SettingsIn, _d: dict = Depends(device)) -> dict:
        saved = store.get("settings", {})
        if body.policy is not None:
            if body.policy not in POLICIES:
                raise HTTPException(400, f"policy must be one of {', '.join(POLICIES)}")
            settings.policy = saved["policy"] = body.policy
        if body.effort is not None:
            if body.effort not in {"low", "medium", "high", "xhigh", "max"}:
                raise HTTPException(400, "effort must be low|medium|high|xhigh|max")
            settings.effort = saved["effort"] = body.effort
        store.put("settings", saved)
        return state()["settings"]

    @app.get("/api/devices")
    def devices(d: dict = Depends(device)) -> list[dict]:
        return [{**x, "current": x["id"] == d["id"]} for x in list_devices(store)]

    @app.delete("/api/devices/{device_id}")
    def delete_device(device_id: int, _d: dict = Depends(device)) -> dict:
        revoke_device(store, device_id)
        return {"ok": True}

    @app.post("/api/devices/pairing-code")
    def pairing_code(_d: dict = Depends(device)) -> dict:
        """Let a paired phone invite another device."""
        return {"code": new_pairing_code(store), "expires_in": 600}

    @app.get("/api/memories")
    def memories(_d: dict = Depends(device)) -> list[dict]:
        return store.memories()

    @app.delete("/api/memories/{memory_id}")
    def forget(memory_id: int, _d: dict = Depends(device)) -> dict:
        store.forget(memory_id)
        return {"ok": True}

    # -- live updates ----------------------------------------------------------------
    @app.websocket("/api/ws")
    async def ws(sock: WebSocket) -> None:
        if device_for_token(store, sock.query_params.get("token")) is None:
            await sock.close(code=4401)
            return
        await sock.accept()
        q = agent.bus.subscribe()
        viewer: dict[str, Any] = {"on": False, "interval": 1.0, "machine": None}

        async def frames() -> None:
            while True:
                if viewer["on"]:
                    try:
                        h = agent.machines.host(viewer["machine"])
                        if h is None:
                            raise RuntimeError(f"{viewer['machine'] or 'the machine'} is offline")
                        s = await h.screenshot()
                        await sock.send_json({"type": "frame", "w": s.width, "h": s.height, "media_type": s.media_type,
                                              "data": base64.b64encode(s.data).decode(),
                                              "busy": agent.screen_busy(viewer["machine"]), "machine": viewer["machine"],
                                              "ts": time.time()})
                    except Exception as exc:  # permissions missing, display asleep
                        await sock.send_json({"type": "frame_error", "error": str(exc)})
                        await asyncio.sleep(3)
                await asyncio.sleep(viewer["interval"])

        async def reader() -> None:
            while True:
                msg = await sock.receive_json()
                if msg.get("type") == "screen":
                    viewer["on"] = bool(msg.get("on"))
                    viewer["interval"] = max(0.4, min(5.0, float(msg.get("interval", 1.0))))
                    viewer["machine"] = msg.get("machine") or None

        jobs = [asyncio.create_task(frames()), asyncio.create_task(reader())]
        try:
            await sock.send_json({"type": "hello", "state": state()})
            while True:
                getter = asyncio.create_task(q.get())
                done, _ = await asyncio.wait({getter, *jobs}, return_when=asyncio.FIRST_COMPLETED)
                if getter not in done:
                    getter.cancel()
                    break  # reader/frames ended: the socket closed
                await sock.send_json(getter.result())
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            agent.bus.unsubscribe(q)
            for j in jobs:
                j.cancel()

    # -- the phone app -----------------------------------------------------------------
    @app.get("/")
    @app.get("/pair")
    def index() -> FileResponse:
        return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=WEB), name="static")

    @app.get("/sw.js")
    def sw() -> FileResponse:
        return FileResponse(WEB / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})

    @app.get("/manifest.webmanifest")
    def manifest() -> FileResponse:
        return FileResponse(WEB / "manifest.webmanifest", media_type="application/manifest+json")

    return app
