"""Tools the agent can call, beyond the screen.

Native tools are faster and more reliable than driving the UI, so the system
prompt steers the model to them first and to the screen when nothing fits.

Each tool has a JSON schema for the model, a classifier (tier + one-line
summary for the approval card on the phone), and an executor. Executors
return text, or a list of content blocks (images for the screen).
"""

from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import scripts
from .config import Settings
from .host import Host, Shot
from .policy import classify_script, classify_shell, is_relay_home, path_tier, resolve
from .store import Store

COMPUTER = "computer"
SCREEN_READ = {"screenshot", "zoom", "cursor_position", "wait", "mouse_move"}
SCREEN_MEMBERS = SCREEN_READ | {
    "left_click", "right_click", "middle_click", "double_click", "triple_click", "left_click_drag",
    "left_mouse_down", "left_mouse_up", "scroll", "type", "key", "hold_key",
}


class ToolError(Exception):
    """A failure to report to the model as an error result."""


class Denied(ToolError):
    pass


@dataclass
class Result:
    content: str | list[dict]
    is_error: bool = False
    shot: Shot | None = None  # kept for the phone timeline


def _s(desc: str, **props: Any) -> dict:
    required = [k for k, v in props.items() if not v.pop("optional", False)]
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


def _str(desc: str, optional: bool = False) -> dict:
    return {"type": "string", "description": desc, **({"optional": True} if optional else {})}


def _int(desc: str, optional: bool = False) -> dict:
    return {"type": "integer", "description": desc, **({"optional": True} if optional else {})}


SPECS: list[dict] = [
    {"name": "run_shell", "description": "Run a zsh command on the Mac (login shell, home directory as cwd). Prefer "
     "read-only commands; anything that changes the system needs the user's approval.",
     "input_schema": _s("", command=_str("the command"), timeout_s=_int("max seconds, default 120", True))},
    {"name": "applescript", "description": "Run AppleScript (or JavaScript for Automation) to control a Mac app that "
     "has no dedicated tool. Return values come back as text.",
     "input_schema": _s("", script=_str("the script source"),
                        language={"type": "string", "enum": ["AppleScript", "JavaScript"], "optional": True})},
    {"name": "open", "description": "Open an app by name (e.g. 'Safari'), a URL, or a file/folder path.",
     "input_schema": _s("", target=_str("app name, URL, or path"))},
    {"name": "list_dir", "description": "List a folder (names, sizes, modified times).",
     "input_schema": _s("", path=_str("folder path; ~ allowed"), limit=_int("max entries, default 200", True))},
    {"name": "read_file", "description": "Read a text file (first max_chars characters).",
     "input_schema": _s("", path=_str("file path"), max_chars=_int("default 20000", True))},
    {"name": "write_file", "description": "Create or overwrite a text file, or append to it.",
     "input_schema": _s("", path=_str("file path"), content=_str("text to write"),
                        append={"type": "boolean", "description": "append instead of overwrite", "optional": True})},
    {"name": "move_path", "description": "Move or rename a file or folder.",
     "input_schema": _s("", source=_str("existing path"), destination=_str("new path"))},
    {"name": "trash_path", "description": "Move a file or folder to the Trash (recoverable).",
     "input_schema": _s("", path=_str("path to trash"))},
    {"name": "spotlight", "description": "Search the Mac with Spotlight (mdfind). Supports raw queries like "
     "kMDItemContentType == 'com.adobe.pdf'.",
     "input_schema": _s("", query=_str("search text or Spotlight query"), folder=_str("limit to folder", True),
                        limit=_int("default 30", True))},
    {"name": "calendar_events", "description": "List Calendar events starting in a window.",
     "input_schema": _s("", days=_int("days ahead to look, default 1"), start=_str("ISO start, default now", True),
                        calendar=_str("only this calendar", True))},
    {"name": "calendar_create", "description": "Create a Calendar event. Times are ISO 8601 with timezone offset.",
     "input_schema": _s("", title=_str("event title"), start=_str("ISO start"), end=_str("ISO end"),
                        calendar=_str("calendar name", True), location=_str("location", True), notes=_str("notes", True))},
    {"name": "reminders_list", "description": "List incomplete Reminders.",
     "input_schema": _s("", list=_str("only this list", True))},
    {"name": "reminder_create", "description": "Create a Reminder, optionally with an ISO due date.",
     "input_schema": _s("", title=_str("reminder text"), due=_str("ISO due date-time", True),
                        list=_str("list name", True), notes=_str("notes", True))},
    {"name": "notes_search", "description": "Search Apple Notes by title or text.",
     "input_schema": _s("", query=_str("text to find"))},
    {"name": "note_create", "description": "Create an Apple Note.",
     "input_schema": _s("", title=_str("title"), body=_str("body text"))},
    {"name": "mail_recent", "description": "List recent Inbox messages in Apple Mail, optionally filtered.",
     "input_schema": _s("", query=_str("filter by subject or sender", True), limit=_int("default 15", True))},
    {"name": "mail_read", "description": "Read one Mail message by the id from mail_recent.",
     "input_schema": _s("", id=_int("message id"))},
    {"name": "mail_draft", "description": "Create a Mail draft (opens it, does not send).",
     "input_schema": _s("", to=_str("recipient address"), subject=_str("subject"), body=_str("plain-text body"))},
    {"name": "mail_send", "description": "Send an email via Apple Mail. Always requires the user's approval.",
     "input_schema": _s("", to=_str("recipient address"), subject=_str("subject"), body=_str("plain-text body"))},
    {"name": "messages_send", "description": "Send an iMessage. Always requires the user's approval.",
     "input_schema": _s("", to=_str("phone number, email, or contact handle"), text=_str("message"))},
    {"name": "clipboard_get", "description": "Read the clipboard text.", "input_schema": _s("")},
    {"name": "clipboard_set", "description": "Put text on the clipboard.", "input_schema": _s("", text=_str("text"))},
    {"name": "system_status", "description": "Front app, running apps, battery/power, disk, volume, uptime.",
     "input_schema": _s("")},
    {"name": "set_volume", "description": "Set output volume 0-100.", "input_schema": _s("", level=_int("0-100"))},
    {"name": "notify", "description": "Show a notification on the Mac's screen.",
     "input_schema": _s("", title=_str("title"), message=_str("message"))},
    {"name": "remember", "description": "Save a lasting fact about the user or their setup for future tasks "
     "(preferences, names, recurring details). Never store passwords or secrets.",
     "input_schema": _s("", fact=_str("the fact, one sentence"))},
    {"name": "ask_user", "description": "Ask the user a question on their phone and wait for the answer. Use when "
     "the task is ambiguous or you need information only they have.",
     "input_schema": _s("", question=_str("the question"))},
    {"name": "request_approval", "description": "Ask the user to approve an irreversible or consequential action you "
     "are about to take on screen (sending, buying, deleting, submitting a form, accepting terms, changing settings, "
     "signing in). Call it BEFORE the click that commits the action.",
     "input_schema": _s("", action=_str("what you will do, specifically"))},
]

