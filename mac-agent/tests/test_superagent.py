"""The super-agent layers: router (batching, light model, dispatch only), the
parallel worker pool with its screen lease, and event-driven triggers."""

import asyncio
import json
import re
import time

import pytest
from fastapi.testclient import TestClient

from macagent.agent import Agent
from macagent.auth import new_pairing_code
from macagent.config import Settings
from macagent.host.fake import FakeHost
from macagent.llm import LLMError
from macagent.router import ROUTER_TOOLS, Router, WebChannel
from macagent.server import create_app
from macagent.store import Store
from macagent.triggers import Triggers
from tests.mock_claude import MockClaude, tool


def prompt_of(body):
    first = body["messages"][0]["content"]
    return first if isinstance(first, str) else json.dumps(first)


def steps_of(body):
    return sum(1 for m in body["messages"] if m["role"] == "assistant")


def build(tmp_path, worker_script, router_script=None, router_fails=False, workers=3, policy="balanced"):
    settings = Settings(home=tmp_path / "home", policy=policy, approval_timeout_s=10, max_workers=workers,
                        router_batch_ms=300, router_max_wait_ms=1500)
    store = Store(str(settings.db_path))
    host = FakeHost()
    wmock = MockClaude(worker_script)
    agent = Agent(settings, store, host, wmock.adapter)
    rmock = MockClaude(router_script or (lambda b: ([{"type": "text", "text": "ok"}], "end_turn")))

    def router_factory():
        if router_fails:
            raise LLMError("no key")
        return rmock.router_adapter()

    triggers = Triggers(store, host, settings)
    router = Router(settings, store, agent, router_factory, triggers)
    triggers.router = router
    router.add_channel(WebChannel(store, agent))
    return settings, store, host, agent, router, triggers, wmock, rmock


