"""Choosing the model from the phone, keeping several keys, and failing over when one runs out."""

import asyncio
import json
import os
import stat

import pytest
from fastapi.testclient import TestClient

from macagent.agent import Agent
from macagent.auth import new_pairing_code
from macagent.config import Settings
from macagent.host.fake import FakeHost
from macagent.llm import LLMError, Turn, _claude_history
from macagent.models import FailoverAdapter, ModelManager
from macagent.server import create_app
from macagent.store import Store

KEYS = ("ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY",
        "RELAY_PROVIDER", "RELAY_MODEL", "RELAY_ROUTER_MODEL")


@pytest.fixture
def clean_env():
    old = {k: os.environ.pop(k, None) for k in KEYS}
    yield
    for k, v in old.items():
        os.environ.pop(k, None)
        if v is not None:
            os.environ[k] = v


def make(tmp_path):
    os.environ["RELAY_PROVIDER"] = "openrouter"
    os.environ["OPENROUTER_API_KEY"] = "sk-or-v1-aaaaaaaaaaaa1111"
    settings = Settings(home=tmp_path / "home")
    store = Store(str(settings.db_path))
    return settings, store, ModelManager(settings, store)


class Fake:
    can_see_screen = False

    def __init__(self, provider, fail=None):
        self.provider, self.fail = provider, fail

    async def step(self, system, history, tools):
        if self.fail:
            raise LLMError(self.fail)
        return Turn([{"type": "text", "text": f"hi from {self.provider}"}], "end_turn", "m", cost_usd=0.002)


def test_keys_are_saved_privately_and_never_shown(tmp_path, clean_env):
    settings, store, m = make(tmp_path)
    m.set_key("gemini", "AIzaSyD-secret-gemini-key-9876")
    env = (settings.home / "env").read_text()
    assert "GEMINI_API_KEY=AIzaSyD-secret-gemini-key-9876" in env
    assert stat.S_IMODE(os.stat(settings.home / "env").st_mode) == 0o600
    status = m.status()
    assert "secret" not in json.dumps(status)  # only the last four characters ever leave the Mac
    gem = next(p for p in status["providers"] if p["id"] == "gemini")
    assert gem["has_key"] and gem["key_last4"] == "9876" and gem["model"] == "gemini-3.8-flash"
    with pytest.raises(ValueError):
        m.set_key("gemini", "short")
    m.remove_key("gemini")
    assert "GEMINI_API_KEY" not in (settings.home / "env").read_text() and not m.has_key("gemini")


def test_choice_switches_now_and_survives_a_restart(tmp_path, clean_env):
    settings, store, m = make(tmp_path)
    assert (settings.provider, settings.model) == ("openrouter", "anthropic/claude-opus-5.5")
    with pytest.raises(ValueError):
        m.use("gemini")  # no key yet
    m.set_key("gemini", "AIzaSyD-secret-gemini-key-9876")
    m.use("gemini", router_model="gemini-3.8-flash")
    assert (settings.provider, settings.model, settings.router_model) == ("gemini", "gemini-3.8-flash", "gemini-3.8-flash")
    fresh = Settings(home=settings.home)  # a restart: the env file still says openrouter
    m2 = ModelManager(fresh, store)
    m2.apply_saved()
    assert (fresh.provider, fresh.router_model) == ("gemini", "gemini-3.8-flash")
    assert m2.models_for("openrouter") == ("anthropic/claude-opus-5.5", "anthropic/claude-haiku-5.5")


def test_failover_when_a_key_runs_out(tmp_path, clean_env):
    settings, store, m = make(tmp_path)
    m.set_key("gemini", "AIzaSyD-secret-gemini-key-9876")
    m.set_fallback(order=["openrouter", "gemini", "deepseek"])
    notes = []
    m.listeners.append(notes.append)
    m.make = lambda role: Fake(settings.provider, fail="openrouter error 402: insufficient credits"
                               if settings.provider == "openrouter" else None)
    turn = asyncio.run(FailoverAdapter(m, "main").step("S", [{"role": "user", "content": "x"}], []))
    assert turn.content[0]["text"] == "hi from gemini" and settings.provider == "gemini"
    assert notes and "switched to Gemini" in notes[0]
    s = {p["id"]: p for p in m.status()["providers"]}
    assert s["openrouter"]["resting"] and "402" in s["openrouter"]["last_error"]
    assert s["gemini"]["today_calls"] == 1 and s["gemini"]["today_usd"] == 0.002


