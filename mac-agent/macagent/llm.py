"""Model adapters. The agent keeps history in Anthropic Messages format; each
adapter turns one history into one assistant turn.

ClaudeAdapter (official `anthropic` SDK) is the only one that can see and
drive the screen, via the computer-use toolset (`computer_toolset_20260801`).
Request features, each dropped automatically if the account or model rejects
it, so an unusual account still works:

* thinking display "updates" — Claude's between-step progress notes come back
  as short readable text, which the phone shows live;
* context management — the server clears old tool results (screenshots) as
  the history grows, so long screen sessions stay within limits without the
  client editing history (which would invalidate thinking blocks);
* server-side refusal fallback (`fallbacks: "default"`).

Top-level prompt caching keeps each step from re-paying for the whole history.

OpenAICompatAdapter serves DeepSeek and OpenAI with the native tools only.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

import anthropic
import httpx

COMPUTER_TOOLSET = {"type": "computer_toolset_20260801"}
SCREEN_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1", "claude-opus-5"}

# USD per million tokens: (input, output, cache read)
PRICES = {
    "claude-opus-5-5": (4.0, 20.0, 0.20), "claude-sonnet-5-5": (2.0, 10.0, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01), "claude-fable-5-1": (10.0, 50.0, 0.25), "claude-opus-5": (5.0, 25.0, 0.50),
    "deepseek-chat": (0.28, 0.42, 0.028), "deepseek-reasoner": (0.28, 0.42, 0.028),
    "gpt-4.1": (2.0, 8.0, 0.5), "gpt-4.1-mini": (0.4, 1.6, 0.1),
    "gpt-5.4-mini": (0.75, 4.50, 0.075), "gpt-5.4-nano": (0.20, 1.25, 0.02),
    # Gemini: 3.8 Flash at its standard rate (the lower introductory rate ends 2026-12-31), so the cap errs safe
    "gemini-3.8-flash": (1.50, 7.50, 0.15), "gemini-3.5-flash-lite": (0.30, 2.50, 0.03),
}

FEATURES = {
    "updates": "thinking-display-updates-2026-08-18",
    "context": "context-management-2025-06-27",
    "fallbacks": "server-side-fallback-2026-07-01",
}


class LLMError(RuntimeError):
    pass


def _claude_history(history: list[dict]) -> list[dict]:
    """Drop fields other providers attach to tool calls (Gemini's thought signatures), which Claude rejects —
    a task may have started on another provider before failing over."""
    if not any(isinstance(m.get("content"), list) and any("extra_content" in b for b in m["content"]) for m in history):
        return history
    return [{**m, "content": [{k: v for k, v in b.items() if k != "extra_content"} for b in m["content"]]}
            if isinstance(m.get("content"), list) else m for m in history]


@dataclass
class Turn:
    content: list[dict]
    stop_reason: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cost_usd: float = 0.0
    refusal: str | None = None
    meta: dict = field(default_factory=dict)


def _price(model: str) -> tuple[float, float, float]:
    """Price by id; OpenRouter ids ("anthropic/claude-opus-5.5") map onto the native ones."""
    if model in PRICES:
        return PRICES[model]
    bare = model.split("/", 1)[-1].split(":", 1)[0]
    return PRICES.get(bare) or PRICES.get(bare.replace(".", "-")) or (10.0, 50.0, 1.0)


def cost(model: str, tin: int, tout: int, cread: int = 0, cwrite: int = 0) -> float:
    pin, pout, pread = _price(model)
    return (tin * pin + tout * pout + cread * pread + cwrite * pin * 1.25) / 1_000_000


class ClaudeAdapter:
    provider = "anthropic"

    def __init__(self, api_key: str, model: str, effort: str = "medium", client: anthropic.AsyncAnthropic | None = None,
                 *, screen: bool = True, features: set[str] | None = None, max_tokens: int = 16_000):
        self.model = model
        self.effort = effort
        self.client = client or anthropic.AsyncAnthropic(api_key=api_key or None, timeout=900.0, max_retries=3)
        self.features = set(FEATURES) if features is None else set(features)
        self.can_see_screen = screen and model in SCREEN_MODELS
        self.max_tokens = max_tokens

    def _kwargs(self, system: str, history: list[dict], tools: list[dict]) -> dict:
        kw: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": _claude_history(history),
            "tools": tools + ([COMPUTER_TOOLSET] if self.can_see_screen else []),
            "output_config": {"effort": self.effort},
            "cache_control": {"type": "ephemeral"},
            "betas": [FEATURES[f] for f in sorted(self.features)],
        }
        if "updates" in self.features:
            kw["thinking"] = {"type": "adaptive", "display": "updates"}
        if "context" in self.features:
            kw["context_management"] = {"edits": [{"type": "clear_tool_uses_20250919"}]}
        if "fallbacks" in self.features:
            kw["fallbacks"] = "default"
        if not kw["betas"]:
            kw.pop("betas")
        return kw

    async def step(self, system: str, history: list[dict], tools: list[dict]) -> Turn:
        for _ in range(len(FEATURES) + 1):
            try:
                resp = await self.client.beta.messages.create(**self._kwargs(system, history, tools))
                break
            except anthropic.BadRequestError as exc:
                msg = str(exc).lower()
                hints = {"updates": "display", "context": "context_management", "fallbacks": "fallback"}
                dropped = next((f for f in sorted(self.features) if FEATURES[f] in msg or hints[f] in msg), None)
                if dropped is None:
                    raise LLMError(f"Claude rejected the request: {exc.message}") from exc
                self.features.discard(dropped)
            except anthropic.AuthenticationError as exc:
                raise LLMError("Anthropic rejected the API key — set ANTHROPIC_API_KEY.") from exc
            except anthropic.RateLimitError as exc:
                raise LLMError("Anthropic rate limit reached; the task will need a retry.") from exc
            except anthropic.APIStatusError as exc:
                raise LLMError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc
            except anthropic.APIConnectionError as exc:
                raise LLMError(f"Could not reach Anthropic: {exc}") from exc
        else:
            raise LLMError("Claude rejected every request variant.")

        u = resp.usage
        tin = u.input_tokens or 0
        cread = getattr(u, "cache_read_input_tokens", 0) or 0
        cwrite = getattr(u, "cache_creation_input_tokens", 0) or 0
        tout = u.output_tokens or 0
        model = getattr(resp, "model", self.model) or self.model
        content = [b.model_dump(mode="json", exclude_none=True) for b in resp.content]
        refusal = None
        if resp.stop_reason == "refusal":
            details = getattr(resp, "stop_details", None)
            refusal = getattr(details, "explanation", None) or getattr(details, "category", None) or "declined"
        return Turn(content, resp.stop_reason or "end_turn", model, tin, tout, cread, cwrite,
                    cost(model, tin, tout, cread, cwrite), refusal)


class OpenAICompatAdapter:
    """DeepSeek / OpenAI / OpenRouter chat completions with function calling (no screen)."""

    can_see_screen = False

    def __init__(self, provider: str, api_key: str, model: str, transport: httpx.AsyncBaseTransport | None = None,
                 base_url: str | None = None):
        self.provider, self.model, self.key = provider, model, api_key
        self.base = (base_url or BASE_URLS.get(provider, BASE_URLS["openai"])).rstrip("/")
        m = model.lower()
        self.reasoning = "low" if (provider == "gemini" or m.startswith(("gpt-5", "o3", "o4"))) else ""
        self.transport = transport

    @staticmethod
    def _messages(system: str, history: list[dict]) -> list[dict]:
        out: list[dict] = [{"role": "system", "content": system}]
        for msg in history:
            content = msg["content"]
            if isinstance(content, str):
                out.append({"role": msg["role"], "content": content})
                continue
            if msg["role"] == "assistant":
                text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
                calls = [{"id": b["id"], "type": "function",
                          "function": {"name": b["name"], "arguments": json.dumps(b.get("input", {}))},
                          **({"extra_content": b["extra_content"]} if b.get("extra_content") else {})}
                         for b in content if b.get("type") == "tool_use"]
                m: dict[str, Any] = {"role": "assistant", "content": text or None}
                if calls:
                    m["tool_calls"] = calls
                out.append(m)
                continue
            texts = []
            for b in content:
                if b.get("type") == "tool_result":
                    c = b.get("content", "")
                    if isinstance(c, list):
                        c = "\n".join(x.get("text", "[image omitted]") if x.get("type") == "text" else "[image omitted]" for x in c)
                    out.append({"role": "tool", "tool_call_id": b["tool_use_id"],
                                "content": ("ERROR: " if b.get("is_error") else "") + str(c)})
                elif b.get("type") == "text":
                    texts.append(b["text"])
            if texts:
                out.append({"role": "user", "content": "\n\n".join(texts)})
        return out

    async def step(self, system: str, history: list[dict], tools: list[dict]) -> Turn:
        if self.provider == "local" and "qwen3" in self.model.lower():
            system += "\n/no_think"  # Qwen3 thinks at length by default; a personal agent needs quick answers
        body: dict[str, Any] = {
            "model": self.model,
            "messages": self._messages(system, history),
            "tools": [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                        "parameters": t["input_schema"]}} for t in tools],
        }
        # GPT-5-era models take max_completion_tokens; everyone else max_tokens
        body["max_completion_tokens" if self.provider == "openai" else "max_tokens"] = 8192
        if self.reasoning:
            body["reasoning_effort"] = self.reasoning  # low: answer fast instead of thinking long
        try:
            async with httpx.AsyncClient(timeout=600, transport=self.transport) as http:
                headers = {"Authorization": f"Bearer {self.key}"}
                if self.provider == "openrouter":
                    headers["X-Title"] = "Relay"
                r = await http.post(f"{self.base}/chat/completions", json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise LLMError(f"Could not reach {self.provider}: {exc}") from exc
        if r.status_code == 400 and "reasoning" in r.text and self.reasoning:
            self.reasoning = ""  # this model has no reasoning control: stop sending it
            return await self.step(system.removesuffix("\n/no_think"), history, tools)
        if r.status_code != 200:
            raise LLMError(f"{self.provider} error {r.status_code}: {r.text[:300]}")
        data = r.json()
        msg = (data.get("choices") or [{}])[0].get("message", {})
        content: list[dict] = []
        text = msg.get("content") or ""
        if "<think>" in text:  # local reasoning models (Qwen3, DeepSeek-R1) put their thinking inline
            text = re.sub(r"<think>.*?(</think>|$)", "", text, flags=re.S).strip()
        if text:
            content.append({"type": "text", "text": text})
        for call in msg.get("tool_calls") or []:
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"_unparsed": call["function"].get("arguments")}
            block = {"type": "tool_use", "id": call.get("id") or f"call_{len(content)}", "name": call["function"]["name"],
                     "input": args}
            if call.get("extra_content"):  # Gemini 3's thought signature: must go back with the next turn
                block["extra_content"] = call["extra_content"]
            content.append(block)
        usage = data.get("usage") or {}
        tin, tout = int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))
        hit = int(usage.get("prompt_cache_hit_tokens", 0))
        # OpenRouter reports what the call actually cost; otherwise price it from the table
        if self.provider == "local":
            usd = 0.0  # runs on this Mac: no bill, and it must not eat the daily cap
        elif isinstance(usage.get("cost"), (int, float)):
            usd = usage["cost"]
        else:
            usd = cost(self.model, tin - hit, tout, hit)
        return Turn(content, "tool_use" if msg.get("tool_calls") else "end_turn", self.model, tin - hit, tout, hit, 0,
                    float(usd))


BASE_URLS = {"deepseek": "https://api.deepseek.com", "openai": "https://api.openai.com/v1",
             "openrouter": "https://openrouter.ai/api/v1",
             "gemini": "https://generativelanguage.googleapis.com/v1beta/openai"}


def _compat_key(settings) -> str:
    key = settings.api_key()
    if not key:
        raise LLMError(f"Set {settings.provider.upper()}_API_KEY")
    return key


def make_router_adapter(settings) -> ClaudeAdapter | OpenAICompatAdapter:
    """The light model for the router: no screen, no betas, short outputs."""
    if settings.provider == "anthropic":
        return ClaudeAdapter(settings.anthropic_api_key, settings.router_model, "low", screen=False, features=set(),
                             max_tokens=4000)
    return OpenAICompatAdapter(settings.provider, _compat_key(settings), settings.router_model,
                               base_url=settings.local_base_url if settings.provider == "local" else None)


def make_adapter(settings) -> ClaudeAdapter | OpenAICompatAdapter:
    if settings.provider == "anthropic":
        return ClaudeAdapter(settings.anthropic_api_key, settings.model, settings.effort)
    return OpenAICompatAdapter(settings.provider, _compat_key(settings), settings.model,
                               base_url=settings.local_base_url if settings.provider == "local" else None)


async def keep_warm(settings, every_s: float = 240, transport: httpx.AsyncBaseTransport | None = None) -> None:
    """Ollama unloads an idle model after 5 minutes, and reloading it costs seconds on every first message.
    Ask it to keep Relay's models loaded (an empty generate loads without generating)."""
    if settings.provider != "local" or ":11434" not in settings.local_base_url:
        return
    native = settings.local_base_url.rstrip("/").removesuffix("/v1")
    async with httpx.AsyncClient(timeout=120, transport=transport) as http:
        while True:
            for m in dict.fromkeys([settings.model, settings.router_model]):
                try:
                    await http.post(f"{native}/api/generate", json={"model": m, "keep_alive": "30m"})
                except httpx.HTTPError:
                    pass  # Ollama not running yet; doctor explains
            await asyncio.sleep(every_s)


__all__ = ["keep_warm", "ClaudeAdapter", "OpenAICompatAdapter", "Turn", "LLMError", "make_adapter", "cost", "time"]