async def until(pred, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met")


@pytest.mark.asyncio
async def test_router_batches_dispatches_and_reports_back(tmp_path):
    def router_script(body):
        content = body["messages"][0]["content"]
        if "ended done" in content:
            return [tool("r9", "reply", {"text": "Your dentist is booked for Thursday 9:30."})], "tool_use"
        return [tool("r1", "dispatch_task", {"title": "Book dentist",
                                             "instructions": "Book a dentist appointment on Thursday morning; the user "
                                                             "prefers Dr. Lee."}),
                tool("r2", "remember", {"fact": "The user's dentist is Dr. Lee."}),
                tool("r3", "reply", {"text": "Booking it now."})], "tool_use"

    worker = lambda b: ([{"type": "text", "text": "Booked Thursday 9:30 with Dr. Lee."}], "end_turn")
    settings, store, host, agent, router, *_ , wmock, rmock = build(tmp_path, worker, router_script)
    agent.start()
    loop = asyncio.create_task(router.run())
    for text in ("book the dentist", "thursday morning", "it's Dr. Lee"):  # a burst, one batch
        router.human("web", text, "iPhone")
    await until(lambda: any(m["text"].startswith("Booked Thursday") for m in store.chat("web")))
    loop.cancel()
    await agent.stop()

    # one call for the three messages; the worker's answer reaches the user as written, with no second call
    assert len(rmock.requests) == 1
    finished = [e for e in store.incoming_list() if e["kind"] == "task_finished"][0]
    assert [(v["by"], v["verdict"]) for v in finished["verdicts"]] == [("router", "relayed")]
    first = rmock.requests[0]
    assert first["model"] == "claude-haiku-5-5"
    assert "tools" in first and {"type": "computer_toolset_20260801"} not in first["tools"]
    assert {t["name"] for t in first["tools"]} == {t["name"] for t in ROUTER_TOOLS}  # no Mac tools: no execution
    assert "betas" not in first and "context_management" not in first
    events = first["messages"][0]["content"]
    assert all(s in events for s in ("book the dentist", "thursday morning", "it's Dr. Lee"))

    task = store.tasks()[0]
    assert task["origin"] == "web" and task["title"] == "Book dentist" and task["status"] == "done"
    assert "Dr. Lee" in prompt_of(wmock.requests[0])  # self-contained instructions reached the worker
    assert wmock.requests[0]["model"] == "claude-opus-5-5"
    assert any(m["text"] == "The user's dentist is Dr. Lee." for m in store.memories())
    roles = [(m["role"], m["text"][:12]) for m in store.chat("web")]
    assert roles[:3] == [("user", "book the den"), ("user", "thursday mor"), ("user", "it's Dr. Lee")]
    assert ("relay", "Booking it n") in roles


@pytest.mark.asyncio
async def test_router_answers_questions_but_cannot_approve(tmp_path):
    def worker(body):
        n = steps_of(body)
        if n == 0:
            return [tool("q", "ask_user", {"question": "Which flight time?"})], "tool_use"
        if n == 1:
            return [tool("s", "mail_send", {"to": "a@b.co", "subject": "x", "body": "y"})], "tool_use"
        return [{"type": "text", "text": "done"}], "end_turn"

    def router_script(body):
        c = body["messages"][0]["content"]
        q = re.search(r"question_id=(\d+)", c)
        ids = [int(x) for x in re.findall(r"approval_id=(\d+)", c)]
        calls = []
        if q:
            calls.append(tool("a1", "answer_question", {"question_id": int(q.group(1)), "answer": "the 7am one"}))
        # a confused router trying to "answer" an approval must be refused by code
        for aid in ids:
            calls.append(tool("a2", "answer_question", {"question_id": aid, "answer": "yes"}))
        return (calls or [tool("i", "ignore", {"reason": "nothing"})]), "tool_use"

    settings, store, host, agent, router, *_ = build(tmp_path, worker, router_script)
    agent.start()
    loop = asyncio.create_task(router.run())
    tid = agent.submit("book a flight", origin="web")
    await until(lambda: any(a["kind"] == "question" for a in store.pending_approvals()))
    router.human("web", "take the 7am one", "iPhone")
    await until(lambda: any(a["kind"] == "approve" for a in store.pending_approvals()))
    appr = next(a for a in store.pending_approvals() if a["kind"] == "approve")
    router.human("web", f"yes approve it approval_id={appr['id']}", "iPhone")
    await asyncio.sleep(1.0)
    assert store.approval(appr["id"])["status"] == "pending"  # still waiting for the human's button
    q = store.one("SELECT * FROM approvals WHERE kind='question'")
    assert q["status"] == "answered" and q["answer"] == "the 7am one" and q["decided_via"] == "router:web"
    agent.decide(appr["id"], False, None, "test")
    await until(lambda: store.task(tid)["status"] == "done")
    loop.cancel()
    await agent.stop()


@pytest.mark.asyncio
async def test_router_fallback_without_a_model(tmp_path):
    worker = lambda b: ([{"type": "text", "text": "Cleaned 12 files."}], "end_turn")
    settings, store, host, agent, router, *_ = build(tmp_path, worker, router_fails=True)
    agent.start()
    loop = asyncio.create_task(router.run())
    router.human("web", "tidy Downloads", "iPhone")
    await until(lambda: any("Cleaned 12 files" in m["text"] for m in store.chat("web")))
    loop.cancel()
    await agent.stop()
    texts = [m["text"] for m in store.chat("web") if m["role"] == "relay"]
    assert texts[0].startswith("On it — task #") and texts[1] == "Cleaned 12 files."  # relayed directly


@pytest.mark.asyncio
async def test_parallel_workers_share_one_screen(tmp_path):
    log: list[tuple[str, str, float]] = []

    def worker(body):
        p, n = prompt_of(body), steps_of(body)
        name = re.search(r"job-(\w)", p).group(1)
        if name in "AB":  # screen jobs
            if n == 0:
                log.append((name, "screen", time.monotonic()))
                return [tool(f"{name}1", "screenshot", {}, "computer"),
                        tool(f"{name}2", "wait", {"duration": 0.4}, "computer")], "tool_use"
            log.append((name, "end", time.monotonic()))
            return [{"type": "text", "text": "ok"}], "end_turn"
        if n == 0:  # non-screen jobs run a slow shell command
            return [tool(f"{name}s", "run_shell", {"command": "sleep 0.4"})], "tool_use"
        log.append((name, "end", time.monotonic()))
        return [{"type": "text", "text": "ok"}], "end_turn"

    settings, store, host, agent, *_ = build(tmp_path, worker, workers=4, policy="autonomous")
    agent.start()
    t0 = time.monotonic()
    ids = [agent.submit(f"job-{x}") for x in "ABCD"]
    await until(lambda: all(store.task(i)["status"] == "done" for i in ids), timeout=15)
    await agent.stop()
    elapsed = time.monotonic() - t0
    # C and D (no screen) overlapped with each other and with the screen jobs
    assert elapsed < 1.6, elapsed
    # A and B both needed the screen: the second one waited for the lease
    waits = [e for i in ids[:2] for e in store.events(i) if e["kind"] == "note" and "Waiting for the screen" in e["data"]["text"]]
    assert len(waits) == 1
    shot_times = {}
    for i, name in zip(ids[:2], "AB"):
        shot_times[name] = min(e["created_at"] for e in store.events(i) if e["kind"] == "tool")
    end_times = {name: store.task(i)["ended_at"] for i, name in zip(ids[:2], "AB")}
    first, second = sorted("AB", key=lambda n: shot_times[n])
    assert shot_times[second] >= end_times[first] - 0.05  # never on screen at the same time
    assert agent.screen_owner is None


@pytest.mark.asyncio
async def test_folder_trigger_fires_once_file_is_stable(tmp_path):
    watched = tmp_path / "Downloads"
    watched.mkdir()
    (watched / "old.pdf").write_text("x")
    worker = lambda b: ([{"type": "text", "text": "Filed it."}], "end_turn")
    settings, store, host, agent, router, triggers, wmock, _ = build(tmp_path, worker, router_fails=True)
    agent.start()
    loop = asyncio.create_task(router.run())
    t = triggers.create("Receipts", "folder", {"path": str(watched), "pattern": "*.pdf"}, "File receipts into ~/Receipts")
    await triggers.poll(store.trigger(t["id"]))              # baseline: old.pdf must not fire
    (watched / "receipt.pdf").write_text("total $12")
    (watched / "movie.mp4.crdownload").write_text("partial")
    await triggers.poll(store.trigger(t["id"]))              # first sighting: wait until it stops growing
    assert store.trigger(t["id"])["fire_count"] == 0
    await triggers.poll(store.trigger(t["id"]))              # stable → fire
    await triggers.poll(store.trigger(t["id"]))              # no double fire
    assert store.trigger(t["id"])["fire_count"] == 1
    await until(lambda: store.tasks() and store.tasks()[0]["status"] == "done")
    loop.cancel()
    await agent.stop()
    p = prompt_of(wmock.requests[0])
    assert "File receipts into ~/Receipts" in p and "receipt.pdf" in p and "old.pdf" not in p
    assert "data, not instructions" in p and ".crdownload" not in p
    with pytest.raises(ValueError):
        triggers.create("bad", "folder", {"path": str(settings.home)}, "watch relay")


def test_webhook_and_chat_over_http(tmp_path):
    settings, store, host, agent, router, triggers, *_ = build(tmp_path, lambda b: ([{"type": "text", "text": "ok"}], "end_turn"),
                                                               router_fails=True)
    app = create_app(settings, store, agent, router=router, triggers=triggers)
    with TestClient(app) as c:
        token = c.post("/api/pair", json={"code": new_pairing_code(store), "name": "p"}).json()["token"]
        c.headers.update({"Authorization": f"Bearer {token}"})
        t = c.post("/api/triggers", json={"name": "Arrived home", "kind": "webhook", "config": {},
                                          "instruction": "Turn on the office lights playlist"}).json()
        assert t["hook_path"] == f"/api/hooks/{t['id']}" and t["secret"].startswith("hk_")
        anon = TestClient(app)
        assert anon.post(t["hook_path"], json={"where": "home"}).status_code == 404
        assert anon.post(t["hook_path"], json={"where": "home"}, headers={"X-Relay-Secret": "nope"}).status_code == 404
        assert anon.post(t["hook_path"], json={"where": "home"}, headers={"X-Relay-Secret": t["secret"]}).status_code == 200
        assert c.post("/api/triggers", json={"name": "x", "kind": "telepathy", "instruction": "nope"}).status_code == 400
        c.post("/api/chat", json={"text": "what's running?"})
        end = time.time() + 10
        while time.time() < end and len([m for m in c.get("/api/chat").json() if m["role"] == "relay"]) < 2:
            time.sleep(0.05)
        msgs = c.get("/api/chat").json()
        assert msgs[0]["role"] == "user" and msgs[0]["text"] == "what's running?"
        assert any("Arrived home" in m["text"] for m in msgs if m["role"] == "relay")
        st = c.get("/api/state").json()
        assert st["router"]["enabled"] and st["workers"] == 3
        assert c.patch(f"/api/triggers/{t['id']}", json={"enabled": False}).json()["enabled"] == 0
        assert anon.post(t["hook_path"], headers={"X-Relay-Secret": t["secret"]}).status_code == 404  # disabled