def test_no_failover_for_ordinary_errors_or_when_switched_off(tmp_path, clean_env):
    settings, store, m = make(tmp_path)
    m.set_key("gemini", "AIzaSyD-secret-gemini-key-9876")
    m.make = lambda role: Fake(settings.provider, fail="openrouter error 400: tools[3] schema invalid")
    with pytest.raises(LLMError):
        asyncio.run(FailoverAdapter(m, "main").step("S", [], []))
    assert settings.provider == "openrouter"  # a bad request is not a spent key: switching would not help
    m.set_fallback(auto=False)
    m.make = lambda role: Fake(settings.provider, fail="openrouter error 429: rate limit")
    with pytest.raises(LLMError):
        asyncio.run(FailoverAdapter(m, "fast").step("S", [], []))
    assert settings.provider == "openrouter"


def test_follows_a_switch_made_mid_task(tmp_path, clean_env):
    settings, store, m = make(tmp_path)
    m.set_key("deepseek", "sk-test-deepseek-not-a-real-key")
    m.make = lambda role: Fake(settings.provider)
    ad = FailoverAdapter(m, "main")
    assert asyncio.run(ad.step("S", [], [])).content[0]["text"] == "hi from openrouter"
    m.use("deepseek")  # tapped in the app while the task runs
    assert asyncio.run(ad.step("S", [], [])).content[0]["text"] == "hi from deepseek"


def test_claude_drops_other_providers_fields():
    h = [{"role": "assistant", "content": [{"type": "tool_use", "id": "c", "name": "x", "input": {},
                                            "extra_content": {"google": {"thought_signature": "s"}}}]}]
    assert "extra_content" not in _claude_history(h)[0]["content"][0]
    plain = [{"role": "user", "content": "hi"}]
    assert _claude_history(plain) is plain


def test_models_api(tmp_path, clean_env):
    settings, store, m = make(tmp_path)

    async def fake_test(provider, transport=None):
        return True, "Key works."

    m.test_key = fake_test
    agent = Agent(settings, store, FakeHost(), lambda: FailoverAdapter(m, "main"))
    app = create_app(settings, store, agent, models=m)
    with TestClient(app) as c:
        assert c.get("/api/models").status_code == 401
        token = c.post("/api/pair", json={"code": new_pairing_code(store), "name": "p"}).json()["token"]
        c.headers.update({"Authorization": f"Bearer {token}"})
        r = c.put("/api/models/keys/gemini", json={"key": "AIzaSyD-secret-gemini-key-9876"}).json()
        assert r["test"]["ok"] and "secret" not in json.dumps(r)
        assert c.post("/api/models/use", json={"provider": "nope"}).status_code == 400
        r = c.post("/api/models/use", json={"provider": "gemini"}).json()
        assert r["active"]["provider"] == "gemini" and c.get("/api/state").json()["active_model"]["label"] == "Gemini"
        assert c.delete("/api/models/keys/gemini").status_code == 409  # the key in use
        r = c.put("/api/models/fallback", json={"order": ["deepseek", "gemini"], "auto": False}).json()
        assert r["order"][:2] == ["deepseek", "gemini"] and r["auto"] is False
        assert c.delete("/api/models/keys/openrouter").status_code == 200


def test_local_counts_only_when_ollama_answers(tmp_path, clean_env):
    settings, store, m = make(tmp_path)
    settings.local_base_url = "http://127.0.0.1:9/v1"  # nothing listens here
    assert not m.has_key("local")
    with pytest.raises(ValueError):
        m.use("local")
    local = next(p for p in m.status()["providers"] if p["id"] == "local")
    assert local["offline"] and not local["has_key"]


