"""Fewer interruptions and fewer tries: "allow for this task", and how-tos saved for next time."""

import asyncio
import json
import time

import httpx
import pytest

from macagent.agent import Agent
from macagent.config import Settings
from macagent.host.fake import FakeHost
from macagent.store import Store
from macagent.telegram import TelegramBot
from tests.mock_claude import MockClaude, tool


def steps(body):
    return sum(1 for m in body["messages"] if m["role"] == "assistant")


async def until(pred, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met")


def make(tmp_path, worker):
    settings = Settings(home=tmp_path / "home", policy="balanced", approval_timeout_s=10)
    store = Store(str(settings.db_path))
    mock = MockClaude(worker)
    return settings, store, Agent(settings, store, FakeHost(), mock.adapter), mock


@pytest.mark.asyncio
async def test_allow_for_this_task_stops_repeat_prompts(tmp_path):
    def worker(body):
        n = steps(body)
        if n < 3:  # three sends of the same kind
            return [tool(f"m{n}", "mail_send", {"to": f"p{n}@x.com", "subject": "notes", "body": "hi"})], "tool_use"
        if n == 3:  # a high-risk action still asks
            return [tool("d", "run_shell", {"command": "rm -rf ~/Documents/old"})], "tool_use"
        return [{"type": "text", "text": "Done."}], "end_turn"

    settings, store, agent, mock = make(tmp_path, worker)
    agent.start()
    tid = agent.submit("send the notes to three people, then clean up", origin="web")
    await until(lambda: store.pending_approvals())
    first = store.pending_approvals()[0]
    assert first["tool"] == "mail_send"
    assert agent.decide(first["id"], True, None, "test", for_task=True)
    await until(lambda: any(a["tool"] == "run_shell" for a in store.pending_approvals()))
    asked = [a["tool"] for a in store.all("SELECT tool FROM approvals WHERE task_id=?", (tid,))]
    assert asked == ["mail_send", "run_shell"]  # sends 2 and 3 didn't ask; the risky one did
    danger = store.pending_approvals()[0]
    agent.decide(danger["id"], True, None, "test", for_task=True)  # can't grant high risk for the task
    await until(lambda: store.task(tid)["status"] == "done")
    assert ("run_shell", "danger") not in agent.grants.get(tid, set()) and tid not in agent.grants  # ended with task
    allowed = [e for e in store.events(tid) if e["kind"] == "decision" and e["data"].get("via") == "allowed for this task"]
    assert len(allowed) == 2  # still logged, so you can see what ran
    await agent.stop()


@pytest.mark.asyncio
async def test_telegram_allow_for_this_task_button(tmp_path):
    settings, store, agent, mock = make(tmp_path, lambda b: ([], "end_turn"))
    calls = []

    def handler(req):
        calls.append(json.loads(req.content or b"{}"))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 3}})

    bot = TelegramBot("1:a", agent, store, transport=httpx.MockTransport(handler))
    store.x("INSERT INTO telegram_chats(chat_id, name, created_at) VALUES (5, 'me', 0)")
    tid = store.create_task("x", origin="telegram:5", title="Send notes")
    aid = store.add_approval(tid, "approve", "mail_send", {}, "send", "Send email to p@x.com")
    await bot.approval(store.approval(aid), store.task(tid))
    rows = calls[-1]["reply_markup"]["inline_keyboard"]
    assert rows[1][0] == {"text": "✅ Allow for this task", "callback_data": f"a:{aid}:2"}
    await bot.handle({"callback_query": {"id": "q", "data": f"a:{aid}:2", "message": {"chat": {"id": 5}, "message_id": 3, "text": "t"}}})
    assert store.approval(aid)["status"] == "approved" and ("mail_send", "send") in agent.grants[tid]
    assert "allowed for the rest of this task" in calls[-1]["text"]
    did = store.add_approval(tid, "approve", "run_shell", {}, "danger", "rm -rf")
    await bot.approval(store.approval(did), store.task(tid))
    assert len(calls[-1]["reply_markup"]["inline_keyboard"]) == 1  # no "allow for task" on high risk


@pytest.mark.asyncio
async def test_howto_saved_and_used_next_time(tmp_path):
    def worker(body):
        prompt = json.dumps(body["messages"][0]["content"])
        if "first time" in prompt and steps(body) == 0:
            return [tool("h", "save_howto", {"app": "WhatsApp", "goal": "send a file to a contact",
                                             "steps": "1. copy the file 2. open the chat 3. paste, press Return"})], "tool_use"
        return [{"type": "text", "text": "Done."}], "end_turn"

    settings, store, agent, mock = make(tmp_path, worker)
    agent.start()
    a = agent.submit("first time: send report.pdf to Sam on WhatsApp", origin="web")
    await until(lambda: store.task(a)["status"] == "done")
    b = agent.submit("send notes.pdf to Priya on whatsapp", origin="web")
    await until(lambda: store.task(b)["status"] == "done")
    c = agent.submit("what's on my calendar", origin="web")
    await until(lambda: store.task(c)["status"] == "done")
    await agent.stop()
    prompts = [json.dumps(r["messages"][0]["content"]) for r in mock.requests]
    second = next(p for p in prompts if "notes.pdf" in p)
    assert "How-tos saved by earlier tasks" in second and "paste, press Return" in second
    assert "How-tos saved" not in next(p for p in prompts if "calendar" in p)  # only when the app is mentioned
