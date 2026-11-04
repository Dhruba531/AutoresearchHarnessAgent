"""A scripted Anthropic Messages endpoint behind the real SDK (httpx2 MockTransport).

Each test gives a list of turns; each turn is a function of the request body
returning (content blocks, stop_reason). Requests are recorded so tests can
assert on exactly what the agent sent, e.g. toolset_name echoes.
"""

from __future__ import annotations

import json

import anthropic
import httpx2

from macagent.llm import ClaudeAdapter


class MockClaude:
    def __init__(self, turns, reject_features: set[str] | None = None):
        self.turns = list(turns)
        self.requests: list[dict] = []
        self.reject = reject_features or set()
        transport = httpx2.MockTransport(self.handle)
        self.client = anthropic.AsyncAnthropic(api_key="test",
                                               http_client=anthropic.DefaultAsyncHttpxClient(transport=transport))

    def handle(self, request):
        body = json.loads(request.content)
        beta = request.headers.get("anthropic-beta", "")
        for feat in self.reject:
            if feat in beta or feat in json.dumps(body.get("thinking", {})):
                return httpx2.Response(400, json={"type": "error", "error": {
                    "type": "invalid_request_error", "message": f"unsupported beta: {feat}"}})
        self.requests.append(body)
        if not self.turns:
            content, stop = [{"type": "text", "text": "Done."}], "end_turn"
        else:
            content, stop = self.turns.pop(0)(body)
        return httpx2.Response(200, json={
            "id": f"msg_{len(self.requests)}", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": content, "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": 1200, "output_tokens": 300, "cache_read_input_tokens": 0,
                      "cache_creation_input_tokens": 0}})

    def adapter(self):
        return ClaudeAdapter("test", "claude-opus-5-5", "medium", client=self.client)


def tool(id_, name, inp, toolset=None):
    b = {"type": "tool_use", "id": id_, "name": name, "input": inp}
    if toolset:
        b["toolset_name"] = toolset
    return b
