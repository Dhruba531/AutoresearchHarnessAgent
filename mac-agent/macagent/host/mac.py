"""The real Mac: `screencapture` for pixels, Quartz CGEvents for mouse and keys.

Coordinate spaces, which is where screen agents usually go wrong:

* The display has a size in *points* (CGDisplayBounds), which is what
  CGEvents use.
* `screencapture` returns *pixels*: 2x the points on Retina displays.
* Claude sees our downscaled screenshot and answers in *its* pixel space.

So every screenshot is resized to `fit(points)` and Claude's coordinates are
divided by `scale = screenshot_width / points_width` before posting events.

Needs macOS permissions for the process running Relay (Terminal, iTerm, or
the Python binary under launchd): Screen Recording, Accessibility, and
Automation for each app it scripts. `python -m macagent doctor` checks them.
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import tempfile
import time

from PIL import Image

from . import Host, Shot, encode_jpeg, fit
from .keys import FLAGS, parse_combo, parse_modifiers

try:  # pyobjc-framework-Quartz
    import Quartz as Q
except ImportError:  # pragma: no cover - reported by `doctor`
    Q = None


def _framework(name: str):
    try:
        return ctypes.cdll.LoadLibrary(f"/System/Library/Frameworks/{name}.framework/{name}")
    except OSError:
        return None


def accessibility_trusted() -> bool | None:
    lib = _framework("ApplicationServices")
    if lib is None:
        return None
    lib.AXIsProcessTrusted.restype = ctypes.c_bool
    return bool(lib.AXIsProcessTrusted())


def screen_capture_allowed() -> bool | None:
    lib = _framework("CoreGraphics")
    if lib is None or not hasattr(lib, "CGPreflightScreenCaptureAccess"):
        return None
    lib.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
    return bool(lib.CGPreflightScreenCaptureAccess())


class MacHost(Host):
    name = "mac"

    def __init__(self, max_edge: int = 1440):
        if Q is None:
            raise RuntimeError("pyobjc-framework-Quartz is not installed: pip install pyobjc-framework-Quartz")
        self.max_edge = max_edge
        self._pressed = False

    # -- geometry ------------------------------------------------------------
    def _points(self) -> tuple[float, float]:
        b = Q.CGDisplayBounds(Q.CGMainDisplayID())
        return float(b.size.width), float(b.size.height)

    def _shot_size(self) -> tuple[int, int]:
        w, h = self._points()
        return fit(int(w), int(h), self.max_edge)

    def _to_points(self, x: float, y: float) -> tuple[float, float]:
        pw, ph = self._points()
        sw, sh = self._shot_size()
        return x * pw / sw, y * ph / sh

    def _from_points(self, x: float, y: float) -> tuple[int, int]:
        pw, ph = self._points()
        sw, sh = self._shot_size()
        return round(x * sw / pw), round(y * sh / ph)

    # -- screen --------------------------------------------------------------
    async def _capture(self) -> Image.Image:
        fd, path = tempfile.mkstemp(suffix=".png", prefix="relay-")
        os.close(fd)
        try:
            # -x: no shutter sound, -C: include the cursor, -m: main display
            proc = await self.run(["screencapture", "-x", "-C", "-m", "-t", "png", path], timeout=20)
            if proc.code != 0 or os.path.getsize(path) == 0:
                raise RuntimeError(
                    "screencapture failed — grant Screen Recording permission to the app running Relay "
                    "(System Settings → Privacy & Security → Screen Recording). " + proc.err.strip()
                )
            img = Image.open(path)
            img.load()
            return img
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    async def screenshot(self) -> Shot:
        img = await self._capture()
        w, h = self._shot_size()
        small = img.resize((w, h), Image.LANCZOS)
        return Shot(encode_jpeg(small), "image/jpeg", w, h)

    async def zoom(self, region: list[int]) -> Shot:
        img = await self._capture()
        sw, sh = self._shot_size()
        fx, fy = img.width / sw, img.height / sh  # screenshot px → capture px
        x0, y0, x1, y1 = (int(v) for v in region)
        box = (max(0, round(x0 * fx)), max(0, round(y0 * fy)), min(img.width, round(x1 * fx)), min(img.height, round(y1 * fy)))
        if box[2] <= box[0] or box[3] <= box[1]:
            raise ValueError("zoom region is empty")
        crop = img.crop(box)
        w, h = fit(crop.width, crop.height, self.max_edge)
        # Upscale small regions to the usual screenshot size so detail is legible.
        s = min(sw / crop.width, sh / crop.height)
        if s > 1:
            w, h = fit(round(crop.width * s), round(crop.height * s), self.max_edge)
        return Shot(encode_jpeg(crop.resize((w, h), Image.LANCZOS)), "image/jpeg", w, h)

    # -- events --------------------------------------------------------------
    @staticmethod
    def _post(evt) -> None:
        Q.CGEventPost(Q.kCGHIDEventTap, evt)

    def _here(self) -> tuple[float, float]:
        loc = Q.CGEventGetLocation(Q.CGEventCreate(None))
        return float(loc.x), float(loc.y)

    def _mouse(self, kind, pt, button=None, flags: int = 0, clicks: int = 1) -> None:
        evt = Q.CGEventCreateMouseEvent(None, kind, pt, button if button is not None else Q.kCGMouseButtonLeft)
        if clicks > 1:
            Q.CGEventSetIntegerValueField(evt, Q.kCGMouseEventClickState, clicks)
        if flags:
            Q.CGEventSetFlags(evt, flags)
        self._post(evt)

    async def move(self, x: int, y: int) -> None:
        pt = self._to_points(x, y)
        kind = Q.kCGEventLeftMouseDragged if self._pressed else Q.kCGEventMouseMoved
        self._mouse(kind, pt)

    async def click(self, x, y, button="left", count=1, modifiers="") -> None:
        pt = self._to_points(x, y) if x is not None and y is not None else self._here()
        flags = parse_modifiers(modifiers)
        downs = {"left": (Q.kCGEventLeftMouseDown, Q.kCGEventLeftMouseUp, Q.kCGMouseButtonLeft),
                 "right": (Q.kCGEventRightMouseDown, Q.kCGEventRightMouseUp, Q.kCGMouseButtonRight),
                 "middle": (Q.kCGEventOtherMouseDown, Q.kCGEventOtherMouseUp, Q.kCGMouseButtonCenter)}
        down, up, btn = downs[button]
        self._mouse(Q.kCGEventMouseMoved, pt)
        await asyncio.sleep(0.03)
        for i in range(1, count + 1):
            self._mouse(down, pt, btn, flags, i)
            self._mouse(up, pt, btn, flags, i)
            await asyncio.sleep(0.04)

    async def mouse_button(self, down: bool) -> None:
        pt = self._here()
        self._mouse(Q.kCGEventLeftMouseDown if down else Q.kCGEventLeftMouseUp, pt)
        self._pressed = down

    async def drag(self, start, end, modifiers="") -> None:
        flags = parse_modifiers(modifiers)
        a, b = self._to_points(*start), self._to_points(*end)
        self._mouse(Q.kCGEventMouseMoved, a)
        self._mouse(Q.kCGEventLeftMouseDown, a, flags=flags)
        steps = 20
        for i in range(1, steps + 1):
            p = (a[0] + (b[0] - a[0]) * i / steps, a[1] + (b[1] - a[1]) * i / steps)
            self._mouse(Q.kCGEventLeftMouseDragged, p, flags=flags)
            await asyncio.sleep(0.01)
        self._mouse(Q.kCGEventLeftMouseUp, b, flags=flags)

    async def scroll(self, x, y, direction, amount, modifiers="") -> None:
        if x is not None and y is not None:
            self._mouse(Q.kCGEventMouseMoved, self._to_points(x, y))
        dy = {"up": 1, "down": -1}.get(direction, 0) * amount
        dx = {"left": 1, "right": -1}.get(direction, 0) * amount
        evt = Q.CGEventCreateScrollWheelEvent(None, Q.kCGScrollEventUnitLine, 2, dy, dx)
        flags = parse_modifiers(modifiers)
        if flags:
            Q.CGEventSetFlags(evt, flags)
        self._post(evt)

    def _key(self, code: int, down: bool, flags: int) -> None:
        evt = Q.CGEventCreateKeyboardEvent(None, code, down)
        Q.CGEventSetFlags(evt, flags)
        self._post(evt)

    async def key(self, combo: str, repeat: int = 1) -> None:
        code, flags = parse_combo(combo)
        for _ in range(max(1, min(100, repeat))):
            self._key(code, True, flags)
            self._key(code, False, flags)
            await asyncio.sleep(0.02)

    async def hold_key(self, combo: str, duration: float) -> None:
        code, flags = parse_combo(combo)
        self._key(code, True, flags)
        try:
            await asyncio.sleep(min(300.0, max(0.0, duration)))
        finally:
            self._key(code, False, flags)

    async def type_text(self, text: str) -> None:
        lines = text.split("\n")
        for i, line in enumerate(lines):
            for j in range(0, len(line), 16):
                chunk = line[j:j + 16]
                for down in (True, False):
                    evt = Q.CGEventCreateKeyboardEvent(None, 0, down)
                    Q.CGEventKeyboardSetUnicodeString(evt, len(chunk), chunk)
                    self._post(evt)
                await asyncio.sleep(0.01)
            if i < len(lines) - 1:
                await self.key("Return")

    async def cursor_position(self) -> tuple[int, int]:
        return self._from_points(*self._here())

    def info(self) -> dict:
        pw, ph = self._points()
        sw, sh = self._shot_size()
        return {"host": self.name, "platform": "darwin", "display_points": [pw, ph], "screenshot": [sw, sh],
                "accessibility": accessibility_trusted(), "screen_recording": screen_capture_allowed(),
                "checked_at": time.time()}


__all__ = ["MacHost", "accessibility_trusted", "screen_capture_allowed", "FLAGS"]
