"""DeepSeek's Super-agent design note, section by section, against the real
SDK (mock transport), the real database, and the virtual desktop."""

import asyncio
import json
import re
import threading
import time

import pytest
from fastapi.testclient import TestClient

from macagent.agent import Agent
from macagent.auth import new_pairing_code
from macagent.config import Settings
from macagent.context import read as read_object
from macagent.filters import Filter
from macagent.host.fake import FakeHost
from macagent.machines import MachineRegistry, serve_request
from macagent.router import Router, WebChannel, cap_words
from macagent.server import create_app
from macagent.store import Store
from macagent.telegram import TelegramBot
from macagent.watchdog import Watchdog
from tests.mock_claude import MockClaude, tool


def user_text(body):
    return json.dumps(body["messages"])


def steps_of(body):
    return sum(1 for m in body["messages"] if m["role"] == "assistant")


def build(tmp_path, worker, light, *, workers=3, policy="balanced", machines=None, host=None):
    settings = Settings(home=tmp_path / "home", policy=policy, approval_timeout_s=10, max_workers=workers,
                        router_batch_ms=250, router_max_wait_ms=1200)
    store = Store(str(settings.db_path))
    host = host or FakeHost()
    wmock, lmock = MockClaude(worker), MockClaude(light)
    reg = machines(store, host) if machines else host
    agent = Agent(settings, store, reg, wmock.adapter)
    router = Router(settings, store, agent, lmock.router_adapter, None, Filter(store, lmock.router_adapter))
    router.add_channel(WebChannel(store, agent))
    return settings, store, host, agent, router, wmock, lmock