NAMES = {s["name"] for s in SPECS}


def _short(v: Any, n: int = 120) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def classify(name: str, inp: dict, toolset: str | None, settings: Settings) -> tuple[str, str]:
    """(tier, human summary) for one call."""
    if toolset == COMPUTER:
        if name in SCREEN_READ:
            return "read", f"screen: {name}"
        where = f" at {inp.get('coordinate')}" if inp.get("coordinate") else ""
        what = f" “{_short(inp.get('text', ''), 60)}”" if name in {"type", "key", "hold_key"} else ""
        return "act", f"screen: {name}{where}{what}"
    if name == "run_shell":
        return classify_shell(inp.get("command", "")), f"Run in Terminal: {_short(inp.get('command', ''), 200)}"
    if name == "applescript":
        return classify_script(inp.get("script", "")), f"Run AppleScript: {_short(inp.get('script', ''), 200)}"
    if name in {"list_dir", "read_file"}:
        p = resolve(inp.get("path", "~"))
        return path_tier(p, "read"), f"{'Read' if name == 'read_file' else 'List'} {p}"
    if name == "write_file":
        p = resolve(inp.get("path", ""))
        return path_tier(p, "change"), f"{'Append to' if inp.get('append') else 'Write'} {p} ({len(inp.get('content', ''))} chars)"
    if name == "move_path":
        a, b = resolve(inp.get("source", "")), resolve(inp.get("destination", ""))
        return max(path_tier(a, "change"), path_tier(b, "change"), key=("change", "danger").index), f"Move {a} → {b}"
    if name == "trash_path":
        p = resolve(inp.get("path", ""))
        return path_tier(p, "change"), f"Move to Trash: {p}"
    if name == "calendar_create":
        return "change", f"Add calendar event “{_short(inp.get('title'), 60)}” {inp.get('start')} – {inp.get('end')}"
    if name == "reminder_create":
        return "change", f"Add reminder “{_short(inp.get('title'), 80)}”" + (f" due {inp['due']}" if inp.get("due") else "")
    if name == "note_create":
        return "change", f"Create note “{_short(inp.get('title'), 80)}”"
    if name == "mail_draft":
        return "change", f"Draft email to {inp.get('to')}: “{_short(inp.get('subject'), 60)}”"
    if name == "mail_send":
        return "send", f"Send email to {inp.get('to')}: “{_short(inp.get('subject'), 60)}” — {_short(inp.get('body'), 140)}"
    if name == "messages_send":
        return "send", f"Send iMessage to {inp.get('to')}: “{_short(inp.get('text'), 160)}”"
    if name == "open":
        return "act", f"Open {inp.get('target')}"
    if name in {"clipboard_set", "set_volume", "notify", "remember"}:
        return "act", f"{name}: {_short(inp, 100)}"
    if name in {"ask_user", "request_approval"}:
        return "read", _short(inp.get("question") or inp.get("action"), 200)
    return "read", f"{name}: {_short(inp, 100)}"


