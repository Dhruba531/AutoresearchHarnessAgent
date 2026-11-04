import asyncio
import json

import httpx

from macagent.llm import OpenAICompatAdapter, cost
from macagent.tools import SPECS


def test_deepseek_adapter_translates_history_and_tool_calls():
    seen = {}

    def handler(req):
        seen["url"] = str(req.url)
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "Checking.", "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "calendar_events", "arguments": "{\"days\": 2}"}}]}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 50, "prompt_cache_hit_tokens": 400}})

    ad = OpenAICompatAdapter("deepseek", "k", "deepseek-chat", transport=httpx.MockTransport(handler))
    history = [
        {"role": "user", "content": "what's on today"},
        {"role": "assistant", "content": [{"type": "text", "text": "Looking."},
                                          {"type": "tool_use", "id": "t1", "name": "screenshot", "input": {}, "toolset_name": "computer"}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "toolset_name": "computer",
                                      "content": [{"type": "image", "source": {"type": "base64", "data": "x"}}]},
                                     {"type": "text", "text": "also check email"}]},
    ]
    turn = asyncio.run(ad.step("SYS", history, SPECS))
    msgs = seen["body"]["messages"]
    assert seen["url"] == "https://api.deepseek.com/chat/completions"
    assert msgs[0] == {"role": "system", "content": "SYS"}
    assert msgs[2]["tool_calls"][0]["function"]["name"] == "screenshot"
    assert msgs[3] == {"role": "tool", "tool_call_id": "t1", "content": "[image omitted]"}
    assert msgs[4] == {"role": "user", "content": "also check email"}
    assert {t["function"]["name"] for t in seen["body"]["tools"]} >= {"run_shell", "mail_send", "ask_user"}
    assert turn.stop_reason == "tool_use"
    assert turn.content[1] == {"type": "tool_use", "id": "call_1", "name": "calendar_events", "input": {"days": 2}}
    assert turn.cache_read == 400 and abs(turn.cost_usd - cost("deepseek-chat", 600, 50, 400)) < 1e-12


def test_openrouter_provider():
    from macagent.config import Settings
    from macagent.llm import make_adapter, make_router_adapter

    seen = {}

    def handler(req):
        seen["url"], seen["headers"], seen["body"] = str(req.url), req.headers, json.loads(req.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "Done."}}],
                                         "usage": {"prompt_tokens": 100, "completion_tokens": 10, "cost": 0.0123}})

    import os, tempfile
    env = {"RELAY_PROVIDER": "openrouter", "OPENROUTER_API_KEY": "sk-or-v1-test", "RELAY_MODEL": "", "RELAY_ROUTER_MODEL": ""}
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        s = Settings(home=__import__("pathlib").Path(tempfile.mkdtemp()))
    finally:
        for k, v in old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
    assert (s.model, s.router_model, s.api_key()) == ("anthropic/claude-opus-5.5", "anthropic/claude-haiku-5.5", "sk-or-v1-test")
    assert not s.can_see_screen
    worker, light = make_adapter(s), make_router_adapter(s)
    assert (worker.model, light.model) == ("anthropic/claude-opus-5.5", "anthropic/claude-haiku-5.5")
    worker.transport = httpx.MockTransport(handler)
    turn = asyncio.run(worker.step("SYS", [{"role": "user", "content": "hi"}], SPECS))
    assert seen["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert seen["headers"]["authorization"] == "Bearer sk-or-v1-test" and seen["headers"]["x-title"] == "Relay"
    assert "temperature" not in seen["body"]  # Haiku 5.5 rejects non-default sampling settings
    assert turn.cost_usd == 0.0123  # OpenRouter's reported cost wins over the table
    # ids map onto the native price table when OpenRouter doesn't report a cost
    assert cost("anthropic/claude-opus-5.5", 1000, 100) == cost("claude-opus-5-5", 1000, 100)
    assert cost("deepseek/deepseek-chat", 1000, 100) == cost("deepseek-chat", 1000, 100)
