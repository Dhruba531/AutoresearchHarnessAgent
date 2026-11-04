"""A virtual desktop for development and tests on machines that are not Macs.

It draws a small desktop (menu bar, a "Notes" window with a text area and a
couple of buttons), tracks the cursor, applies clicks and typing to that
state, and records every action. AppleScript/JXA calls return canned data
keyed by the `relay:<name>` marker each script in macagent/scripts.py carries.
Shell commands run for real on the local machine.
"""

from __future__ import annotations

import asyncio
import json
import re

from PIL import Image, ImageDraw

from . import Host, Proc, Shot, encode_jpeg, fit
from .keys import parse_combo, parse_modifiers

W, H = 1280, 800
BUTTONS = {"Save": (900, 640, 1010, 680), "Clear": (1030, 640, 1140, 680)}
TEXT_AREA = (180, 180, 1140, 620)

CANNED = {
    "calendar_events": [{"calendar": "Home", "title": "Dentist", "start": "2026-11-05T09:30:00Z",
                         "end": "2026-11-05T10:30:00Z", "location": "Main St"}],
    "calendar_create": "created",
    "reminders_list": [{"name": "Buy milk", "due": None}],
    "reminder_create": "created",
    "notes_search": [{"name": "Trip ideas", "snippet": "Kyoto in spring"}],
    "note_create": "created",
    "mail_recent": [{"id": 101, "from": "alice@example.com", "subject": "Lunch?", "date": "2026-11-04"}],
    "mail_read": {"from": "alice@example.com", "subject": "Lunch?", "body": "Free at 1pm?"},
    "mail_compose": "draft created",
    "messages_send": "sent",
    "frontmost": {"app": "Notes", "window": "Untitled"},
    "running_apps": ["Finder", "Notes", "Safari"],
    "notify": "ok",
    "volume_get": {"output": 40, "muted": False},
    "volume_set": "ok",
    "trash": "moved to Trash",
}


