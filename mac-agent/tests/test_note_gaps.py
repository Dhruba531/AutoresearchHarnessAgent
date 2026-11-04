"""The rest of the design note: per-channel replies (§9), handing an event back (§6), sub-workers (§4),
background jobs across a restart (§10), and GitHub as an event source."""

import asyncio
import hashlib
import hmac
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from macagent.agent import Agent
from macagent.auth import new_pairing_code
from macagent.config import Settings
from macagent.filters import Filter
from macagent.host.fake import FakeHost
from macagent.router import Router, WebChannel
from macagent.server import create_app
from macagent.store import Store
from macagent.tools import classify
from macagent.triggers import Triggers, github_item
from tests.mock_claude import MockClaude, tool


def build(tmp_path, worker, light, policy="balanced"):
    settings = Settings(home=tmp_path / "home", policy=policy, approval_timeout_s=10, router_batch_ms=200,
                        router_max_wait_ms=800)
    store = Store(str(settings.db_path))
    wmock, lmock = MockClaude(worker), MockClaude(light)
    agent = Agent(settings, store, FakeHost(), wmock.adapter, light_factory=lmock.router_adapter)
    router = Router(settings, store, agent, lmock.router_adapter, None, Filter(store, lmock.router_adapter))
    router.add_channel(WebChannel(store, agent))
    return settings, store, agent, router, wmock, lmock