def test_error_reasons_are_readable():
    import httpx
    from macagent.models import _reason

    r = httpx.Response(400, json={"error": {"code": 400, "message": "Please pass a valid API key", "status": "INVALID"}})
    assert _reason(r) == "Please pass a valid API key"
    assert _reason(httpx.Response(401, text="nope")) == "nope"


@pytest.mark.asyncio
async def test_telegram_model_command(tmp_path, clean_env):
    import httpx

    from macagent.telegram import TelegramBot

    settings, store, m = make(tmp_path)
    m.set_key("gemini", "AIzaSyD-secret-gemini-key-9876")
    agent = Agent(settings, store, FakeHost(), lambda: None)
    calls = []

    def handler(req):
        calls.append((req.url.path.rsplit("/", 1)[-1], json.loads(req.content or b"{}")))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

    bot = TelegramBot("123:abc", agent, store, transport=httpx.MockTransport(handler), models=m)
    store.x("INSERT INTO telegram_chats(chat_id, name, created_at) VALUES (5, 'me', 0)")
    await bot.handle({"message": {"chat": {"id": 5, "type": "private"}, "text": "/model", "from": {"first_name": "D"}}})
    menu = next(b for n, b in calls if n == "sendMessage")
    labels = [r[0]["text"] for r in menu["reply_markup"]["inline_keyboard"]]
    assert labels[0].startswith("Gemini") and any(t.startswith("✓ OpenRouter") for t in labels)
    assert "secret" not in json.dumps(menu)
    await bot.handle({"callback_query": {"id": "q", "data": "m:gemini",
                                         "message": {"chat": {"id": 5}, "message_id": 7}}})
    assert settings.provider == "gemini"
    assert any(n == "editMessageText" and "Now using Gemini" in b["text"] for n, b in calls)


@pytest.mark.asyncio
async def test_telegram_lists_its_commands(tmp_path, clean_env):
    import httpx

    from macagent.telegram import TelegramBot

    settings, store, m = make(tmp_path)
    calls = []

    def handler(req):
        name = req.url.path.rsplit("/", 1)[-1]
        calls.append((name, json.loads(req.content or b"{}")))
        if name == "getUpdates":
            raise httpx.ConnectError("stop")
        return httpx.Response(200, json={"ok": True, "result": True})

    bot = TelegramBot("123:abc", Agent(settings, store, FakeHost(), lambda: None), store,
                      transport=httpx.MockTransport(handler), models=m)
    task = asyncio.create_task(bot.run())
    for _ in range(100):
        if any(n == "getUpdates" for n, _ in calls):
            break
        await asyncio.sleep(0.01)
    task.cancel()
    cmds = next(b for n, b in calls if n == "setMyCommands")["commands"]
    assert [c["command"] for c in cmds][:2] == ["app", "model"]
    assert {"command": "model", "description": "Which model runs; tap to switch"} in cmds


@pytest.mark.asyncio
async def test_telegram_app_link(tmp_path, clean_env):
    import httpx

    from macagent.auth import redeem_pairing_code
    from macagent.telegram import TelegramBot

    settings, store, m = make(tmp_path)
    calls = []

    def handler(req):
        calls.append((req.url.path.rsplit("/", 1)[-1], json.loads(req.content or b"{}")))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    agent = Agent(settings, store, FakeHost(), lambda: None)
    t = httpx.MockTransport(handler)
    store.x("INSERT INTO telegram_chats(chat_id, name, created_at) VALUES (5, 'me', 0), (-9, 'team', 0)")
    msg = lambda chat, kind="private": {"message": {"chat": {"id": chat, "type": kind}, "text": "/app", "from": {}}}

    no_url = TelegramBot("1:a", agent, store, transport=t)
    await no_url.handle(msg(5))
    assert "needs Tailscale" in calls[-1][1]["text"]

    bot = TelegramBot("1:a", agent, store, transport=t, app_url="https://mini.tail1.ts.net/", public=True)
    await bot.handle(msg(-9, "group"))
    assert "private chat" in calls[-1][1]["text"] and "pair?code" not in calls[-1][1]["text"]
    await bot.handle(msg(5))
    body = calls[-1][1]
    url = body["reply_markup"]["inline_keyboard"][0][0]["web_app"]["url"]
    assert url.startswith("https://mini.tail1.ts.net/pair?code=") and "any browser" in body["text"]
    code = url.split("code=")[1]
    assert redeem_pairing_code(store, code, "uni browser") and not redeem_pairing_code(store, code, "again")  # once
    await bot.handle(msg(42))  # a stranger gets nothing
    assert calls[-1][1].get("chat_id") != 42


