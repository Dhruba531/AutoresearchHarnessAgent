"""The extra Telegram commands, against a stand-in Mac and a mock Telegram API."""

import json
from pathlib import Path

import httpx
import pytest

from macagent.agent import Agent
from macagent.config import Settings
from macagent.host import Proc
from macagent.host.fake import FakeHost
from macagent.store import Store
from macagent.telegram import TelegramBot
from macagent.tgcommands import safe_path


class Mac(FakeHost):
    """Answers the few commands the bot runs, like macOS would."""

    def __init__(self):
        super().__init__()
        self.clipboard, self.ran = "notes from the lab", []

    async def run(self, argv, *, timeout=60, stdin=None, cwd=None):
        self.ran.append(argv)
        out = {"pmset": " -InternalBattery-0 (id=1)\t83%; discharging; 4:10 remaining present: true",
               "df": "Filesystem 1024-blocks Used Available Capacity\n/dev/disk3 488245288 300000000 150000000 67% /",
               "uptime": "21:00  up 3 days,  4:12, 2 users, load averages: 1.2", "pbpaste": self.clipboard,
               "mdfind": stdin or ""}.get(argv[0], "")
        if argv[0] == "pbcopy":
            self.clipboard = stdin
        if argv[0] == "mdfind":
            out = "\n".join(str(p) for p in self.found)
        return Proc(0, out, "")

    async def osascript(self, script, args=None, *, language="AppleScript", timeout=90):
        self.ran.append(["osascript", script])
        return Proc(0, "Safari" if "frontmost" in script else "", "")


@pytest.fixture
def bot(tmp_path):
    settings = Settings(home=tmp_path / "relay")
    store = Store(str(settings.db_path))
    mac = Mac()
    agent = Agent(settings, store, mac, lambda: None)
    calls = []

    def handler(req):
        name = req.url.path.rsplit("/", 1)[-1]
        if "/file/bot" in str(req.url):
            return httpx.Response(200, content=b"%PDF-1.7 hello")
        body = json.loads(req.content) if req.headers.get("content-type", "").startswith("application/json") else \
            {"multipart": req.content.decode("latin-1")}
        calls.append((name, body))
        if name == "getFile":
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "documents/f.pdf"}})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    b = TelegramBot("1:a", agent, store, transport=httpx.MockTransport(handler))
    b.home = tmp_path / "home"
    (b.home / "Documents").mkdir(parents=True)
    (b.home / ".ssh").mkdir()
    (b.home / ".ssh" / "id_ed25519").write_text("PRIVATE KEY")
    (b.home / "Documents" / "resume.pdf").write_bytes(b"%PDF resume")
    store.x("INSERT INTO telegram_chats(chat_id, name, created_at) VALUES (5, 'me', 0), (-9, 'team', 0)")
    mac.found = []
    return b, mac, calls, settings, store


def msg(text=None, chat=5, kind="private", **extra):
    m = {"chat": {"id": chat, "type": kind}, "from": {"first_name": "D"}, **extra}
    if text is not None:
        m["text"] = text
    return {"message": m}


def last_text(calls):
    return [b for n, b in calls if n == "sendMessage"][-1]["text"]


@pytest.mark.asyncio
async def test_mac_status_and_spend(bot):
    b, mac, calls, settings, store = bot
    await b.handle(msg("/mac"))
    t = last_text(calls)
    assert "83%" in t and "free of" in t and "Up 3 days" in t and "In front: Safari" in t and "cap" in t
    await b.handle(msg("/cap 2"))
    assert settings.daily_cap_usd == 2.0 and store.get("settings")["daily_cap_usd"] == 2.0
    await b.handle(msg("/cap lots"))
    assert "for example" in last_text(calls) and settings.daily_cap_usd == 2.0
    await b.handle(msg("/spend"))
    assert "$2.00 daily cap" in last_text(calls)


@pytest.mark.asyncio
async def test_clipboard_open_and_lock(bot):
    b, mac, calls, *_ = bot
    await b.handle(msg("/clip"))
    assert last_text(calls) == "notes from the lab"
    await b.handle(msg("/copy hello from the bus"))
    assert mac.clipboard == "hello from the bus"
    await b.handle(msg("/open Safari"))
    assert ["open", "-a", "Safari"] in mac.ran and last_text(calls) == "Opened Safari."
    await b.handle(msg("/lock"))
    assert "Locked" in last_text(calls) and any("control down, command down" in " ".join(r) for r in mac.ran)


@pytest.mark.asyncio
async def test_private_only_in_groups(bot):
    b, mac, calls, *_ = bot
    for cmd in ("/clip", "/get resume", "/lock", "/open Safari", "/copy x"):
        await b.handle(msg(cmd, chat=-9, kind="group"))
        assert "only in your private chat" in last_text(calls)
    assert mac.ran == []  # nothing touched the Mac


@pytest.mark.asyncio
async def test_get_sends_files_but_never_secrets(bot):
    b, mac, calls, *_ = bot
    await b.handle(msg("/get ~/Documents/resume.pdf"))
    doc = [x for n, x in calls if n == "sendDocument"][-1]["multipart"]
    assert "resume.pdf" in doc and "%PDF resume" in doc
    for bad in ("~/.ssh/id_ed25519", "/etc/passwd", "~/../../etc/hosts"):
        await b.handle(msg(f"/get {bad}"))
        assert "never from hidden folders" in last_text(calls)
    mac.found = [b.home / "Documents" / "resume.pdf", b.home / ".ssh" / "id_ed25519", b.home / "Documents" / "nope.pdf"]
    n = len([1 for name, _ in calls if name == "sendDocument"])
    await b.handle(msg("/get resume"))  # one real match after filtering: sent straight away
    assert len([1 for name, _ in calls if name == "sendDocument"]) == n + 1
    (b.home / "Documents" / "resume-old.pdf").write_bytes(b"old")
    mac.found = [b.home / "Documents" / "resume.pdf", b.home / "Documents" / "resume-old.pdf"]
    await b.handle(msg("/get resume"))
    buttons = [x for name, x in calls if name == "sendMessage"][-1]["reply_markup"]["inline_keyboard"]
    assert len(buttons) == 2
    await b.handle({"callback_query": {"id": "q", "data": "f:1", "message": {"chat": {"id": 5}, "message_id": 3}}})
    assert "resume-old.pdf" in [x for name, x in calls if name == "sendDocument"][-1]["multipart"]


@pytest.mark.asyncio
async def test_files_sent_to_the_bot_land_in_downloads(bot):
    b, mac, calls, *_ = bot
    await b.handle(msg(document={"file_id": "F1", "file_name": "../../evil.pdf", "file_size": 14}))
    saved = list((b.home / "Downloads" / "From Telegram").iterdir())
    assert len(saved) == 1 and "/" not in saved[0].name and not saved[0].name.startswith(".")  # no escaping the folder
    assert saved[0].read_bytes() == b"%PDF-1.7 hello" and "Saved to" in last_text(calls)
    await b.handle(msg(chat=-9, kind="group", document={"file_id": "F2", "file_name": "x.pdf", "file_size": 1}))
    assert len(list((b.home / "Downloads" / "From Telegram").iterdir())) == 1  # not from a group


def test_safe_path(tmp_path):
    assert safe_path("~/x", tmp_path) == (tmp_path / "x").resolve()
    assert safe_path("Documents/a.pdf", tmp_path) == (tmp_path / "Documents" / "a.pdf").resolve()
    assert safe_path(".relay/env", tmp_path) is None
    assert safe_path(str(tmp_path / ".." / "etc"), tmp_path) is None