async def until(pred, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met")


def prompt_of(body):
    return json.dumps(body["messages"][0]["content"])


def relay_texts(store):
    return [m["text"] for m in store.chat("web") if m["role"] == "relay"]


# §9 — one finding, each channel in its own form
@pytest.mark.asyncio
async def test_outbox_public_draft_and_email_after_approval(tmp_path):
    def light(body):
        if "router of Relay" in body["system"]:
            return [tool("p", "outbox", {"channel": "public", "text": "Fixed the Android login bug in v2.3.1."}),
                    tool("e", "outbox", {"channel": "email", "to": "sam@example.com", "subject": "Login bug fixed",
                                         "text": "Hi Sam,\n\nThe Android login bug is fixed in 2.3.1.\n\nThanks,\nD"}),
                    tool("bad", "outbox", {"channel": "email", "to": "nobody", "text": "x"})], "tool_use"
        if "Send exactly this email" in prompt_of(body):  # the quick worker that sends it
            n = sum(1 for m in body["messages"] if m["role"] == "assistant")
            if n == 0:
                return [tool("m", "mail_send", {"to": "sam@example.com", "subject": "Login bug fixed",
                                                "body": "Hi Sam, ..."})], "tool_use"
            return [{"type": "text", "text": "Sent."}], "end_turn"
        return [{"type": "text", "text": "ok"}], "end_turn"

    settings, store, agent, router, wmock, lmock = build(tmp_path, lambda b: ([], "end_turn"), light)
    agent.start()
    router.human("web", "tell everyone the login bug is fixed", "me")
    await router.handle([store.incoming(store.incoming_list(1)[0]["id"])])
    texts = relay_texts(store)
    assert any(t.startswith("Draft for a public post") and "v2.3.1" in t for t in texts)
    assert any("an email needs to" in t for t in texts)  # the malformed one is refused, not sent
    task = next(t for t in store.tasks() if t["title"].startswith("Email sam@example.com"))
    await until(lambda: store.pending_approvals())  # mail_send is a send: the user decides
    appr = store.pending_approvals()[0]
    assert appr["tool"] == "mail_send" and appr["task_id"] == task["id"]
    agent.decide(appr["id"], False, None, "test")
    await until(lambda: store.task(task["id"])["status"] in {"done", "failed"})
    await agent.stop()


def test_github_post_needs_approval_and_uses_the_token(tmp_path):
    tier, summary = classify("github_post", {"repo": "o/r", "number": 7, "body": "## Summary"}, None, Settings(home=tmp_path))
    assert tier == "send" and "o/r#7" in summary
    settings = Settings(home=tmp_path / "home")
    settings.github_token = "ghp_test_not_real"
    agent = Agent(settings, Store(str(settings.db_path)), FakeHost(), lambda: None)
    seen = {}

    def handler(req):
        seen.update(url=str(req.url), auth=req.headers["authorization"], body=json.loads(req.content), method=req.method)
        return httpx.Response(201, json={"html_url": "https://github.com/o/r/issues/7#issuecomment-1"})

    agent.github_transport = httpx.MockTransport(handler)
    res = asyncio.run(agent._github_post({"repo": "o/r", "number": 7, "body": "## Summary\nFixed."}))
    assert seen["url"] == "https://api.github.com/repos/o/r/issues/7/comments" and seen["method"] == "POST"
    assert seen["auth"] == "Bearer ghp_test_not_real" and "issuecomment-1" in res.content
    asyncio.run(agent._github_post({"repo": "o/r", "number": 8, "body": "desc", "where": "pr_description"}))
    assert seen["url"].endswith("/pulls/8") and seen["method"] == "PATCH"


# §6 — a worker hands an event back; it is routed again, not to that worker
@pytest.mark.asyncio
async def test_hand_back(tmp_path):
    def worker(body):
        n = sum(1 for m in body["messages"] if m["role"] == "assistant")
        if n == 0:
            return [tool("h", "hand_back", {"event_id": "E1", "reason": "this is about billing, not the login bug"})], "tool_use"
        return [{"type": "text", "text": "Handed it back."}], "end_turn"

    settings, store, agent, router, wmock, lmock = build(tmp_path, worker, lambda b: ([], "end_turn"))
    agent.start()
    ev = router.human("web", "my invoice is wrong", "me", thread="tg:1:9")
    tid = agent.submit("look into the login bug", origin="web")
    store.subscribe("tg:1:9", tid)
    await until(lambda: store.task(tid)["status"] == "done")
    await agent.stop()
    assert tid not in store.subscribers("tg:1:9", live_only=False)
    again = next(e for e in store.incoming_list() if e["payload"].get("_handed_back_by"))
    assert again["id"] != 1 and again["text"] == "my invoice is wrong" and again["thread"] == "tg:1:9"
    assert again["payload"]["_handed_back_by"] == [tid]
    assert ("W%d" % tid, "handed back") in [(v["by"], v["verdict"]) for v in store.incoming(1)["verdicts"]]
    assert f"handed back by W{tid}" in router.render([again]) and "not to them" in router.render([again])


# §4 — a worker starts sub-workers; their results come back to it, not to the user
@pytest.mark.asyncio
async def test_delegate_to_sub_workers(tmp_path):
    def worker(body):
        p, n = prompt_of(body), sum(1 for m in body["messages"] if m["role"] == "assistant")
        if "CHILD" in p:
            return [{"type": "text", "text": "child found 3 typos"}], "end_turn"
        if n == 0:
            return [tool("d", "delegate", {"instructions": "CHILD: proofread chapter 2", "title": "Proofread ch2"})], "tool_use"
        if n == 1:
            return [{"type": "text", "text": "Waiting for the sub-worker."}], "end_turn"
        assert "child found 3 typos" in json.dumps(body["messages"])
        return [{"type": "text", "text": "All done: 3 typos fixed."}], "end_turn"

    settings, store, agent, router, wmock, lmock = build(tmp_path, worker, lambda b: ([], "end_turn"))
    agent.start()
    parent = agent.submit("PARENT: check my thesis", origin="web")
    await until(lambda: store.task(parent)["status"] == "done", 15)
    await agent.stop()
    child = next(t for t in store.tasks() if t["origin"] == f"task:{parent}")
    assert child["status"] == "done" and store.task(parent)["result"] == "All done: 3 typos fixed."
    assert not any(e["kind"] == "task_finished" and e["payload"].get("_task_id") == child["id"]
                   for e in store.incoming_list())  # the user hears from the parent only
    from macagent.agent import ToolError
    with pytest.raises(ToolError):  # one level deep
        await agent._worker_tool(child, "delegate", {"instructions": "x"}, FakeHost())


# §10 — background jobs lost to a restart are named when the worker resumes
def test_resumed_worker_hears_which_jobs_were_lost(tmp_path):
    settings = Settings(home=tmp_path / "home")
    store = Store(str(settings.db_path))
    tid = store.create_task("build it", origin="web")
    store.update_task(tid, status="running")
    store.put("jobs_live", {"4": {"task": tid, "command": "make test", "started": time.time()},
                            "5": {"task": 999, "command": "other", "started": time.time()}})
    agent = Agent(settings, store, FakeHost(), lambda: None)
    agent.recover()
    note = agent.inbox[tid][0]
    assert "[J4] make test" in note and "other" not in note and store.get("jobs_live") == {}


# GitHub as an event source
def sign(secret, body):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_github_events_are_signed_and_threaded(tmp_path):
    settings = Settings(home=tmp_path / "home")
    store = Store(str(settings.db_path))
    agent = Agent(settings, store, FakeHost(), lambda: None)
    triggers = Triggers(store, FakeHost(), settings)
    router = Router(settings, store, agent, lambda: None, triggers)
    triggers.router = router
    t = triggers.create("app repo", "github", {}, "fix CI failures, triage new issues")
    app = create_app(settings, store, agent, router=router, triggers=triggers)
    comment = {"action": "created", "repository": {"full_name": "dhruba/app"}, "sender": {"login": "priya"},
               "issue": {"number": 42, "title": "Login fails on Android", "html_url": "https://github.com/dhruba/app/issues/42"},
               "comment": {"body": "Still broken on 2.3.0"}}
    body = json.dumps(comment).encode()
    with TestClient(app) as c:
        h = {"X-GitHub-Event": "issue_comment", "Content-Type": "application/json"}
        assert c.post(f"/api/hooks/{t['id']}", content=body, headers={**h, "X-Hub-Signature-256": "sha256=bad"}).status_code == 404
        assert c.post(f"/api/hooks/{t['id']}", content=body, headers=h).status_code == 404  # unsigned
        ok = c.post(f"/api/hooks/{t['id']}", content=body, headers={**h, "X-Hub-Signature-256": sign(t["secret"], body)})
        assert ok.status_code == 200
        ping = json.dumps({"zen": "hi", "hook_id": 1}).encode()
        assert c.post(f"/api/hooks/{t['id']}", content=ping, headers={"X-GitHub-Event": "ping",
                                                                     "X-Hub-Signature-256": sign(t["secret"], ping)}).status_code == 200
        token = c.post("/api/pair", json={"code": new_pairing_code(store), "name": "p"}).json()["token"]
        assert c.get("/api/triggers", headers={"Authorization": f"Bearer {token}"}).json()[0]["hook_path"] == f"/api/hooks/{t['id']}"
    evs = store.incoming_list()
    assert len(evs) == 1  # the ping made no event
    ev = evs[0]
    assert ev["thread"] == "gh:dhruba/app#42" and ev["sender"] == "@priya" and "Still broken" in ev["text"]
    assert ev["payload"]["_github"]["number"] == 42 and ev["payload"]["_trigger_kind"] == "github"
    assert Filter(store).code(ev)[0] == "candidate"  # GitHub is noisy: the model filter judges it


def test_github_item_shapes():
    ci = github_item("workflow_run", {"action": "completed", "repository": {"full_name": "o/r"},
                                      "workflow_run": {"conclusion": "failure", "name": "tests", "head_branch": "main",
                                                       "html_url": "u", "display_title": "fix"}})
    assert ci["thread"] == "gh:o/r:ci:main" and "CI failed" in ci["text"]
    assert github_item("workflow_run", {"action": "completed", "workflow_run": {"conclusion": "success"}}) is None
    pr = github_item("pull_request", {"action": "closed", "repository": {"full_name": "o/r"}, "sender": {"login": "a"},
                                      "pull_request": {"number": 5, "title": "Fix", "merged": True}})
    assert pr["thread"] == "gh:o/r#5" and "(merged)" in pr["text"]
    assert github_item("watch", {"action": "started"}) is None
