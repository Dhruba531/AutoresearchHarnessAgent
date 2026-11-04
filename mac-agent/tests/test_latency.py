"""Latency: people and results are routed at once, quick work runs on the fast model, finished
answers skip a second router call, Telegram shows "typing…", and the cheap fast providers
(Gemini, GPT-5-era OpenAI) get the parameters they need."""

import asyncio
import json
import os
import time

import httpx
import pytest

from macagent.agent import Agent
from macagent.config import Settings
from macagent.host.fake import FakeHost
from macagent.llm import OpenAICompatAdapter, cost
from macagent.router import Router, WebChannel
from macagent.store import Store
from macagent.telegram import TelegramBot
from macagent.tools import SPECS
from tests.mock_claude import MockClaude, tool


def settings_for(tmp_path, **env):
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        return Settings(home=tmp_path / "home")
    finally:
        for k, v in old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


async def until(pred, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met")


def test_gemini_keeps_thought_signatures_and_answers_fast(tmp_path):
    s = settings_for(tmp_path, RELAY_PROVIDER="gemini", GEMINI_API_KEY="AIza-test", RELAY_MODEL="", RELAY_ROUTER_MODEL="")
    assert (s.model, s.router_model, s.api_key()) == ("gemini-3.8-flash", "gemini-3.5-flash-lite", "AIza-test")
    bodies = []

    def handler(req):
        bodies.append(json.loads(req.content))
        if len(bodies) == 1:
            return httpx.Response(200, json={"choices": [{"message": {"content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "calendar_events", "arguments": "{}"},
                 "extra_content": {"google": {"thought_signature": "SIG123"}}}]}}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 20}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "Two meetings today."}}],
                                         "usage": {"prompt_tokens": 1200, "completion_tokens": 10}})

    ad = OpenAICompatAdapter("gemini", "AIza-test", s.model, transport=httpx.MockTransport(handler))
    assert ad.base == "https://generativelanguage.googleapis.com/v1beta/openai"
    history = [{"role": "user", "content": "what's on today"}]
    t1 = asyncio.run(ad.step("SYS", history, SPECS))
    assert t1.content[0]["extra_content"] == {"google": {"thought_signature": "SIG123"}}
    history += [{"role": "assistant", "content": t1.content},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "2 events"}]}]
    t2 = asyncio.run(ad.step("SYS", history, SPECS))
    sent_back = bodies[1]["messages"][2]["tool_calls"][0]
    assert sent_back["extra_content"] == {"google": {"thought_signature": "SIG123"}}  # Gemini 3 requires it
    assert bodies[0]["reasoning_effort"] == "low" and "max_tokens" in bodies[0]
    assert t2.content == [{"type": "text", "text": "Two meetings today."}]
    assert t1.cost_usd == cost("gemini-3.8-flash", 1000, 20) > 0


def test_gpt5_parameters_and_reasoning_fallback(tmp_path):
    s = settings_for(tmp_path, RELAY_PROVIDER="openai", OPENAI_API_KEY="sk-proj-x", RELAY_MODEL="", RELAY_ROUTER_MODEL="")
    assert (s.model, s.router_model) == ("gpt-5.4-mini", "gpt-5.4-nano")
    bodies = []

    def handler(req):
        bodies.append(json.loads(req.content))
        if "reasoning_effort" in bodies[-1]:
            return httpx.Response(400, json={"error": {"message": "Unsupported parameter: 'reasoning_effort'"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}], "usage": {}})

    ad = OpenAICompatAdapter("openai", "k", "gpt-5.4-mini", transport=httpx.MockTransport(handler))
    turn = asyncio.run(ad.step("SYS", [{"role": "user", "content": "hi"}], SPECS))
    assert "max_completion_tokens" in bodies[0] and "max_tokens" not in bodies[0]  # GPT-5-era parameter
    assert len(bodies) == 2 and "reasoning_effort" not in bodies[1] and turn.content[0]["text"] == "ok"
    asyncio.run(ad.step("SYS", [{"role": "user", "content": "again"}], SPECS))
    assert len(bodies) == 3 and "reasoning_effort" not in bodies[2]  # remembered: no second failed call


def build(tmp_path, worker, light, *, batch_ms=1500):
    settings = Settings(home=tmp_path / "home", approval_timeout_s=10, router_batch_ms=batch_ms, router_max_wait_ms=4000)
    store = Store(str(settings.db_path))
    wmock, lmock = MockClaude(worker), MockClaude(light)
    agent = Agent(settings, store, FakeHost(), wmock.adapter, light_factory=lmock.router_adapter)
    router = Router(settings, store, agent, lmock.router_adapter, None)
    router.add_channel(WebChannel(store, agent))
    return settings, store, agent, router, wmock, lmock