class FakeHost(Host):
    name = "fake"

    def __init__(self, max_edge: int = 1440):
        self.max_edge = max_edge
        self.cursor = [W // 2, H // 2]
        self.text = ""
        self.focused = False
        self.saved: list[str] = []
        self.pressed = False
        self.actions: list[dict] = []
        self.scripts: list[dict] = []
        self.size = fit(W, H, max_edge)
        self.lock = asyncio.Lock()

    # coordinates arrive in screenshot space; the desktop is drawn at W×H
    def _desk(self, x: float, y: float) -> tuple[int, int]:
        return round(x * W / self.size[0]), round(y * H / self.size[1])

    def _render(self) -> Image.Image:
        img = Image.new("RGB", (W, H), (58, 86, 120))
        d = ImageDraw.Draw(img)
        d.rectangle((0, 0, W, 28), fill=(236, 236, 236))
        d.text((16, 8), "  Relay virtual Mac      File   Edit   View   Window   Help", fill=(20, 20, 20))
        d.rounded_rectangle((150, 110, 1170, 700), 12, fill=(250, 250, 248), outline=(180, 180, 180))
        d.rectangle((150, 110, 1170, 150), fill=(230, 230, 228))
        d.text((630, 124), "Notes", fill=(40, 40, 40))
        d.rectangle(TEXT_AREA, outline=(0, 122, 255) if self.focused else (200, 200, 200), width=2)
        y = TEXT_AREA[1] + 10
        for line in (self.text or ("Click here and type…" if not self.focused else "")).split("\n")[-20:]:
            d.text((TEXT_AREA[0] + 12, y), line[:140], fill=(30, 30, 30) if self.text else (150, 150, 150))
            y += 20
        for label, box in BUTTONS.items():
            d.rounded_rectangle(box, 8, fill=(0, 122, 255) if label == "Save" else (220, 220, 220))
            d.text((box[0] + 34, box[1] + 13), label, fill=(255, 255, 255) if label == "Save" else (30, 30, 30))
        d.text((180, 660), f"saved notes: {len(self.saved)}", fill=(90, 90, 90))
        cx, cy = self.cursor
        d.polygon([(cx, cy), (cx, cy + 18), (cx + 5, cy + 14), (cx + 12, cy + 14)], fill=(0, 0, 0), outline=(255, 255, 255))
        return img

    def _log(self, action: str, **kw) -> None:
        self.actions.append({"action": action, **kw})

    async def screenshot(self) -> Shot:
        img = self._render().resize(self.size, Image.LANCZOS)
        return Shot(encode_jpeg(img), "image/jpeg", *self.size)

    async def zoom(self, region: list[int]) -> Shot:
        x0, y0 = self._desk(region[0], region[1])
        x1, y1 = self._desk(region[2], region[3])
        if x1 <= x0 or y1 <= y0:
            raise ValueError("zoom region is empty")
        crop = self._render().crop((x0, y0, x1, y1))
        w, h = fit(crop.width * 2, crop.height * 2, self.max_edge)
        return Shot(encode_jpeg(crop.resize((w, h))), "image/jpeg", w, h)

    async def move(self, x, y) -> None:
        self.cursor = list(self._desk(x, y))
        self._log("move", x=x, y=y)

    async def click(self, x, y, button="left", count=1, modifiers="") -> None:
        parse_modifiers(modifiers)
        if x is not None and y is not None:
            self.cursor = list(self._desk(x, y))
        cx, cy = self.cursor
        self._log("click", x=x, y=y, button=button, count=count, modifiers=modifiers)
        tx0, ty0, tx1, ty1 = TEXT_AREA
        self.focused = tx0 <= cx <= tx1 and ty0 <= cy <= ty1
        for label, (bx0, by0, bx1, by1) in BUTTONS.items():
            if bx0 <= cx <= bx1 and by0 <= cy <= by1 and button == "left":
                if label == "Save" and self.text:
                    self.saved.append(self.text)
                self.text = "" if label in {"Save", "Clear"} else self.text

    async def mouse_button(self, down: bool) -> None:
        self.pressed = down
        self._log("mouse_down" if down else "mouse_up")

    async def drag(self, start, end, modifiers="") -> None:
        self.cursor = list(self._desk(*end))
        self._log("drag", start=start, end=end)

    async def scroll(self, x, y, direction, amount, modifiers="") -> None:
        self._log("scroll", x=x, y=y, direction=direction, amount=amount)

    async def type_text(self, text: str) -> None:
        if self.focused:
            self.text += text
        self._log("type", text=text)

    async def key(self, combo: str, repeat: int = 1) -> None:
        code, flags = parse_combo(combo)  # validates like the real host
        name = combo.split("+")[-1].lower()
        for _ in range(repeat):
            if self.focused and name in {"return", "enter"} and not flags:
                self.text += "\n"
            elif self.focused and name in {"backspace", "delete"}:
                self.text = self.text[:-1]
        self._log("key", text=combo, repeat=repeat)

    async def hold_key(self, combo: str, duration: float) -> None:
        parse_combo(combo)
        await asyncio.sleep(min(duration, 0.01))
        self._log("hold_key", text=combo, duration=duration)

    async def cursor_position(self) -> tuple[int, int]:
        return round(self.cursor[0] * self.size[0] / W), round(self.cursor[1] * self.size[1] / H)

    async def osascript(self, script, args=None, *, language="AppleScript", timeout=90) -> Proc:
        m = re.search(r"relay:([a-z_]+)", script)
        name = m.group(1) if m else "custom"
        self.scripts.append({"name": name, "language": language, "args": args or [], "script": script})
        out = CANNED.get(name, "ok")
        return Proc(0, out if isinstance(out, str) else json.dumps(out), "")

    def info(self) -> dict:
        return {"host": "fake", "platform": "virtual", "display_points": [W, H], "screenshot": list(self.size),
                "accessibility": True, "screen_recording": True}
