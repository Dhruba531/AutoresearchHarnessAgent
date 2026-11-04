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
