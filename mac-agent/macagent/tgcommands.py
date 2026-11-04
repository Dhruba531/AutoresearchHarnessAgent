"""More Telegram commands: run the Mac from your phone without opening the app.

  /mac            battery, disk, uptime, the app in front, the model, today's spend
  /spend          today's cost per provider; /cap 2 sets the daily cap in dollars
  /open Safari    open an app
  /clip           what's on the Mac's clipboard; /copy text puts text on it
  /get name       send yourself a file from your home folder (search by name, or a ~/path)
  /lock           lock the Mac's screen
  a file or photo → saved to ~/Downloads/From Telegram

Commands that touch files, the clipboard or the screen work only in your private chat with the bot,
never in a group: anyone in a group could otherwise use them. /get never reads hidden folders
(~/.ssh, ~/.relay with your keys) and only reads inside your home folder.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from .agent import local_midnight

log = logging.getLogger("relay.telegram")
MAX_SEND = 45 * 1024 * 1024  # Telegram bots may send files up to 50 MB
PRIVATE_ONLY = {"/open", "/clip", "/copy", "/get", "/lock"}


def safe_path(raw: str, home: Path | None = None) -> Path | None:
    """A file inside the home folder and outside any hidden folder, or None."""
    home = (home or Path.home()).resolve()
    raw = raw.strip().strip('"').strip("'")
    p = home / raw[2:] if raw == "~" or raw.startswith("~/") else Path(raw)
    p = (p if p.is_absolute() else home / p).resolve()
    try:
        rel = p.relative_to(home)
    except ValueError:
        return None
    if any(part.startswith(".") for part in rel.parts):
        return None
    return p


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


class Commands:
    """Mixed into TelegramBot; uses its say/call/http/base/agent/store/models."""

    home: Path = Path.home()

    async def command(self, chat: int, text: str, group: bool) -> bool:
        """Handle one of the commands above. Returns False if `text` isn't one of them."""
        cmd, _, arg = text.partition(" ")
        cmd, arg = cmd.split("@", 1)[0].lower(), arg.strip()
        handlers = {"/mac": self.cmd_mac, "/spend": self.cmd_spend, "/cap": self.cmd_cap, "/open": self.cmd_open,
                    "/clip": self.cmd_clip, "/copy": self.cmd_copy, "/get": self.cmd_get, "/lock": self.cmd_lock}
        if cmd not in handlers:
            return False
        if group and cmd in PRIVATE_ONLY:
            await self.say(chat, f"{cmd} works only in your private chat with me.")
            return True
        try:
            await handlers[cmd](chat, arg)
        except Exception as exc:  # a missing tool or a timeout on the Mac: say so, keep the bot alive
            log.exception("telegram %s failed", cmd)
            await self.say(chat, f"{cmd} didn't work: {exc}"[:300])
        return True

    # -- status and money -------------------------------------------------------------
    async def cmd_mac(self, chat: int, arg: str) -> None:
        h = self.agent.host
        lines = []
        batt = await h.run(["pmset", "-g", "batt"], timeout=10)
        m = re.search(r"(\d+)%;\s*([^;]+)", batt.out or "")
        if m:
            lines.append(f"🔋 {m.group(1)}% ({m.group(2).strip()})")
        elif "AC Power" in (batt.out or ""):
            lines.append("🔌 On mains power")
        df = await h.run(["df", "-k", str(self.home)], timeout=10)
        rows = (df.out or "").strip().splitlines()
        if len(rows) > 1:
            f = rows[-1].split()
            try:
                total, free = int(f[1]) * 1024, int(f[3]) * 1024
                lines.append(f"💾 {human_size(free)} free of {human_size(total)}")
            except (IndexError, ValueError):
                pass
        up = await h.run(["uptime"], timeout=10)
        m = re.search(r"up\s+(.+?),\s+\d+\s+users?", up.out or "")
        if m:
            lines.append(f"⏱ Up {m.group(1).strip()}")
        front = await h.osascript('tell application "System Events" to get name of first application process '
                                  'whose frontmost is true')
        if front.code == 0 and front.out.strip():
            lines.append(f"🪟 In front: {front.out.strip()}")
        running = len(self.agent.running)
        lines.append(f"⚙️ {running} task{'s' if running != 1 else ''} running")
        if self.models is not None:
            a = self.models.status()["active"]
            lines.append(f"🧠 {a['label']} · {a['model'].split('/')[-1]}")
        spent = self.store.spend_since(local_midnight())
        lines.append(f"💸 ${spent:.3f} today of ${self.agent.settings.daily_cap_usd:.2f} cap")
        await self.say(chat, "\n".join(lines))

    async def cmd_spend(self, chat: int, arg: str) -> None:
        spent, cap = self.store.spend_since(local_midnight()), self.agent.settings.daily_cap_usd
        lines = [f"Today: ${spent:.3f} of your ${cap:.2f} daily cap."]
        if self.models is not None:
            for p in self.models.status()["providers"]:
                if p["today_calls"]:
                    lines.append(f"• {p['label']}: ${p['today_usd']:.3f} · {p['today_calls']} calls")
        lines.append("Set the cap with /cap 2 (dollars a day).")
        await self.say(chat, "\n".join(lines))

    async def cmd_cap(self, chat: int, arg: str) -> None:
        try:
            cap = float(arg.lstrip("$"))
            if not 0 <= cap <= 1000:
                raise ValueError
        except ValueError:
            return await self.say(chat, "Send the cap in dollars a day, for example: /cap 2")
        self.agent.settings.daily_cap_usd = cap
        saved = self.store.get("settings", {})
        saved["daily_cap_usd"] = cap
        self.store.put("settings", saved)
        await self.say(chat, f"Daily cap set to ${cap:.2f}. Relay stops spending when it's reached, until midnight.")

    # -- the Mac -------------------------------------------------------------------------
    async def cmd_open(self, chat: int, arg: str) -> None:
        if not arg:
            return await self.say(chat, "Which app? For example: /open Safari")
        r = await self.agent.host.run(["open", "-a", arg], timeout=20)
        await self.say(chat, f"Opened {arg}." if r.code == 0 else f"Couldn't open “{arg}”: {(r.err or r.out).strip()[:200]}")

    async def cmd_clip(self, chat: int, arg: str) -> None:
        r = await self.agent.host.run(["pbpaste"], timeout=10)
        text = (r.out or "").strip()
        if not text:
            return await self.say(chat, "The Mac's clipboard has no text.")
        more = f"\n\n(… {len(text) - 3500} more characters)" if len(text) > 3500 else ""
        await self.say(chat, text[:3500] + more)

    async def cmd_copy(self, chat: int, arg: str) -> None:
        if not arg:
            return await self.say(chat, "Send the text after the command, for example: /copy hello from my phone")
        r = await self.agent.host.run(["pbcopy"], stdin=arg, timeout=10)
        await self.say(chat, "Copied to the Mac's clipboard." if r.code == 0 else "Couldn't set the clipboard.")

    async def cmd_lock(self, chat: int, arg: str) -> None:
        # Ctrl-Cmd-Q is macOS's own "Lock Screen"
        r = await self.agent.host.osascript('tell application "System Events" to keystroke "q" using '
                                            '{control down, command down}')
        if r.code != 0:
            r = await self.agent.host.run(["pmset", "displaysleepnow"], timeout=10)
        await self.say(chat, "🔒 Locked the Mac." if r.code == 0 else "Couldn't lock the screen.")

    # -- files ------------------------------------------------------------------------------
    async def cmd_get(self, chat: int, arg: str) -> None:
        if not arg:
            return await self.say(chat, "Which file? A name (/get resume.pdf) or a path (/get ~/Documents/notes.txt).")
        if "/" in arg or arg.startswith("~"):
            p = safe_path(arg, self.home)
            if p is None:
                return await self.say(chat, "I only send files from your home folder, and never from hidden folders.")
            if not p.is_file():
                return await self.say(chat, f"No file at {arg}.")
            return await self.send_file(chat, p)
        r = await self.agent.host.run(["mdfind", "-onlyin", str(self.home), "-name", arg], timeout=20)
        hits = [p for p in (safe_path(x, self.home) for x in (r.out or "").splitlines() if x.strip())
                if p and p.is_file()][:5]
        if not hits:
            return await self.say(chat, f"No file named like “{arg}” in your home folder.")
        if len(hits) == 1:
            return await self.send_file(chat, hits[0])
        self.file_choices[chat] = [str(p) for p in hits]
        rows = [[{"text": f"{p.name} · {p.parent.name}", "callback_data": f"f:{i}"}] for i, p in enumerate(hits)]
        await self.say(chat, "Which one?", reply_markup={"inline_keyboard": rows})

    async def send_file(self, chat: int, p: Path) -> None:
        size = p.stat().st_size
        if size > MAX_SEND:
            return await self.say(chat, f"{p.name} is {human_size(size)}; Telegram bots can send up to 50 MB.")
        with p.open("rb") as fh:
            r = await self.http.post(f"{self.base}/sendDocument", data={"chat_id": str(chat), "caption": str(p)[:1000]},
                                     files={"document": (p.name, fh)})
        if not r.json().get("ok"):
            await self.say(chat, f"Couldn't send it: {r.json().get('description')}")

    async def on_file_choice(self, chat: int, idx: str) -> str:
        choices = self.file_choices.get(chat) or []
        try:
            p = safe_path(choices[int(idx)], self.home)
        except (ValueError, IndexError):
            return "That list is old; send /get again."
        if p is None or not p.is_file():
            return "That file is gone."
        await self.send_file(chat, p)
        return f"Sending {p.name}"

    async def receive_file(self, chat: int, msg: dict) -> None:
        """A document or photo sent to the bot is saved on the Mac."""
        doc = msg.get("document") or (msg.get("photo") or [None])[-1]
        if not doc:
            return
        name = msg.get("document", {}).get("file_name") or f"photo-{time.strftime('%Y%m%d-%H%M%S')}.jpg"
        name = re.sub(r"[/\\\\:]+", "_", name).lstrip(".") or "file"
        if (doc.get("file_size") or 0) > 20 * 1024 * 1024:
            return await self.say(chat, "Telegram lets bots download files up to 20 MB; send a smaller one.")
        info = await self.call("getFile", file_id=doc["file_id"])
        r = await self.http.get(f"{self.base.replace('/bot', '/file/bot', 1)}/{info['file_path']}")
        folder = self.home / "Downloads" / "From Telegram"
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / name
        n = 1
        while dest.exists():
            dest = folder / f"{Path(name).stem} ({n}){Path(name).suffix}"
            n += 1
        dest.write_bytes(r.content)
        await self.say(chat, f"Saved to ~/Downloads/From Telegram/{dest.name} ({human_size(len(r.content))}).")