def image_block(shot: Shot) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": shot.media_type,
                                         "data": base64.b64encode(shot.data).decode()}}


class Toolbox:
    def __init__(self, host: Host, store: Store, settings: Settings):
        self.host, self.store, self.settings = host, store, settings

    def specs(self) -> list[dict]:
        return SPECS

    def _guard(self, *paths: Path) -> None:
        for p in paths:
            if is_relay_home(p, self.settings.home):
                raise Denied(f"{p} is Relay's own data directory; it is off limits to tools.")

    async def _js(self, script: str, args: dict, timeout: float = 90) -> Any:
        proc = await self.host.osascript(script, [json.dumps(args)], language=scripts.JXA, timeout=timeout)
        if proc.code != 0:
            raise ToolError(_automation_hint(proc.err))
        try:
            return json.loads(proc.out)
        except json.JSONDecodeError:
            return proc.out.strip()

    async def _as(self, script: str, args: list[str], timeout: float = 90) -> str:
        proc = await self.host.osascript(script, args, language=scripts.AS, timeout=timeout)
        if proc.code != 0:
            raise ToolError(_automation_hint(proc.err))
        return proc.out.strip()

    # -- computer toolset -------------------------------------------------------
    async def computer(self, name: str, inp: dict) -> Result:
        h = self.host
        xy = inp.get("coordinate")
        x, y = (xy[0], xy[1]) if isinstance(xy, (list, tuple)) and len(xy) == 2 else (None, None)
        mods = inp.get("text", "") if name.endswith("click") or name in {"scroll", "left_click_drag"} else ""
        if name == "screenshot":
            shot = await h.screenshot()
            return Result([image_block(shot)], shot=shot)
        if name == "zoom":
            shot = await h.zoom(list(inp["region"]))
            return Result([image_block(shot)], shot=shot)
        if name == "cursor_position":
            cx, cy = await h.cursor_position()
            return Result(f"X={cx}, Y={cy}")
        clicks = {"left_click": ("left", 1), "right_click": ("right", 1), "middle_click": ("middle", 1),
                  "double_click": ("left", 2), "triple_click": ("left", 3)}
        if name in clicks:
            button, count = clicks[name]
            await h.click(x, y, button, count, mods)
        elif name == "left_click_drag":
            await h.drag(list(inp["start_coordinate"]), list(inp["coordinate"]), mods)
        elif name == "mouse_move":
            await h.move(x, y)
        elif name in {"left_mouse_down", "left_mouse_up"}:
            await h.mouse_button(name == "left_mouse_down")
        elif name == "scroll":
            await h.scroll(x, y, inp.get("scroll_direction", "down"), int(inp.get("scroll_amount", 3)), mods)
        elif name == "type":
            await h.type_text(inp.get("text", ""))
        elif name == "key":
            await h.key(inp["text"], int(inp.get("repeat", 1)))
        elif name == "hold_key":
            await h.hold_key(inp["text"], float(inp.get("duration", 1)))
        elif name == "wait":
            import asyncio

            await asyncio.sleep(min(300.0, float(inp.get("duration", 1))))
        else:
            raise ToolError(f"unsupported computer action '{name}'")
        return Result("OK")

    # -- native tools -------------------------------------------------------------
    async def run(self, name: str, inp: dict) -> Result:
        fn: Callable[[dict], Awaitable[Any]] | None = getattr(self, f"t_{name}", None)
        if fn is None:
            raise ToolError(f"unknown tool '{name}'")
        out = await fn(inp)
        return out if isinstance(out, Result) else Result(out if isinstance(out, str) else json.dumps(out, ensure_ascii=False, default=str))

    async def t_run_shell(self, inp: dict) -> str:
        home = str(self.settings.home.resolve())
        tilde = "~/" + os.path.relpath(home, os.path.expanduser("~")) if home.startswith(os.path.expanduser("~")) else home
        if home in inp["command"] or tilde in inp["command"]:
            raise Denied("Commands touching Relay's data directory are refused.")
        proc = await self.host.shell(inp["command"], timeout=min(int(inp.get("timeout_s") or self.settings.shell_timeout_s), 900))
        parts = [f"exit code: {proc.code}" + (" (timed out)" if proc.timed_out else "")]
        if proc.out:
            parts.append(f"stdout:\n{proc.out}")
        if proc.err:
            parts.append(f"stderr:\n{proc.err}")
        return "\n".join(parts)

    async def t_applescript(self, inp: dict) -> str:
        proc = await self.host.osascript(inp["script"], language=inp.get("language") or "AppleScript")
        if proc.code != 0:
            raise ToolError(_automation_hint(proc.err))
        return proc.out.strip() or "(no result)"

    async def t_open(self, inp: dict) -> str:
        target = inp["target"].strip()
        if target.startswith(("http://", "https://", "mailto:", "/", "~", "file:")):
            argv = ["open", os.path.expanduser(target)]
        else:
            argv = ["open", "-a", target]
        if self.host.name == "fake":
            return f"opened {target}"
        proc = await self.host.run(argv, timeout=30)
        if proc.code != 0:
            raise ToolError(proc.err.strip() or f"could not open {target}")
        return f"opened {target}"

    async def t_list_dir(self, inp: dict) -> list[dict]:
        p = resolve(inp.get("path") or "~")
        self._guard(p)
        if not p.is_dir():
            raise ToolError(f"not a folder: {p}")
        out = []
        for child in sorted(p.iterdir(), key=lambda c: c.name.lower())[: int(inp.get("limit") or 200)]:
            try:
                st = child.stat()
            except OSError:
                continue
            out.append({"name": child.name + ("/" if child.is_dir() else ""), "size": st.st_size,
                        "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))})
        return out

    async def t_read_file(self, inp: dict) -> str:
        p = resolve(inp["path"])
        self._guard(p)
        if not p.is_file():
            raise ToolError(f"no such file: {p}")
        data = p.read_bytes()[: int(inp.get("max_chars") or 20000) * 4]
        if b"\x00" in data[:4096]:
            raise ToolError(f"{p} looks binary; open it with an app instead")
        return data.decode("utf-8", errors="replace")[: int(inp.get("max_chars") or 20000)]

    async def t_write_file(self, inp: dict) -> str:
        p = resolve(inp["path"])
        self._guard(p)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a" if inp.get("append") else "w", encoding="utf-8") as f:
            f.write(inp["content"])
        return f"wrote {len(inp['content'])} chars to {p}"

    async def t_move_path(self, inp: dict) -> str:
        a, b = resolve(inp["source"]), resolve(inp["destination"])
        self._guard(a, b)
        if not a.exists():
            raise ToolError(f"no such path: {a}")
        if b.exists() and b.is_dir():
            b = b / a.name
        if b.exists():
            raise ToolError(f"destination exists: {b}")
        import shutil

        shutil.move(str(a), str(b))
        return f"moved {a} → {b}"

    async def t_trash_path(self, inp: dict) -> str:
        p = resolve(inp["path"])
        self._guard(p)
        if not p.exists():
            raise ToolError(f"no such path: {p}")
        return await self._as(scripts.TRASH, [str(p)])

    async def t_spotlight(self, inp: dict) -> str:
        argv = ["mdfind"]
        if inp.get("folder"):
            argv += ["-onlyin", str(resolve(inp["folder"]))]
        argv.append(inp["query"])
        proc = await self.host.run(argv, timeout=30)
        lines = [ln for ln in proc.out.splitlines() if ln and not is_relay_home(Path(ln), self.settings.home)]
        limit = int(inp.get("limit") or 30)
        return "\n".join(lines[:limit]) + (f"\n… {len(lines) - limit} more" if len(lines) > limit else "") or "no matches"

    async def t_calendar_events(self, inp: dict) -> Any:
        return await self._js(scripts.CALENDAR_EVENTS, {"days": inp.get("days") or 1, "from": inp.get("start"),
                                                        "calendar": inp.get("calendar")}, timeout=120)

    async def t_calendar_create(self, inp: dict) -> Any:
        return await self._js(scripts.CALENDAR_CREATE, inp)

    async def t_reminders_list(self, inp: dict) -> Any:
        return await self._js(scripts.REMINDERS_LIST, {"list": inp.get("list")})

    async def t_reminder_create(self, inp: dict) -> Any:
        return await self._js(scripts.REMINDER_CREATE, inp)

    async def t_notes_search(self, inp: dict) -> Any:
        return await self._js(scripts.NOTES_SEARCH, {"query": inp["query"]})

    async def t_note_create(self, inp: dict) -> str:
        return await self._as(scripts.NOTE_CREATE, [inp["title"], inp["body"].replace("\n", "<br>")])

    async def t_mail_recent(self, inp: dict) -> Any:
        return await self._js(scripts.MAIL_RECENT, {"query": inp.get("query"), "limit": inp.get("limit") or 15}, timeout=120)

    async def t_mail_read(self, inp: dict) -> Any:
        return await self._js(scripts.MAIL_READ, {"id": int(inp["id"])})

    async def t_mail_draft(self, inp: dict) -> str:
        return await self._as(scripts.MAIL_COMPOSE, [inp["to"], inp["subject"], inp["body"], "0"])

    async def t_mail_send(self, inp: dict) -> str:
        return await self._as(scripts.MAIL_COMPOSE, [inp["to"], inp["subject"], inp["body"], "1"])

    async def t_messages_send(self, inp: dict) -> str:
        return await self._as(scripts.MESSAGES_SEND, [inp["to"], inp["text"]])

    async def t_clipboard_get(self, inp: dict) -> str:
        return (await self.host.run(["pbpaste"], timeout=10)).out or "(clipboard empty)"

    async def t_clipboard_set(self, inp: dict) -> str:
        await self.host.run(["pbcopy"], stdin=inp["text"], timeout=10)
        return "copied"

    async def t_system_status(self, inp: dict) -> dict:
        out: dict[str, Any] = {"host": self.host.info()}
        front = await self.host.osascript(scripts.FRONTMOST, timeout=15)
        if front.code == 0:
            out["frontmost"] = front.out.strip()
        apps = await self.host.osascript(scripts.RUNNING_APPS, timeout=15)
        if apps.code == 0:
            out["running_apps"] = apps.out.strip()
        vol = await self.host.osascript(scripts.VOLUME_GET, timeout=15)
        if vol.code == 0:
            out["volume"] = vol.out.strip()
        for key, argv in {"power": ["pmset", "-g", "batt"], "disk": ["df", "-h", "/"], "uptime": ["uptime"]}.items():
            try:
                proc = await self.host.run(argv, timeout=10)
                out[key] = proc.out.strip()
            except FileNotFoundError:
                pass
        return out

    async def t_set_volume(self, inp: dict) -> str:
        return await self._as(scripts.VOLUME_SET, [str(max(0, min(100, int(inp["level"]))))])

    async def t_notify(self, inp: dict) -> str:
        return await self._as(scripts.NOTIFY, [inp["title"], inp["message"]])

    async def t_remember(self, inp: dict) -> str:
        fact = inp["fact"].strip()[:500]
        if any(w in fact.lower() for w in ("password", "passcode", "api key", "secret", "cvv", "pin ")):
            raise Denied("Refusing to store something that looks like a secret.")
        self.store.remember(fact)
        return "remembered"


def _automation_hint(err: str) -> str:
    err = err.strip()
    if "-1743" in err or "Not authorized to send Apple events" in err:
        return (f"{err}\nmacOS blocked automation. Allow it in System Settings → Privacy & Security → Automation "
                "for the app running Relay.")
    if "-1728" in err or "Can’t get" in err or "Can't get" in err:
        return f"{err}\n(The item was not found — check the name, or that the app has that data.)"
    return err or "script failed"