def test_names_people_say(tmp_path, clean_env):
    settings, store, m = make(tmp_path)  # OpenRouter key
    m.set_key("gemini", "AIzaSyD-secret-gemini-key-9876")
    m.set_key("openai", "sk-proj-test-openai-not-a-real-key")
    assert m.resolve("gemini") == ("gemini", None)
    assert m.resolve("use google please") == ("gemini", None)
    assert m.resolve("gpt nano") == ("openai", "gpt-5.4-nano")
    assert m.resolve("gpt-5.4-mini") == ("openai", "gpt-5.4-mini")
    assert m.resolve("chatgpt") == ("openai", None)
    assert m.resolve("haiku") == ("openrouter", "anthropic/claude-haiku-5.5")  # Claude via OpenRouter: no Anthropic key
    assert m.resolve("claude") == ("openrouter", None)
    assert m.resolve("flash lite") == ("gemini", "gemini-3.5-flash-lite")
    assert m.resolve("deepseek") is None  # no key for it
    assert m.resolve("pizza") is None


@pytest.mark.asyncio
async def test_switch_by_name_in_telegram_and_by_asking(tmp_path, clean_env):
    import httpx

    from macagent.router import Router
    from macagent.telegram import TelegramBot
    from tests.mock_claude import MockClaude, tool

    settings, store, m = make(tmp_path)
    m.set_key("gemini", "AIzaSyD-secret-gemini-key-9876")
    agent = Agent(settings, store, FakeHost(), lambda: None)
    calls = []

    def handler(req):
        calls.append((req.url.path.rsplit("/", 1)[-1], json.loads(req.content or b"{}")))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    bot = TelegramBot("1:a", agent, store, transport=httpx.MockTransport(handler), models=m)
    store.x("INSERT INTO telegram_chats(chat_id, name, created_at) VALUES (5, 'me', 0)")
    await bot.handle({"message": {"chat": {"id": 5, "type": "private"}, "text": "/model gemini flash lite", "from": {}}})
    assert (settings.provider, settings.model) == ("gemini", "gemini-3.5-flash-lite")
    assert "Now using Gemini" in calls[-1][1]["text"]
    await bot.handle({"message": {"chat": {"id": 5, "type": "private"}, "text": "/model pizza", "from": {}}})
    assert "don't know" in calls[-1][1]["text"] and settings.provider == "gemini"

    # "go back to claude", said in chat: the router calls switch_model
    light = MockClaude(lambda b: ([tool("s", "switch_model", {"provider": "openrouter"}),
                                   tool("r", "reply", {"text": "Back on Claude."})], "tool_use"))
    router = Router(settings, store, agent, light.router_adapter, None, models=m)
    from macagent.router import WebChannel
    router.add_channel(WebChannel(store, agent))
    router.human("web", "go back to claude", "me")
    await router.handle([store.incoming(e["id"]) for e in store.incoming_list(1)])
    assert settings.provider == "openrouter"
    assert "can switch to:" in light.requests[0]["messages"][0]["content"]
    # the same request arriving in an email is refused
    m.use("gemini")
    router.submit({"kind": "trigger", "source": "trigger:1", "text": "switch to claude", "sender": "x@y",
                   "trigger": {"id": 1, "name": "m", "kind": "webhook", "instruction": "triage"}})
    await router.handle([store.incoming(store.incoming_list(1)[0]["id"])])
    assert settings.provider == "gemini"
