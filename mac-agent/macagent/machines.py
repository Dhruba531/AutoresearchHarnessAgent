"""Compute, state and environments, separated (design note §10, "bring your own machine").

The agent loop and the session database live in the Relay server. Each
machine whose environment a task needs — the Mac mini itself, a MacBook, a
build box — runs a small daemon that keeps no session state and does one
thing: execute host operations (screenshots, clicks, keys, shell, AppleScript)
and return the results.

* The daemon connects OUT to the server over a WebSocket, so the machine needs
  no open port and can sit behind NAT or sleep.
* A task targets a machine. If that machine is offline, only that task waits
  ("needs macbook: offline, waiting") and resumes when it reconnects; other
  tasks keep running.
* The server's own Mac, when there is one, is just the machine called
  "this-mac".

Security (the note's open problem): a daemon executes what the loop sends.
Pair only machines you own, keep the server private (Tailscale), and revoke a
machine from Settings to cut it off.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import secrets
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from .host import Host, Proc, Shot
from .store import Store

log = logging.getLogger("relay.machines")
LOCAL = "this-mac"
RPC_TIMEOUT = 120


class MachineOffline(RuntimeError):
    pass


def _h(v: str) -> str:
    return hashlib.sha256(v.encode()).hexdigest()


class RemoteHost(Host):
    """A Host whose every operation runs on a daemon."""

    name = "remote"

    def __init__(self, machine: str, send: Callable[[dict], Awaitable[None]], info: dict | None = None):
        self.machine = machine
        self._send = send
        self._info = info or {}
        self._pending: dict[int, asyncio.Future] = {}
        self._n = 0
        self.closed = False

    async def call(self, method: str, _wait: float = RPC_TIMEOUT, **args: Any) -> Any:
        if self.closed:
            raise MachineOffline(f"{self.machine} is offline")
        self._n += 1
        rid = self._n
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self._send({"id": rid, "method": method, "args": args})
            return await asyncio.wait_for(fut, _wait)
        except asyncio.TimeoutError as exc:
            raise MachineOffline(f"{self.machine} did not answer {method} within {_wait:.0f}s") from exc
        finally:
            self._pending.pop(rid, None)

    def resolve(self, msg: dict) -> None:
        fut = self._pending.get(msg.get("id"))
        if fut is None or fut.done():
            return
        if msg.get("ok"):
            fut.set_result(msg.get("result"))
        else:
            fut.set_exception(RemoteError(msg.get("error") or "remote error"))

    def close(self) -> None:
        self.closed = True
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(MachineOffline(f"{self.machine} disconnected"))

    @staticmethod
    def _shot(r: dict) -> Shot:
        return Shot(base64.b64decode(r["data"]), r["media_type"], r["width"], r["height"])

    @staticmethod
    def _proc(r: dict) -> Proc:
        return Proc(r.get("code"), r.get("out", ""), r.get("err", ""), r.get("timed_out", False))

    async def run(self, argv, *, timeout=60, stdin=None, cwd=None) -> Proc:
        return self._proc(await self.call("run", timeout + 30, argv=argv, timeout=timeout, stdin=stdin, cwd=cwd))

    async def shell(self, command, timeout=120) -> Proc:
        return self._proc(await self.call("shell", timeout + 30, command=command, timeout=timeout))

    async def osascript(self, script, args=None, *, language="AppleScript", timeout=90) -> Proc:
        return self._proc(await self.call("osascript", timeout + 30, script=script, args=args or [], language=language,
                                          timeout=timeout))

    async def screenshot(self) -> Shot:
        return self._shot(await self.call("screenshot"))

    async def zoom(self, region) -> Shot:
        return self._shot(await self.call("zoom", region=list(region)))

    async def click(self, x, y, button="left", count=1, modifiers="") -> None:
        await self.call("click", x=x, y=y, button=button, count=count, modifiers=modifiers)

    async def move(self, x, y) -> None:
        await self.call("move", x=x, y=y)

    async def mouse_button(self, down: bool) -> None:
        await self.call("mouse_button", down=down)

    async def drag(self, start, end, modifiers="") -> None:
        await self.call("drag", start=list(start), end=list(end), modifiers=modifiers)

    async def scroll(self, x, y, direction, amount, modifiers="") -> None:
        await self.call("scroll", x=x, y=y, direction=direction, amount=amount, modifiers=modifiers)

    async def type_text(self, text: str) -> None:
        await self.call("type_text", text=text)

    async def key(self, combo: str, repeat: int = 1) -> None:
        await self.call("key", combo=combo, repeat=repeat)

    async def hold_key(self, combo: str, duration: float) -> None:
        await self.call("hold_key", duration + 30, combo=combo, duration=duration)

    async def cursor_position(self) -> tuple[int, int]:
        r = await self.call("cursor_position")
        return int(r[0]), int(r[1])

    def info(self) -> dict:
        return {**self._info, "host": f"remote:{self.machine}"}


class RemoteError(RuntimeError):
    pass


async def serve_request(host: Host, msg: dict) -> dict:
    """Daemon side: execute one request on the local host."""
    m, a = msg.get("method"), msg.get("args") or {}
    try:
        if m in {"screenshot", "zoom"}:
            s = await (host.screenshot() if m == "screenshot" else host.zoom(a["region"]))
            res: Any = {"data": base64.b64encode(s.data).decode(), "media_type": s.media_type, "width": s.width,
                        "height": s.height}
        elif m in {"run", "shell", "osascript"}:
            if m == "run":
                p = await host.run(a["argv"], timeout=a.get("timeout", 60), stdin=a.get("stdin"), cwd=a.get("cwd"))
            elif m == "shell":
                p = await host.shell(a["command"], timeout=a.get("timeout", 120))
            else:
                p = await host.osascript(a["script"], a.get("args"), language=a.get("language", "AppleScript"),
                                         timeout=a.get("timeout", 90))
            res = {"code": p.code, "out": p.out, "err": p.err, "timed_out": p.timed_out}
        elif m == "cursor_position":
            res = list(await host.cursor_position())
        elif m == "info":
            res = host.info()
        elif m in {"click", "move", "mouse_button", "drag", "scroll", "type_text", "key", "hold_key"}:
            await getattr(host, m)(**a)
            res = "OK"
        else:
            raise ValueError(f"unknown method {m}")
        return {"id": msg.get("id"), "ok": True, "result": res}
    except Exception as exc:  # report every failure to the server; the daemon keeps running
        return {"id": msg.get("id"), "ok": False, "error": f"{type(exc).__name__}: {exc}"}


class MachineRegistry:
    def __init__(self, store: Store, local: Host | None = None, local_name: str = LOCAL):
        self.store = store
        self.hosts: dict[str, Host] = {}
        self.local_name = local_name if local is not None else None
        if local is not None:
            self.hosts[local_name] = local
        self._online: dict[str, asyncio.Event] = {}
        self.listeners: list[Callable[[str, bool], None]] = []

    # -- pairing --------------------------------------------------------------
    def register(self, name: str) -> str:
        """Create (or re-key) a machine and return its daemon token."""
        name = name.strip()[:60] or "machine"
        if name == self.local_name:
            raise ValueError(f"'{name}' is the server's own machine name")
        token = "rlm_" + secrets.token_urlsafe(32)
        if self.store.one("SELECT id FROM machines WHERE name=?", (name,)):
            self.store.x("UPDATE machines SET token_hash=?, revoked=0 WHERE name=?", (_h(token), name))
        else:
            self.store.x("INSERT INTO machines(name,token_hash,created_at) VALUES(?,?,?)", (name, _h(token), time.time()))
        return token

    def machine_for_token(self, token: str | None) -> str | None:
        if not token or not token.startswith("rlm_"):
            return None
        row = self.store.one("SELECT name, revoked FROM machines WHERE token_hash=?", (_h(token),))
        return row["name"] if row and not row["revoked"] else None

    def revoke(self, name: str) -> None:
        self.store.x("UPDATE machines SET revoked=1 WHERE name=?", (name,))
        h = self.hosts.get(name)
        if isinstance(h, RemoteHost):
            h.close()
            self.detach(name)

    # -- connections ----------------------------------------------------------
    def _ev(self, name: str) -> asyncio.Event:
        if name not in self._online:
            self._online[name] = asyncio.Event()
            if name in self.hosts:
                self._online[name].set()
        return self._online[name]

    def attach(self, name: str, send: Callable[[dict], Awaitable[None]], info: dict) -> RemoteHost:
        old = self.hosts.get(name)
        if isinstance(old, RemoteHost):
            old.close()
        host = RemoteHost(name, send, info)
        self.hosts[name] = host
        self.store.x("UPDATE machines SET last_seen=?, info=? WHERE name=?", (time.time(), json.dumps(info, default=str), name))
        self._ev(name).set()
        for cb in self.listeners:
            cb(name, True)
        return host

    def detach(self, name: str, host: Host | None = None) -> None:
        if host is not None and self.hosts.get(name) is not host:
            return  # a newer connection already replaced this one
        h = self.hosts.pop(name, None)
        if isinstance(h, RemoteHost):
            h.close()
        self.store.x("UPDATE machines SET last_seen=? WHERE name=?", (time.time(), name))
        self._ev(name).clear()
        for cb in self.listeners:
            cb(name, False)

    # -- lookup -----------------------------------------------------------------
    @property
    def default(self) -> str | None:
        if self.local_name:
            return self.local_name
        online = [n for n in self.hosts]
        return online[0] if online else (self.names()[0] if self.names() else None)

    def names(self) -> list[str]:
        remote = [r["name"] for r in self.store.all("SELECT name FROM machines WHERE revoked=0 ORDER BY id")]
        return ([self.local_name] if self.local_name else []) + remote

    def host(self, name: str | None) -> Host | None:
        return self.hosts.get(name or self.default or "")

    def online(self, name: str | None) -> bool:
        return (name or self.default) in self.hosts

    async def wait_online(self, name: str) -> None:
        await self._ev(name).wait()

    def status(self) -> list[dict]:
        rows = {r["name"]: r for r in self.store.all("SELECT name,last_seen,info FROM machines WHERE revoked=0")}
        out = []
        for n in self.names():
            r = rows.get(n, {})
            out.append({"name": n, "online": n in self.hosts, "kind": "local" if n == self.local_name else "daemon",
                        "last_seen": r.get("last_seen"), "info": json.loads(r["info"]) if r.get("info") else
                        (self.hosts[n].info() if n in self.hosts else {})})
        return out


# -- the daemon -------------------------------------------------------------------

DAEMON_STATE = Path("~/.relay-daemon.json").expanduser()


async def run_daemon(server: str, name: str, code: str | None, fake: bool = False, max_edge: int = 1440) -> None:
    """`python -m macagent daemon`: connect to a Relay server and serve this machine."""
    import httpx
    import websockets

    from .host import make_host

    state = json.loads(DAEMON_STATE.read_text()) if DAEMON_STATE.exists() else {}
    server = server.rstrip("/")
    if code or not state.get("token") or state.get("server") != server:
        if not code:
            raise SystemExit("First run: pass --code from `python -m macagent pair` on the server.")
        r = httpx.post(f"{server}/api/machines/register", json={"code": code, "name": name}, timeout=30)
        if r.status_code != 200:
            raise SystemExit(f"Registration failed: {r.status_code} {r.text}")
        state = {"server": server, "name": name, "token": r.json()["token"]}
        DAEMON_STATE.write_text(json.dumps(state))
        DAEMON_STATE.chmod(0o600)
    host = make_host(fake, max_edge)
    ws_url = server.replace("http", "ws", 1) + f"/api/machines/ws?token={state['token']}"
    backoff = 1
    while True:
        try:
            async with websockets.connect(ws_url, max_size=32 * 1024 * 1024, ping_interval=20) as ws:
                await ws.send(json.dumps({"type": "hello", "info": host.info()}))
                print(f"connected to {server} as {state['name']}; this daemon keeps no session state")
                backoff = 1
                async for raw in ws:
                    msg = json.loads(raw)
                    asyncio.create_task(_answer(ws, host, msg))
        except (OSError, websockets.WebSocketException) as exc:
            print(f"disconnected ({exc}); retrying in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(60, backoff * 2)


async def _answer(ws, host: Host, msg: dict) -> None:
    await ws.send(json.dumps(await serve_request(host, msg)))
