"""Talk to your Mac from Telegram.

Long polling, so nothing on the Mac has to be reachable from the internet.
Create a bot with @BotFather, set RELAY_TELEGRAM_TOKEN, then in the chat send
`/pair CODE` with a code from `python -m macagent pair`. Only paired chats are
obeyed; everything else is ignored.

  plain text      → the router, which replies, dispatches tasks, or steers them
  reply to a task → a follow-up for that task
  /status         → current task and pending approvals
  /tasks          → recent tasks
  /screen         → a screenshot of the Mac
  /stop           → cancel the running task
  approvals       → Approve / Deny buttons; questions → reply with the answer

In a group (pair it by sending /pair CODE there), Relay behaves like the
design note's team chat: an @mention of the bot or a reply to it is an
explicit request and is admitted directly; other messages are candidates the
filter judges against your policies (turn off the bot's privacy mode in
@BotFather so it can see them). Reply chains form Threads, so follow-ups go
straight to the worker already on them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time

import httpx

from .agent import Agent
from .auth import _h  # pairing codes are stored hashed
from .store import Store

log = logging.getLogger("relay.telegram")


class TelegramBot:
    """Both an inbound source and a router channel ("telegram:<chat id>")."""

    name = "telegram"

    def __init__(self, token: str, agent: Agent, store: Store, transport=None, router=None):
        self.base = f"https://api.telegram.org/bot{token}"
        self.agent, self.store = agent, store
        self.http = httpx.AsyncClient(timeout=60, transport=transport)
        self.offset = store.get("telegram_offset", 0)
        self.task_messages: dict[tuple[int, int], int] = {}  # (chat, message) → task id
        self.question_messages: dict[tuple[int, int], int] = {}  # (chat, message) → approval id
        self.router = router
        self.me: dict | None = None
        self.roots: dict[tuple[int, int], int] = {}  # (chat, message) → root message of its reply chain
        self.typing: dict[int, asyncio.Task] = {}  # chat → "typing…" kept alive until Relay answers

    # -- plumbing --------------------------------------------------------------------
    async def call(self, method: str, **params) -> dict:
        r = await self.http.post(f"{self.base}/{method}", json=params)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"telegram {method}: {data.get('description')}")
        return data["result"]

    def show_typing(self, chat: int, limit_s: float = 180) -> None:
        """Telegram clears "typing…" after ~5 s; keep it up while Relay works for this chat."""
        t = self.typing.get(chat)
        if t and not t.done():
            return

        async def loop() -> None:
            end = time.monotonic() + limit_s
            while time.monotonic() < end:
                try:
                    await self.call("sendChatAction", chat_id=chat, action="typing")
                except Exception:
                    return
                await asyncio.sleep(4.5)

        self.typing[chat] = asyncio.create_task(loop())

    def stop_typing(self, chat: int) -> None:
        t = self.typing.pop(chat, None)
        if t:
            t.cancel()
        # still working on something this chat asked for: keep showing it
        origin = f"telegram:{chat}"
        if any((self.store.task(tid) or {}).get("origin") == origin for tid in self.agent.running):
            self.show_typing(chat)

    def chats(self) -> list[int]:
        return [r["chat_id"] for r in self.store.all("SELECT chat_id FROM telegram_chats")]

    def authorized(self, chat_id: int) -> bool:
        return chat_id in self.chats()

    async def say(self, chat_id: int, text: str, **kw) -> dict:
        return await self.call("sendMessage", chat_id=chat_id, text=text[:4000], **kw)

    async def run(self) -> None:
        while True:
            try:
                updates = await self.call("getUpdates", offset=self.offset, timeout=30,
                                          allowed_updates=["message", "callback_query"])
                for u in updates:
                    self.offset = u["update_id"] + 1
                    self.store.put("telegram_offset", self.offset)
                    try:
                        await self.handle(u)
                    except Exception:
                        log.exception("telegram update failed")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("telegram polling: %s", exc)
                await asyncio.sleep(5)

    # -- inbound ---------------------------------------------------------------------
    async def handle(self, u: dict) -> None:
        if "callback_query" in u:
            return await self.on_button(u["callback_query"])
        msg = u.get("message") or {}
        chat = (msg.get("chat") or {}).get("id")
        group = (msg.get("chat") or {}).get("type") in {"group", "supergroup"}
        text = (msg.get("text") or "").strip()
        if not chat or not text:
            return
        if text.startswith("/pair"):
            return await self.on_pair(chat, text, msg)
        if not self.authorized(chat):
            return  # silent: do not confirm the bot does anything for strangers
        reply_to = (msg.get("reply_to_message") or {}).get("message_id")
        if reply_to and (chat, reply_to) in self.question_messages:
            aid = self.question_messages.pop((chat, reply_to))
            ok = self.agent.decide(aid, True, text, "telegram")
            return await self.say(chat, "Got it." if ok else "That question was already answered.")
        if reply_to and (chat, reply_to) in self.task_messages:
            tid = self.task_messages[(chat, reply_to)]
            self.agent.message(tid, text)
            return await self.say(chat, f"Added to task #{tid}.")
        if text in {"/start", "/help"}:
            return await self.say(chat, __doc__.split("\n\n", 2)[2])
        if text == "/status":
            return await self.say(chat, self.status_text())
        if text == "/tasks":
            rows = self.store.tasks(10)
            return await self.say(chat, "\n".join(f"#{t['id']} {t['status']} — {t['title']}" for t in rows) or "No tasks yet.")
        if text == "/stop":
            running = list(self.agent.running)
            for tid in running:
                self.agent.cancel(tid)
            return await self.say(chat, f"Stopping {len(running)} task(s)." if running else "Nothing is running.")
        if text == "/screen":
            return await self.send_screen(chat)
        if self.router is not None:
            name = (msg.get("from") or {}).get("first_name") or "user"
            if group:
                return await self.on_group(chat, msg, text, name)
            self.show_typing(chat)
            self.router.human(f"telegram:{chat}", text, sender=name)
            return
        tid = self.agent.submit(text, source="telegram", origin=f"telegram:{chat}")
        sent = await self.say(chat, f"On it — task #{tid}. Reply to this message to add details.")
        self.task_messages[(chat, sent["message_id"])] = tid

    async def on_group(self, chat: int, msg: dict, text: str, sender: str) -> None:
        if self.me is None:
            try:
                self.me = await self.call("getMe")
            except Exception:
                self.me = {}
        username = (self.me or {}).get("username") or ""
        reply = msg.get("reply_to_message") or {}
        mentioned = bool(username) and f"@{username.lower()}" in text.lower()
        to_bot = (reply.get("from") or {}).get("id") == (self.me or {}).get("id") and bool(self.me)
        root = self.roots.get((chat, reply["message_id"]), reply["message_id"]) if reply else msg.get("message_id")
        self.roots[(chat, msg.get("message_id"))] = root
        if username:
            text = re.sub(rf"@{re.escape(username)}\b", "", text, flags=re.I).strip() or text
        if mentioned or to_bot:
            self.show_typing(chat)
        self.router.human(f"telegram:{chat}", text, sender=sender, source=f"telegram-group:{chat}",
                          explicit=mentioned or to_bot, thread=f"tg:{chat}:{root}")

    async def on_pair(self, chat: int, text: str, msg: dict) -> None:
        parts = text.split()
        code = parts[1].strip().upper().replace("-", "") if len(parts) > 1 else ""
        row = self.store.one("SELECT * FROM pairing_codes WHERE code_hash=?", (_h(code),)) if code else None
        if not row or row["used"] or row["expires_at"] < time.time():
            return await self.say(chat, "That pairing code is wrong or expired. Run `python -m macagent pair` on the Mac.")
        self.store.x("UPDATE pairing_codes SET used=1 WHERE code_hash=?", (_h(code),))
        name = (msg.get("chat") or {}).get("username") or (msg.get("from") or {}).get("first_name") or "telegram"
        self.store.x("INSERT OR REPLACE INTO telegram_chats(chat_id,name,created_at) VALUES(?,?,?)", (chat, name, time.time()))
        await self.say(chat, "Paired. Send me anything you want your Mac to do.")

    async def on_button(self, q: dict) -> None:
        chat = ((q.get("message") or {}).get("chat") or {}).get("id")
        if not chat or not self.authorized(chat):
            return await self.call("answerCallbackQuery", callback_query_id=q["id"])
        try:
            kind, aid, verdict = q.get("data", "").split(":")
        except ValueError:
            return await self.call("answerCallbackQuery", callback_query_id=q["id"])
        ok = kind == "a" and self.agent.decide(int(aid), verdict == "1", None, "telegram")
        await self.call("answerCallbackQuery", callback_query_id=q["id"],
                        text=("Approved" if verdict == "1" else "Denied") if ok else "Already decided")
        if ok and q.get("message"):
            label = "✅ Approved" if verdict == "1" else "⛔ Denied"
            await self.call("editMessageText", chat_id=chat, message_id=q["message"]["message_id"],
                            text=f"{q['message'].get('text', '')}\n\n{label}")

    def status_text(self) -> str:
        running = [self.store.task(t) for t in self.agent.running]
        lines = [f"Running: #{t['id']} {t['title']} ({t['status']})" for t in running if t] or ["Idle."]
        for a in self.store.pending_approvals():
            lines.append(f"Waiting on you: {a['summary']}")
        return "\n".join(lines)

    async def send_screen(self, chat: int) -> None:
        shot = await self.agent.host.screenshot()
        r = await self.http.post(f"{self.base}/sendPhoto", data={"chat_id": str(chat)},
                                 files={"photo": ("screen.jpg", shot.data, shot.media_type)})
        if not r.json().get("ok"):
            await self.say(chat, f"Could not send the screenshot: {r.json().get('description')}")

    # -- Notifier --------------------------------------------------------------------
    async def approval(self, approval: dict, task: dict) -> None:
        for chat in self.chats():
            if approval["kind"] == "question":
                sent = await self.say(chat, f"❓ {approval['summary']}\n\n(task #{task['id']} — reply to this message)",
                                      reply_markup={"force_reply": True, "selective": True})
                self.question_messages[(chat, sent["message_id"])] = approval["id"]
            else:
                buttons = {"inline_keyboard": [[{"text": "✅ Approve", "callback_data": f"a:{approval['id']}:1"},
                                                 {"text": "⛔ Deny", "callback_data": f"a:{approval['id']}:0"}]]}
                await self.say(chat, f"🔐 Approve ({approval['risk']})?\n{approval['summary']}\n\ntask #{task['id']}: "
                                     f"{task['title']}", reply_markup=buttons)

    # -- router channel ------------------------------------------------------------
    def targets(self) -> list[str]:
        return [f"telegram:{c}" for c in self.chats()]

    async def send(self, target: str, text: str, *, task_id: int | None = None, notify: bool = False) -> None:
        chat = int(target.split(":", 1)[1])
        if not self.authorized(chat):
            return
        sent = await self.say(chat, text)
        self.stop_typing(chat)
        if task_id:
            self.task_messages[(chat, sent["message_id"])] = task_id


__all__ = ["TelegramBot", "json"]
