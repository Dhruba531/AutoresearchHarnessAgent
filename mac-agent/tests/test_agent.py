import asyncio
import json
import time

import httpx2  # noqa: F401  (imported by mock_claude)
import pytest

from macagent.agent import HALT, Agent
from macagent.config import Settings
from macagent.host.fake import FakeHost
from macagent.store import Store
from tests.mock_claude import MockClaude, tool


def make(tmp_path, turns, policy="balanced", **kw):
    settings = Settings(home=tmp_path / "home", policy=policy, approval_timeout_s=5, **kw)
    store = Store(str(settings.db_path))
    host = FakeHost()
    mock = MockClaude(turns)
    agent = Agent(settings, store, host, mock.adapter)
    return agent, store, host, mock


async def run_until(agent, store, tid, statuses, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        t = store.task(tid)
        if t["status"] in statuses:
            return t
        await asyncio.sleep(0.02)
    raise AssertionError(f"task stuck in {store.task(tid)['status']}")


def results_of(req):
    last = req["messages"][-1]
    return {r["tool_use_id"]: r for r in last["content"] if r.get("type") == "tool_result"}


@pytest.mark.asyncio
async def test_screen_batch_typing_and_toolset_echo(tmp_path):
    turns = [
        lambda b: ([{"type": "thinking", "thinking": "Opening the note field.", "signature": "s1"},
                    tool("t1", "screenshot", {}, "computer"),
                    tool("t2", "left_click", {"coordinate": [500, 300]}, "computer"),
                    tool("t3", "type", {"text": "Buy milk"}, "computer")], "tool_use"),
        lambda b: ([tool("t4", "key", {"text": "super+nonsense"}, "computer"),
                    tool("t5", "type", {"text": "never typed"}, "computer")], "tool_use"),
        lambda b: ([{"type": "text", "text": "Typed the note."}], "end_turn"),
    ]
    agent, store, host, mock = make(tmp_path, turns)
    agent.start()
    tid = agent.submit("Write 'Buy milk' in the notes window")
    task = await run_until(agent, store, tid, {"done", "failed"})
    await agent.stop()
    assert task["status"] == "done" and task["result"] == "Typed the note."
    assert host.text == "Buy milk"

    first = mock.requests[0]
    assert {"type": "computer_toolset_20260801"} in first["tools"]
    assert first["thinking"] == {"type": "adaptive", "display": "updates"}
    assert first["context_management"]["edits"][0]["type"] == "clear_tool_uses_20250919"
    assert first["cache_control"] == {"type": "ephemeral"} and first["fallbacks"] == "default"
    assert "<context>" in first["messages"][0]["content"]

    r = results_of(mock.requests[1])
    assert all(x["toolset_name"] == "computer" for x in r.values())
    assert r["t1"]["content"][0]["type"] == "image" and r["t1"]["content"][0]["source"]["media_type"] == "image/jpeg"
    assert r["t2"]["content"][0]["text"] == "OK"
    # thinking block passed back verbatim (append-only history)
    assert mock.requests[1]["messages"][1]["content"][0] == {"type": "thinking", "thinking": "Opening the note field.",
                                                             "signature": "s1"}
    r2 = results_of(mock.requests[2])
    assert r2["t4"]["is_error"] and "unknown key" in json.dumps(r2["t4"]["content"])
    assert r2["t5"]["is_error"] and r2["t5"]["content"] == HALT  # batch halts after the first failure
    assert "never typed" not in host.text
    kinds = [e["kind"] for e in store.events(tid)]
    assert "note" in kinds and kinds.count("tool") == 5


@pytest.mark.asyncio
async def test_send_waits_for_phone_approval_and_denial_is_reported(tmp_path):
    turns = [
        lambda b: ([tool("m1", "messages_send", {"to": "+15551234567", "text": "Running late"})], "tool_use"),
        lambda b: ([tool("m2", "mail_send", {"to": "a@b.co", "subject": "Hi", "body": "x"})], "tool_use"),
        lambda b: ([{"type": "text", "text": "Sent the text; skipped the email."}], "end_turn"),
    ]
    agent, store, host, mock = make(tmp_path, turns)
    agent.start()
    tid = agent.submit("Text Sam I'm late and email Bo")
    await run_until(agent, store, tid, {"waiting"})
    pend = store.pending_approvals()
    assert len(pend) == 1 and pend[0]["risk"] == "send" and "+15551234567" in pend[0]["summary"]
    assert not host.scripts  # nothing ran before approval
    assert agent.decide(pend[0]["id"], True, None, "test")
    await asyncio.sleep(0.1)
    await run_until(agent, store, tid, {"waiting"})
    pend = store.pending_approvals()
    agent.decide(pend[0]["id"], False, "not now", "test")
    task = await run_until(agent, store, tid, {"done"})
    await agent.stop()
    assert [s["name"] for s in host.scripts] == ["messages_send"]
    denied = results_of(mock.requests[2])["m2"]
    assert denied["is_error"] and "declined" in json.dumps(denied) and "not now" in json.dumps(denied)
    assert task["result"].startswith("Sent the text")


@pytest.mark.asyncio
async def test_policy_tiers_and_refused_paths(tmp_path):
    secret = tmp_path / "home" / "relay.db"
    turns = [
        lambda b: ([tool("s1", "run_shell", {"command": "echo hello"}),
                    tool("s2", "read_file", {"path": str(secret)}),
                    tool("s3", "calendar_events", {"days": 2})], "tool_use"),
        lambda b: ([{"type": "text", "text": "ok"}], "end_turn"),
    ]
    agent, store, host, mock = make(tmp_path, turns, policy="autonomous")
    agent.start()
    tid = agent.submit("check things")
    await run_until(agent, store, tid, {"done"})
    await agent.stop()
    r = results_of(mock.requests[1])
    assert "hello" in r["s1"]["content"][0]["text"]
    assert r["s2"]["is_error"] and "off limits" in r["s2"]["content"][0]["text"]
    assert "Dentist" in r["s3"]["content"][0]["text"]
    assert store.pending_approvals() == []


@pytest.mark.asyncio
async def test_ask_user_followup_and_feature_fallback(tmp_path):
    turns = [
        lambda b: ([tool("q1", "ask_user", {"question": "Which calendar?"})], "tool_use"),
        lambda b: ([{"type": "text", "text": "Added to Work."}], "end_turn"),
        lambda b: ([{"type": "text", "text": "Also noted."}], "end_turn"),
    ]
    settings = Settings(home=tmp_path / "h", approval_timeout_s=5)
    store = Store(str(settings.db_path))
    mock = MockClaude(turns, reject_features={"thinking-display-updates-2026-08-18"})
    agent = Agent(settings, store, FakeHost(), mock.adapter)
    agent.start()
    tid = agent.submit("add my meeting")
    await run_until(agent, store, tid, {"waiting"})
    q = store.pending_approvals()[0]
    assert q["kind"] == "question"
    agent.decide(q["id"], True, "Work", "test")
    await run_until(agent, store, tid, {"done"})
    assert "The user answered: Work" in json.dumps(mock.requests[1]["messages"][-1])
    assert "thinking" not in mock.requests[0]  # rejected beta was dropped, request retried
    agent.message(tid, "Also add a reminder")
    task = await run_until(agent, store, tid, {"done"}, timeout=5)
    await agent.stop()
    assert task["result"] == "Also noted."
    assert mock.requests[-1]["messages"][-1] == {"role": "user", "content": "Also add a reminder"}


@pytest.mark.asyncio
async def test_cancel_repairs_history_and_daily_cap(tmp_path):
    gate = asyncio.Event()

    def slow(b):
        return ([tool("w1", "wait", {"duration": 30}, "computer")], "tool_use")

    agent, store, host, mock = make(tmp_path, [slow])
    agent.start()
    tid = agent.submit("wait around")
    await run_until(agent, store, tid, {"running"})
    await asyncio.sleep(0.3)
    agent.cancel(tid)
    task = await run_until(agent, store, tid, {"cancelled"})
    hist = store.task(tid, with_history=True)["history"]
    last = hist[-1]["content"][0]
    assert last["tool_use_id"] == "w1" and last["toolset_name"] == "computer" and last["is_error"]
    store.add_spend(None, 999)
    tid2 = agent.submit("anything")
    task2 = await run_until(agent, store, tid2, {"blocked"})
    await agent.stop()
    assert "Daily spend cap" in task2["error"]
    assert gate is not None and task["status"] == "cancelled"