async def until(pred, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met")


def is_filter(body):
    return "event filter" in body["system"]


def is_router(body):
    return "router of Relay" in body["system"]


# §5 — Triggers, and which ones matter: code first, then a model against editable policies
@pytest.mark.asyncio
async def test_two_stage_filter(tmp_path):
    def light(body):
        txt = body["messages"][0]["content"]
        if is_filter(body):
            ids = re.findall(r'\[E(\d+)\][^\n"]*"([^"\n]*)"', txt)
            return [tool(f"v{i}", "verdict", {"event_id": f"E{i}", "admit": "lunch" not in t,
                                              "reason": "social chat" if "lunch" in t else "bug report"}) for i, t in ids], "tool_use"
        return [tool("i", "ignore", {"reason": "test"})], "tool_use"

    settings, store, host, agent, router, wmock, lmock = build(tmp_path, lambda b: ([], "end_turn"), light)
    store.add_policy("Never reply in public without a person's approval.", by="test")
    batch = [
        router.human("telegram:-42", "lunch at 1?", "Riya", source="telegram-group:-42", explicit=False),
        router.human("telegram:-42", "login fails on Android since yesterday", "Priya", source="telegram-group:-42", explicit=False),
        router.human("telegram:-42", "what changed in yesterday's release?", "Arjun", source="telegram-group:-42", explicit=True),
    ]
    mail = router.submit({"kind": "trigger", "source": "trigger:9", "sender": "noreply@shop.example", "text": "Your order shipped",
                          "trigger": {"id": 9, "name": "mail", "kind": "mail", "instruction": "triage"}})
    events = [store.incoming(i) for i in range(1, 5)]
    admitted = await router.intake(events)
    v = {e["id"]: [(x["by"], x["verdict"]) for x in store.incoming(e["id"])["verdicts"]] for e in events}
    assert v[1] == [("code", "candidate"), ("model", "drop")]                # social chat, dropped by the model
    assert v[2] == [("code", "candidate"), ("model", "admit"), ("router", "admitted")]
    assert v[3] == [("code", "admit"), ("router", "admitted")]               # explicit request skips the model
    assert v[4] == [("code", "drop")]                                        # ignore-list sender, no model call
    assert [e["id"] for e in admitted] == [2, 3]
    filt = [r for r in lmock.requests if is_filter(r)]
    assert len(filt) == 1  # one call for the batch of candidates
    prompt = filt[0]["messages"][0]["content"]
    assert "Never reply in public" in prompt and "lunch at 1?" in prompt and "what changed" not in prompt
    assert len(batch) == 3 and mail["id"] == 4


# §6 Threads & pass-through, §9 full duplex + worker reports, §4 one voice
@pytest.mark.asyncio
async def test_threads_pass_through_and_full_duplex(tmp_path):
    def worker(body):
        n, txt = steps_of(body), user_text(body)
        if n == 0:
            return [tool("j", "job_start", {"command": "sleep 1.2; echo suite passed"})], "tool_use"
        if "please also attach the PDF" in txt and "Sent to the router" not in txt:
            return [tool("r", "report", {"text": "Will attach the PDF; the long check is still running."})], "tool_use"
        if "finished: exit 0" in txt:
            return [{"type": "text", "text": "Invoice paid and PDF attached; suite passed."}], "end_turn"
        return [{"type": "text", "text": "waiting"}], "end_turn"

    def light(body):
        txt = body["messages"][0]["content"]
        if "worker_report" in txt or "from worker" in txt:
            m = re.search(r"\"([^\"]+)\"\s*$", txt.split("<events>")[1].strip(), re.M)
            return [tool("rr", "reply", {"text": "Update from W1: attaching the PDF now."})], "tool_use"
        return [tool("d", "dispatch_task", {"title": "Pay invoice", "instructions": "Pay the March invoice from ACME.",
                                            "events": ["E1"]})], "tool_use"

    settings, store, host, agent, router, wmock, lmock = build(tmp_path, worker, light, policy="autonomous")
    agent.start()
    loop = asyncio.create_task(router.run())
    router.submit({"kind": "message", "source": "web", "reply": "web", "text": "pay the ACME invoice",
                   "thread": "mail:acme invoice", "explicit": True})
    await until(lambda: store.tasks() and store.tasks()[0]["status"] == "waiting")
    tid = store.tasks()[0]["id"]
    assert store.subscribers("mail:acme invoice") == [tid]
    decisions_before = router.decisions
    # a follow-up on the same Thread: code passes it straight to W1, no router decision
    router.submit({"kind": "message", "source": "web", "reply": "web", "text": "please also attach the PDF",
                   "thread": "mail:acme invoice", "explicit": True})
    await until(lambda: any(m["text"].startswith("Update from W1") for m in store.chat("web")))
    passed = store.incoming(2)
    assert passed["stage"] == "passed" and passed["routed_to"] == [tid]
    assert router.decisions == decisions_before + 1  # only the worker's report needed a router call
    # the job finishes later and arrives as an event; the worker then finishes
    await until(lambda: store.task(tid)["status"] == "done", timeout=10)
    loop.cancel()
    await agent.stop()
    kinds = [e["kind"] for e in store.events(tid)]
    assert kinds.index("event_in") < kinds.index("job")  # answered while the job was still running
    job_msg = next(e for e in store.events(tid) if e["kind"] == "job")["data"]["text"]
    oid = re.search(r"read\(O(\d+)\)", job_msg).group(1)
    assert "suite passed" in read_object(store, f"O{oid}")


# §7 Inline & Spill for workers, Chain of Recall for the router
@pytest.mark.asyncio
async def test_spill_and_chain_of_recall(tmp_path):
    def worker(body):
        n = steps_of(body)
        if n == 0:
            return [tool("s", "run_shell", {"command": "python3 -c \"print('x'*20000 + 'END')\""})], "tool_use"
        if n == 1:
            oid = re.search(r"read\((O\d+)\)", user_text(body)).group(1)
            return [tool("r", "read", {"id": oid, "offset": 19000})], "tool_use"
        return [{"type": "text", "text": "Fixed the Android login bug in PR 412."}], "end_turn"

    def light(body):
        txt = user_text(body)
        if is_router(body) and "did the Android login thing get fixed" in txt:
            if "tool_result" not in txt:
                w = re.search(r"\[W(\d+)\]", body["messages"][0]["content"]).group(1)
                return [tool("rd", "read", {"id": f"W{w}"})], "tool_use"
            return [tool("rp", "reply", {"text": "Yes — fixed in PR 412."})], "tool_use"
        return [tool("i", "ignore", {"reason": "x"})], "tool_use"

    settings, store, host, agent, router, wmock, lmock = build(tmp_path, worker, light, policy="autonomous")
    agent.start()
    tid = agent.submit("fix the android login bug", origin="web")
    await until(lambda: store.task(tid)["status"] == "done")
    first_result = json.dumps(wmock.requests[1]["messages"][-1])
    assert "read(O" in first_result and len(first_result) < 6000  # 20k of output did not enter the window
    assert "END" in json.dumps(wmock.requests[2]["messages"][-1])   # the tail was one read() away
    loop = asyncio.create_task(router.run())
    router.human("web", "did the Android login thing get fixed?", "Priya")
    await until(lambda: any(m["text"].startswith("Yes") for m in store.chat("web")))
    loop.cancel()
    await agent.stop()
    hop = [r for r in lmock.requests if is_router(r) and "did the Android" in user_text(r)]
    assert len(hop) == 2  # one line kept → read(W) → reply
    assert "From my history" in hop[0]["messages"][0]["content"]
    assert "Fixed the Android login bug" in json.dumps(hop[1]["messages"][-1])  # read result fed back


# §8 Acting ahead: silent workers
@pytest.mark.asyncio
async def test_silent_workers_discard_or_report(tmp_path):
    def worker(body):
        n, txt = steps_of(body), user_text(body)
        if "check settings speed" in txt:
            if n == 0:
                return [tool("a", "screenshot", {}, "computer"),
                        tool("b", "mail_send", {"to": "x@y.z", "subject": "s", "body": "b"})], "tool_use"
            return [tool("d", "discard", {"reason": "could not confirm the slowdown"})], "tool_use"
        if n == 0:
            return [tool("m", "mail_draft", {"to": "sam@x.com", "subject": "Re: planning", "body": "Could we start at 11?"})], "tool_use"
        return [{"type": "text", "text": "Drafted a reply proposing 11:00; it's in Mail for you to send."}], "end_turn"

    light = lambda b: ([tool("r", "reply", {"text": "I drafted your reply to Sam; it's in Mail."})], "tool_use")
    settings, store, host, agent, router, wmock, lmock = build(tmp_path, worker, light)
    agent.start()
    loop = asyncio.create_task(router.run())
    quiet = agent.submit("check settings speed", mode="silent", origin="web")
    draft = agent.submit("draft a reply to the planning invite", mode="silent", origin="web")
    await until(lambda: store.task(quiet)["status"] == "discarded" and store.task(draft)["status"] == "done")
    await until(lambda: any("drafted your reply" in m["text"] for m in store.chat("web")))
    loop.cancel()
    await agent.stop()
    req = next(r for r in wmock.requests if "check settings speed" in user_text(r) and steps_of(r) == 1)
    assert {"type": "computer_toolset_20260801"} not in req["tools"] and "discard" in {t.get("name") for t in req["tools"]}
    res = json.dumps(req["messages"][-1])
    assert "cannot use the screen" in res and "Background mode" in res
    assert store.pending_approvals() == [] and [s["name"] for s in host.scripts] == ["mail_compose"]  # the draft only
    assert host.scripts[0]["args"][3] == "0"  # draft, never sent
    assert store.task(quiet)["result"].startswith("Discarded")
    assert not any("settings" in m["text"] for m in store.chat("web"))  # nobody heard about the discarded one


def test_chat_replies_are_capped_at_100_words():
    long = " ".join(f"word{i}." if i % 12 == 11 else f"word{i}" for i in range(180))
    out = cap_words(long, task_id=7)
    assert len(out.split()) <= 106 and out.endswith("(details in task #7)")
    assert cap_words("short reply") == "short reply"


# §10 Compute, state and environments
@pytest.mark.asyncio
async def test_task_waits_for_its_machine_and_runs_there(tmp_path):
    remote = FakeHost()
    remote.text = "MACBOOK"

    def worker(body):
        if steps_of(body) == 0:
            return [tool("a", "screenshot", {}, "computer"), tool("b", "run_shell", {"command": "echo on-the-laptop"})], "tool_use"
        return [{"type": "text", "text": "done on the laptop"}], "end_turn"

    settings, store, host, agent, router, wmock, lmock = build(tmp_path, worker, lambda b: ([], "end_turn"),
                                                              machines=MachineRegistry, policy="autonomous")
    agent.machines.register("macbook")
    agent.start()
    other = agent.submit("local job")
    tid = agent.submit("run the emulator test", machine="macbook")
    await until(lambda: store.task(tid)["status"] == "waiting" and store.task(other)["status"] == "done")
    assert any("Waiting for macbook" in e["data"].get("text", "") for e in store.events(tid))

    async def send(msg):  # an in-process "daemon" answering on the remote FakeHost
        asyncio.get_running_loop().call_soon(lambda m=msg: asyncio.create_task(answer(m)))

    async def answer(msg):
        rh.resolve(await serve_request(remote, msg))

    rh = agent.machines.attach("macbook", send, {"host": "fake", "display_points": [1280, 800]})
    await until(lambda: store.task(tid)["status"] == "done")
    await agent.stop()
    shot = next(e for e in store.events(tid) if e["kind"] == "tool" and e["data"].get("shot"))
    assert shot["data"]["ok"]
    assert "on-the-laptop" in json.dumps(wmock.requests[-1]["messages"][-1])
    assert agent.machines.status()[1] == {**agent.machines.status()[1], "name": "macbook", "online": True}


def test_sessions_resume_after_restart(tmp_path):
    settings = Settings(home=tmp_path / "h")
    store = Store(str(settings.db_path))
    tid = store.create_task("long job")
    store.update_task(tid, status="running", history=[{"role": "user", "content": "long job"},
                                                      {"role": "assistant", "content": [tool("t", "run_shell", {"command": "x"})]}])
    agent = Agent(settings, store, FakeHost(), lambda: None)
    for n in range(3):
        agent.recover()
        assert store.task(tid)["status"] == "queued" and store.task(tid)["resume_count"] == n + 1
        store.update_task(tid, status="running")
    agent.recover()
    assert store.task(tid)["status"] == "interrupted"  # a crash loop stops after three resumes
    assert "Relay restarted" in agent.inbox[tid][0]


def test_daemon_over_websocket(tmp_path):
    settings = Settings(home=tmp_path / "home")
    store = Store(str(settings.db_path))
    agent = Agent(settings, store, MachineRegistry(store, FakeHost()), lambda: None)
    app = create_app(settings, store, agent)
    with TestClient(app) as c:
        token = c.post("/api/pair", json={"code": new_pairing_code(store), "name": "p"}).json()["token"]
        c.headers.update({"Authorization": f"Bearer {token}"})
        assert c.post("/api/machines/register", json={"code": "WRONG123", "name": "macbook"}).status_code == 403
        code = c.post("/api/machines/pairing-code").json()["code"]
        mtoken = c.post("/api/machines/register", json={"code": code, "name": "macbook"}).json()["token"]
        assert c.post("/api/machines/register", json={"code": code, "name": "x"}).status_code == 403  # single use
        laptop = FakeHost()
        with c.websocket_connect(f"/api/machines/ws?token={mtoken}") as ws:
            ws.send_json({"type": "hello", "info": laptop.info()})
            got: dict = {}

            def fetch():
                got["r"] = c.get("/api/screen?machine=macbook")

            th = threading.Thread(target=fetch)
            th.start()
            req = ws.receive_json()
            assert req["method"] == "screenshot"
            ws.send_json(asyncio.run(serve_request(laptop, req)))
            th.join(10)
            assert got["r"].status_code == 200 and got["r"].content[:2] == b"\xff\xd8"
            assert {m["name"]: m["online"] for m in c.get("/api/machines").json()} == {"this-mac": True, "macbook": True}
        time.sleep(0.2)
        assert {m["name"]: m["online"] for m in c.get("/api/machines").json()}["macbook"] is False
        with pytest.raises(Exception):
            with c.websocket_connect("/api/machines/ws?token=rlm_forged") as ws:
                ws.receive_json()


# §11 The Watchdog
@pytest.mark.asyncio
async def test_watchdog_nudges_then_pauses_and_takes_proposals(tmp_path):
    def light(body):
        if "judge whether a worker" in body["system"]:
            return [tool("a", "assess", {"verdict": "stuck", "why": "same failing test 6 times",
                                         "nudge": "Stop retrying refresh_afterExpiry; read the device log first."})], "tool_use"
        txt = user_text(body)
        if "tool_result" in txt:
            return [{"type": "text", "text": "Its guidance says to rerun flaky tests; this failure is identical each time. "
                                             "I proposed allowing at most 2 reruns."}], "end_turn"
        return [tool("p", "propose_change", {"kind": "worker_guidance", "summary": "At most 2 reruns for identical failures",
                                             "data": {"text": "Rerun a failing test at most twice when the stack trace is identical."}})], "tool_use"

    settings = Settings(home=tmp_path / "h")
    store = Store(str(settings.db_path))
    agent = Agent(settings, store, FakeHost(), lambda: None)
    lmock = MockClaude(light)
    tid = store.create_task("fix the login bug")
    store.update_task(tid, status="running", started_at=time.time())
    agent.running[tid] = asyncio.get_running_loop().create_future()
    for _ in range(6):
        store.add_event(tid, "tool", {"name": "run_shell", "summary": "Run in Terminal: ./gradlew test --tests refresh_afterExpiry",
                                      "ok": False})
    wd = Watchdog(settings, store, agent, lmock.router_adapter, interval_s=0)
    rep = await wd.check()
    assert rep["infra"]["database"] == "ok" and rep["progress"][0]["verdict"] == "stuck"
    assert rep["actions"][0].startswith(f"nudged W{tid}")
    assert "Stop retrying" in agent.inbox[tid][0]
    rep2 = await wd.check()  # still stuck right after a nudge → pause and tell the maintainer
    assert rep2["actions"][0].startswith(f"paused W{tid}") and store.task(tid)["status"] == "paused"
    reply = await wd.chat("why did it keep retrying?")
    assert "2 reruns" in reply["data"]["text"]
    prop = store.proposals("pending")[0]
    assert prop["kind"] == "worker_guidance"
    wd.apply(prop["id"], True)
    assert "at most twice" in store.get("worker_guidance")
    new = agent.submit("another task")
    assert "at most twice" in store.task(new, with_history=True)["guidance"]  # new workers follow it
    with pytest.raises(ValueError):
        wd.apply(prop["id"], True)
    agent.running.clear()


@pytest.mark.asyncio
async def test_telegram_group_explicit_vs_candidate_and_threads(tmp_path):
    settings, store, host, agent, router, *_ = build(tmp_path, lambda b: ([], "end_turn"), lambda b: ([], "end_turn"))
    import httpx

    def handler(req):
        method = req.url.path.rsplit("/", 1)[-1]
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"id": 555, "username": "relay_bot"}})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    bot = TelegramBot("t", agent, store, transport=httpx.MockTransport(handler), router=router)
    store.x("INSERT INTO telegram_chats(chat_id,name,created_at) VALUES(?,?,?)", (-100, "team", time.time()))
    g = {"id": -100, "type": "supergroup"}
    await bot.handle({"update_id": 1, "message": {"message_id": 10, "chat": g, "from": {"first_name": "Riya"}, "text": "lunch at 1?"}})
    await bot.handle({"update_id": 2, "message": {"message_id": 11, "chat": g, "from": {"first_name": "Arjun"},
                                                  "text": "@relay_bot what changed yesterday?"}})
    await bot.handle({"update_id": 3, "message": {"message_id": 12, "chat": g, "from": {"first_name": "Meera"},
                                                  "text": "same for me", "reply_to_message": {"message_id": 10, "from": {"id": 9}}}})
    e1, e2, e3 = store.incoming(1), store.incoming(2), store.incoming(3)
    assert e1["source"] == "telegram-group:-100" and e1["payload"]["_explicit"] is False
    assert e2["payload"]["_explicit"] is True and e2["text"] == "what changed yesterday?"
    assert e3["thread"] == e1["thread"] == "tg:-100:10"  # a reply joins its message's Thread
    assert Filter(store).code(e1)[0] == "candidate" and Filter(store).code(e2)[0] == "admit"


