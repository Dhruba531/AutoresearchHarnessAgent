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


class SettingsIn(BaseModel):
    policy: str | None = None
    effort: str | None = None


def token_of(request: Request) -> str | None:
    h = request.headers.get("authorization", "")
    return h[7:].strip() if h.lower().startswith("bearer ") else request.query_params.get("token")


def create_app(settings: Settings, store: Store, agent: Agent, extra_startup=None) -> FastAPI:
    limiter = RateLimiter(limit=8, window_s=60)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        saved = store.get("settings", {})
        if saved.get("policy") in POLICIES:
            settings.policy = saved["policy"]
        if saved.get("effort"):
            settings.effort = saved["effort"]
        agent.start()
        tasks = [asyncio.create_task(c) for c in (extra_startup() if extra_startup else [])]
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
            "host": agent.host.info(),
            "current": agent.current,
            "screen_busy": agent.screen_busy(),
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
            if agent.current != task_id:
                raise HTTPException(409, "Only the running task can be paused.")
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
    @app.get("/api/screen")
    async def screen(_d: dict = Depends(device)) -> Response:
        s = await agent.host.screenshot()
        return Response(s.data, media_type=s.media_type, headers={"Cache-Control": "no-store",
                                                                   "X-Width": str(s.width), "X-Height": str(s.height)})

    @app.post("/api/control")
    async def control(body: ControlIn, d: dict = Depends(device)) -> dict:
        if agent.screen_busy():
            if not body.force:
                raise HTTPException(409, "A task is using the screen. Take over (pauses it) to control manually.")
            agent.pause(agent.current)
        h = agent.host
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
        viewer: dict[str, Any] = {"on": False, "interval": 1.0}

        async def frames() -> None:
            while True:
                if viewer["on"]:
                    try:
                        s = await agent.host.screenshot()
                        await sock.send_json({"type": "frame", "w": s.width, "h": s.height, "media_type": s.media_type,
                                              "data": base64.b64encode(s.data).decode(), "busy": agent.screen_busy(),
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