@pytest.mark.asyncio
async def test_people_skip_the_batching_window_triggers_do_not(tmp_path):
    stamps = []

    def light(body):
        stamps.append(time.monotonic())
        return [tool("i", "ignore", {"reason": "test"})], "tool_use"

    settings, store, agent, router, wmock, lmock = build(tmp_path, lambda b: ([], "end_turn"), light, batch_ms=1500)
    loop = asyncio.create_task(router.run())
    t0 = time.monotonic()
    router.human("web", "what's my battery?", "me")
    await until(lambda: len(stamps) == 1)
    person = stamps[0] - t0
    t1 = time.monotonic()
    router.submit({"kind": "trigger", "source": "trigger:3", "text": "a file appeared", "sender": "folder:3",
                   "trigger": {"id": 3, "name": "dl", "kind": "webhook", "instruction": "file it"}})
    await until(lambda: len(stamps) == 2)
    trigger = stamps[1] - t1
    loop.cancel()
    assert person < 0.5, f"a person waited {person:.2f}s for the router"
    assert trigger >= 1.4  # automations still batch, so a burst becomes one call


@pytest.mark.asyncio
async def test_quick_tasks_run_on_the_fast_model_and_answer_directly(tmp_path):
    def light(body):
        if "router of Relay" in body["system"]:
            return [tool("d", "dispatch_task", {"title": "Battery", "instructions": "Report the battery level.",
                                                "quick": True})], "tool_use"
        return [{"type": "text", "text": "Battery is at 81%."}], "end_turn"  # the quick worker, on the light model

    settings, store, agent, router, wmock, lmock = build(tmp_path, lambda b: ([], "end_turn"), light)
    agent.start()
    loop = asyncio.create_task(router.run())
    router.human("web", "battery?", "me")
    await until(lambda: any(m["text"] == "Battery is at 81%." for m in store.chat("web")))
    loop.cancel()
    await agent.stop()
    task = store.tasks()[0]
    assert task["model"] == settings.router_model and task["status"] == "done"
    assert wmock.requests == []  # the heavy model was never called
    router_calls = [r for r in lmock.requests if "router of Relay" in r["system"]]
    assert len(router_calls) == 1  # dispatch only: the answer went straight to the user


@pytest.mark.asyncio
async def test_failed_tasks_still_go_through_the_router(tmp_path):
    def worker(body):
        raise AssertionError  # not reached

    def light(body):
        if "ended failed" in body["messages"][0]["content"]:
            return [tool("r", "reply", {"text": "That didn't work: no network. Want me to retry later?"})], "tool_use"
        return [tool("d", "dispatch_task", {"title": "Fetch", "instructions": "x"})], "tool_use"

    settings, store, agent, router, wmock, lmock = build(tmp_path, worker, light)
    tid = store.create_task("x", origin="web", title="Fetch")
    store.update_task(tid, status="failed", error="no network")
    loop = asyncio.create_task(router.run())
    router.on_task_finished(store.task(tid))
    await until(lambda: any("didn't work" in m["text"] for m in store.chat("web")))
    loop.cancel()


@pytest.mark.asyncio
async def test_telegram_keeps_typing_while_relay_works(tmp_path):
    settings = Settings(home=tmp_path / "home")
    store = Store(str(settings.db_path))
    agent = Agent(settings, store, FakeHost(), lambda: None)
    calls = []

    def handler(req):
        calls.append((req.url.path.rsplit("/", 1)[-1], json.loads(req.content or b"{}")))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(calls)}})

    bot = TelegramBot("123:abc", agent, store, transport=httpx.MockTransport(handler))
    store.x("INSERT INTO telegram_chats(chat_id, name, created_at) VALUES (5, 'me', 0)")
    bot.show_typing(5)
    await until(lambda: ("sendChatAction", {"chat_id": 5, "action": "typing"}) in calls)
    bot.show_typing(5)  # already showing: no second loop
    tid = store.create_task("long job", origin="telegram:5")
    agent.running[tid] = None
    await bot.send("telegram:5", "On it — task #1")
    assert not bot.typing[5].done()  # a task for this chat is still running: keep "typing…"
    agent.running.pop(tid)
    await bot.send("telegram:5", "Done.")
    assert 5 not in bot.typing  # answered, nothing running: stop