# The one-page system map: every module's live counts, from the same records the views use
def test_system_map_counts_every_module(tmp_path):
    def light(body):
        txt = body["messages"][0]["content"]
        ids = re.findall(r'\[E(\d+)\][^\n"]*"([^"\n]*)"', txt)
        return [tool(f"v{i}", "verdict", {"event_id": f"E{i}", "admit": "lunch" not in t, "reason": "t"})
                for i, t in ids], "tool_use"

    settings, store, host, agent, router, wmock, lmock = build(tmp_path, lambda b: ([], "end_turn"), light)
    router.human("telegram:-42", "lunch at 1?", "Riya", source="telegram-group:-42", explicit=False)
    router.human("telegram:-42", "login fails on Android", "Priya", source="telegram-group:-42", explicit=False)
    router.human("web", "what is on my calendar?", "me")
    router.submit({"kind": "trigger", "source": "trigger:9", "sender": "noreply@shop.example", "text": "shipped",
                   "trigger": {"id": 9, "name": "mail", "kind": "mail", "instruction": "triage"}})
    asyncio.run(router.intake([store.incoming(i) for i in range(1, 5)]))
    store.verdict(3, "router", "dispatch", "started W1", stage="routed", routed_to=[1])
    store.add_chat("web", "relay", "On it.")
    store.add_chat("telegram:-42", "relay", "Looking into the login bug.")
    router.submit({"kind": "worker_report", "source": "worker:1", "text": "found it"})  # internal: not a source

    app = create_app(settings, store, agent, router=router)
    tid = store.create_task("Reproduce Android login bug", source="router", mode="silent")
    store.subscribe("tg:-42:7", tid)
    with TestClient(app) as c:
        agent.running[tid] = None  # stand-in for a live worker: the map reads which tasks are live
        assert c.get("/api/system").status_code == 401
        token = c.post("/api/pair", json={"code": new_pairing_code(store), "name": "p"}).json()["token"]
        c.headers.update({"Authorization": f"Bearer {token}"})
        x = c.get("/api/system").json()
        agent.running.pop(tid)
    assert x["sources"] == {"telegram-group": 2, "web": 1, "trigger:mail": 1}
    f = x["flow"]
    assert (f["events"], f["code_admit"], f["candidates"], f["model_admit"], f["dropped"]) == (4, 1, 2, 1, 2)
    assert (f["dispatched"], f["routed"]) == (1, 1)
    assert x["replies"] == {"web": 1, "telegram": 1}
    assert x["modes"]["telegram-group"] == "candidate" and x["policies"] == 3
    w = x["workers"]["live"]
    assert x["router"]["enabled"] and x["workers"]["max"] == 3 and len(w) == 1
    assert (w[0]["id"], w[0]["mode"], w[0]["threads"], w[0]["machine"]) == (tid, "silent", ["tg:-42:7"], "this-mac")
    assert x["threads"] == 1
    assert x["machines"][0]["online"] and x["db"]["incoming"] == 5 and x["db"]["bytes"] > 0
