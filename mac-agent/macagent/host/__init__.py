"""The machine Relay controls.

`Host` is the whole surface the agent and the phone's manual controls use:
screen capture, mouse and keyboard, shell, and AppleScript/JXA. All screen
coordinates are in the pixel space of the screenshots the host returns; the
host converts to the display's own coordinates.

`MacHost` is the real thing. `FakeHost` draws a virtual desktop so the server,
phone UI and tests run anywhere.
"""

from __future__ import annotations

import asyncio
import io
import os
import signal
import sys
from dataclasses import dataclass

MAX_OUTPUT = 60_000


@dataclass
class Shot:
    data: bytes
    media_type: str
    width: int
    height: int


@dataclass
class Proc:
    code: int | None
    out: str
    err: str
    timed_out: bool = False


def _clip(b: bytes) -> str:
    s = b.decode("utf-8", errors="replace")
    return s if len(s) <= MAX_OUTPUT else s[: MAX_OUTPUT // 2] + "\n…[truncated]…\n" + s[-MAX_OUTPUT // 2:]


def fit(width: int, height: int, max_edge: int) -> tuple[int, int]:
    """Largest size within max_edge on the long side and ~1.15 MP, aspect kept."""
    s = min(1.0, max_edge / max(width, height), (1_150_000 / (width * height)) ** 0.5)
    return max(1, round(width * s)), max(1, round(height * s))


def encode_jpeg(img, quality: int = 78) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue()


class Host:
    name = "host"
    platform = sys.platform

    async def run(self, argv: list[str], *, timeout: float = 60, stdin: str | None = None,
                  cwd: str | None = None) -> Proc:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=cwd, stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(stdin.encode() if stdin is not None else None), timeout)
        except asyncio.TimeoutError:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            out, err = await proc.communicate()
            return Proc(None, _clip(out), _clip(err) + f"\n[killed after {timeout:.0f}s]", True)
        except asyncio.CancelledError:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            raise
        return Proc(proc.returncode, _clip(out), _clip(err))

    async def shell(self, command: str, timeout: float = 120) -> Proc:
        sh = "/bin/zsh" if os.path.exists("/bin/zsh") and sys.platform == "darwin" else "/bin/sh"
        return await self.run([sh, "-lc" if sh.endswith("zsh") else "-c", command], timeout=timeout,
                              cwd=os.path.expanduser("~"))

    async def osascript(self, script: str, args: list[str] | None = None, *, language: str = "AppleScript",
                        timeout: float = 90) -> Proc:
        """Run AppleScript or JXA. `args` reach the script's run handler as argv,
        so values are never spliced into script source."""
        argv = ["osascript", "-l", language, "-e", script, *(args or [])]
        return await self.run(argv, timeout=timeout)

    # Screen and input — implemented by subclasses.
    async def screenshot(self) -> Shot: raise NotImplementedError
    async def zoom(self, region: list[int]) -> Shot: raise NotImplementedError
    async def click(self, x: int | None, y: int | None, button: str = "left", count: int = 1, modifiers: str = "") -> None: raise NotImplementedError
    async def move(self, x: int, y: int) -> None: raise NotImplementedError
    async def mouse_button(self, down: bool) -> None: raise NotImplementedError
    async def drag(self, start: list[int], end: list[int], modifiers: str = "") -> None: raise NotImplementedError
    async def scroll(self, x: int | None, y: int | None, direction: str, amount: int, modifiers: str = "") -> None: raise NotImplementedError
    async def type_text(self, text: str) -> None: raise NotImplementedError
    async def key(self, combo: str, repeat: int = 1) -> None: raise NotImplementedError
    async def hold_key(self, combo: str, duration: float) -> None: raise NotImplementedError
    async def cursor_position(self) -> tuple[int, int]: raise NotImplementedError

    def info(self) -> dict:
        return {"host": self.name, "platform": self.platform}


def make_host(fake: bool, max_edge: int) -> Host:
    if fake or sys.platform != "darwin":
        from .fake import FakeHost

        return FakeHost(max_edge=max_edge)
    from .mac import MacHost

    return MacHost(max_edge=max_edge)
