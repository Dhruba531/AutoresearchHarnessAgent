"""Model providers behind one small interface, with per-call cost accounting.

Every call returns an `LLMResult` carrying the token counts and the dollar cost
computed from the price table below, so runs and campaigns can enforce their
budgets on real spend rather than guesses.

Claude goes through the official `anthropic` SDK. OpenAI and Gemini are plain
HTTPS calls; they are alternates the console lets operators configure.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

import anthropic
import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .db import ProviderKey, User
from .security import decrypt_secret

# USD per million tokens (input, output). Anthropic first-party list prices;
# OpenAI list prices. Unknown models fall back to a conservative rate.
PRICES: dict[str, tuple[float, float]] = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-fable-5": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-haiku-4-5": (1.0, 5.0),
    "gpt-4.1": (2.0, 8.0),
    "gpt-4.1-mini": (0.4, 1.6),
    "gpt-4o": (2.5, 10.0),
    "gpt-4o-mini": (0.15, 0.6),
    "o4-mini": (1.1, 4.4),
    # DeepSeek list prices (cache-miss input). Override with AGENTLAB_PRICE_<MODEL>="in,out" if they change.
    "deepseek-chat": (0.28, 0.42),
    "deepseek-reasoner": (0.28, 0.42),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-pro": (1.25, 10.0),
}
FALLBACK_PRICE = (10.0, 50.0)
IMAGE_PRICE_USD = 0.04  # per generated figure (gemini-2.5-flash-image list price ≈ $0.039)

DEFAULT_MODELS = {
    "anthropic": "claude-opus-5-5",
    "openai": "gpt-4.1",
    "deepseek": "deepseek-reasoner",
    "gemini": "gemini-2.5-flash-image",
}

# Models that take `output_config.effort`, and the subset that accepts the
# server-side refusal fallback (`fallbacks: "default"`).
_EFFORT_MODELS = {
    "claude-fable-5-1", "claude-fable-5", "claude-opus-5-5", "claude-opus-5", "claude-opus-4-8",
    "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-5-5", "claude-sonnet-5", "claude-sonnet-4-6",
    "claude-haiku-5-5",
}
_FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"

TEXT_PROVIDERS = ("anthropic", "openai", "deepseek")


def price_for(model: str) -> tuple[float, float]:
    override = os.environ.get("AGENTLAB_PRICE_" + re.sub(r"[^A-Z0-9]", "_", model.upper()))
    if override:
        try:
            pin, pout = (float(x) for x in override.split(","))
            return pin, pout
        except ValueError:
            pass
    if model in PRICES:
        return PRICES[model]
    for name, price in PRICES.items():  # tolerate suffixed ids, e.g. "gpt-4.1-2025-04-14"
        if model.startswith(name):
            return price
    return FALLBACK_PRICE


def cost_of(model: str, input_tokens: int, output_tokens: int) -> float:
    pin, pout = price_for(model)
    return (input_tokens * pin + output_tokens * pout) / 1_000_000


class LLMError(RuntimeError):
    """A provider call failed in a way the caller should surface."""


class NoProviderError(LLMError):
    pass


@dataclass
class LLMResult:
    text: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    model: str
    provider: str
    duration_ms: int


class TextClient(Protocol):
    provider: str
    model: str

    async def complete(
        self, system: str, prompt: str, *, max_tokens: int = 8000, effort: str = "medium"
    ) -> LLMResult: ...


class AnthropicClient:
    provider = "anthropic"

    def __init__(self, api_key: str, model: str):
        self.model = model or DEFAULT_MODELS["anthropic"]
        self._client = anthropic.AsyncAnthropic(api_key=api_key, timeout=600.0, max_retries=2)
        self._use_fallbacks = self.model in _FALLBACK_MODELS

    async def complete(self, system: str, prompt: str, *, max_tokens: int = 8000, effort: str = "medium") -> LLMResult:
        started = time.monotonic()
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        }
        if self.model in _EFFORT_MODELS:
            kwargs["output_config"] = {"effort": effort}
        try:
            if self._use_fallbacks:
                try:
                    resp = await self._client.beta.messages.create(
                        betas=[FALLBACK_BETA], fallbacks="default", **kwargs
                    )
                except anthropic.BadRequestError as exc:
                    # Accounts or models without the fallback beta: retry plain.
                    if "fallback" not in str(exc).lower():
                        raise
                    self._use_fallbacks = False
                    resp = await self._client.messages.create(**kwargs)
            else:
                resp = await self._client.messages.create(**kwargs)
        except anthropic.AuthenticationError as exc:
            raise LLMError("Anthropic rejected the API key (401). Update it under Provider keys.") from exc
        except anthropic.RateLimitError as exc:
            raise LLMError("Anthropic rate limit hit (429); try again shortly.") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"Could not reach the Anthropic API: {exc}") from exc

        usage = resp.usage
        in_tok = (usage.input_tokens or 0) + (getattr(usage, "cache_creation_input_tokens", 0) or 0) + (
            getattr(usage, "cache_read_input_tokens", 0) or 0
        )
        out_tok = usage.output_tokens or 0
        served = getattr(resp, "model", self.model) or self.model
        cost = cost_of(served, in_tok, out_tok)
        if resp.stop_reason == "refusal":
            details = getattr(resp, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise LLMError(f"The model declined this request (refusal{f': {category}' if category else ''}).")
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        return LLMResult(text, in_tok, out_tok, cost, served, self.provider, int((time.monotonic() - started) * 1000))


class OpenAIClient:
    """OpenAI chat completions; also any OpenAI-compatible API (DeepSeek)."""

    provider = "openai"
    base_url = "https://api.openai.com/v1"
    label = "OpenAI"

    def __init__(self, api_key: str, model: str):
        self.model = model or DEFAULT_MODELS[self.provider]
        self._key = api_key

    def _body(self, system: str, prompt: str, max_tokens: int, effort: str) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "max_completion_tokens": max_tokens,
        }
        if self.model.startswith("o"):
            body["reasoning_effort"] = {"max": "high", "xhigh": "high"}.get(effort, effort)
        return body

    async def complete(self, system: str, prompt: str, *, max_tokens: int = 8000, effort: str = "medium") -> LLMResult:
        started = time.monotonic()
        body = self._body(system, prompt, max_tokens, effort)
        try:
            async with httpx.AsyncClient(timeout=900) as http:
                resp = await http.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self._key}"},
                    json=body,
                )
        except httpx.HTTPError as exc:
            raise LLMError(f"Could not reach the {self.label} API: {exc}") from exc
        if resp.status_code == 401:
            raise LLMError(f"{self.label} rejected the API key (401). Update it under Provider keys.")
        if resp.status_code == 402:
            raise LLMError(f"{self.label} reports insufficient balance (402). Top up the account.")
        if resp.status_code != 200:
            raise LLMError(f"{self.label} API error {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        usage = data.get("usage") or {}
        in_tok, out_tok = int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        return LLMResult(text, in_tok, out_tok, cost_of(self.model, in_tok, out_tok), self.model, self.provider,
                         int((time.monotonic() - started) * 1000))


class DeepSeekClient(OpenAIClient):
    """DeepSeek's OpenAI-compatible API. `deepseek-reasoner` thinks before
    answering (its reasoning is billed as output but not returned in
    `content`); `deepseek-chat` answers directly and caps output at 8K tokens."""

    provider = "deepseek"
    base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
    label = "DeepSeek"

    def _body(self, system: str, prompt: str, max_tokens: int, effort: str) -> dict[str, Any]:
        cap = 8192 if self.model == "deepseek-chat" else 32_768
        return {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "max_tokens": min(max_tokens, cap),
            "stream": False,
        }


async def gemini_image(api_key: str, model: str, prompt: str) -> tuple[bytes, str]:
    """Generate one figure. Returns (image bytes, mime type)."""
    import base64

    model = model or DEFAULT_MODELS["gemini"]
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    try:
        async with httpx.AsyncClient(timeout=180) as http:
            resp = await http.post(
                url,
                headers={"x-goog-api-key": api_key},
                json={"contents": [{"parts": [{"text": prompt}]}],
                      "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]}},
            )
    except httpx.HTTPError as exc:
        raise LLMError(f"Could not reach the Gemini API: {exc}") from exc
    if resp.status_code != 200:
        raise LLMError(f"Gemini API error {resp.status_code}: {resp.text[:300]}")
    for cand in resp.json().get("candidates", []):
        for part in (cand.get("content") or {}).get("parts", []):
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                return base64.b64decode(inline["data"]), inline.get("mimeType") or inline.get("mime_type") or "image/png"
    raise LLMError("Gemini returned no image for the figure prompt.")


# ---- key tests -------------------------------------------------------------

async def test_key(provider: str, api_key: str, model: str) -> tuple[bool, str, str]:
    """Live-check a key without spending tokens. Returns (ok, status, detail)."""
    try:
        if provider == "anthropic":
            client = anthropic.AsyncAnthropic(api_key=api_key, timeout=30.0, max_retries=0)
            try:
                page = await client.models.list(limit=50)
            except anthropic.AuthenticationError:
                return False, "invalid_key", "Anthropic rejected the key (401)."
            except anthropic.APIStatusError as exc:
                return False, "error", f"Anthropic responded {exc.status_code}."
            ids = [m.id for m in page.data]
            if model and ids and model not in ids:
                return True, "ok", f"Key valid. Model '{model}' not in this key's first {len(ids)} listed models — check the id."
            return True, "ok", "Key valid."
        if provider == "openai":
            async with httpx.AsyncClient(timeout=30) as http:
                resp = await http.get("https://api.openai.com/v1/models", headers={"Authorization": f"Bearer {api_key}"})
            if resp.status_code == 401:
                return False, "invalid_key", "OpenAI rejected the key (401)."
            return resp.status_code == 200, "ok" if resp.status_code == 200 else "error", f"OpenAI responded {resp.status_code}."
        if provider == "deepseek":
            async with httpx.AsyncClient(timeout=30) as http:
                resp = await http.get(f"{DeepSeekClient.base_url}/models",
                                      headers={"Authorization": f"Bearer {api_key}"})
            if resp.status_code == 401:
                return False, "invalid_key", "DeepSeek rejected the key (401)."
            if resp.status_code != 200:
                return False, "error", f"DeepSeek responded {resp.status_code}."
            ids = [m.get("id") for m in resp.json().get("data", [])]
            if model and ids and model not in ids:
                return True, "ok", f"Key valid. Available models: {', '.join(ids)} — '{model}' not among them."
            return True, "ok", "Key valid."
        if provider == "gemini":
            async with httpx.AsyncClient(timeout=30) as http:
                resp = await http.get("https://generativelanguage.googleapis.com/v1beta/models",
                                      headers={"x-goog-api-key": api_key})
            if resp.status_code in (400, 401, 403):
                return False, "invalid_key", f"Gemini rejected the key ({resp.status_code})."
            return resp.status_code == 200, "ok" if resp.status_code == 200 else "error", f"Gemini responded {resp.status_code}."
        if provider == "runpod":
            async with httpx.AsyncClient(timeout=30) as http:
                resp = await http.post("https://api.runpod.io/graphql", headers={"Authorization": f"Bearer {api_key}"},
                                       json={"query": "query { myself { id } }"})
            ok = resp.status_code == 200 and not resp.json().get("errors")
            return ok, "ok" if ok else "invalid_key", "RunPod key valid." if ok else f"RunPod responded {resp.status_code}."
    except httpx.HTTPError as exc:
        return False, "unreachable", f"Network error: {exc}"
    return False, "error", f"Unknown provider {provider}"


# ---- resolving a user's provider ------------------------------------------

_ENV_KEYS = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "deepseek": "DEEPSEEK_API_KEY",
             "gemini": "GEMINI_API_KEY",
             "runpod": "RUNPOD_API_KEY"}


def provider_credentials(db: Session, user: User, provider: str) -> tuple[str, str] | None:
    """(api_key, model) for an enabled provider, or None."""
    row = db.scalar(select(ProviderKey).where(ProviderKey.user_id == user.id, ProviderKey.provider == provider))
    if row and row.is_enabled:
        try:
            return decrypt_secret(row.encrypted_key), row.model_name
        except ValueError:
            return None
    if settings.server_keys and os.environ.get(_ENV_KEYS[provider]):
        model = os.environ.get(f"AGENTLAB_{provider.upper()}_MODEL", DEFAULT_MODELS.get(provider, ""))
        return os.environ[_ENV_KEYS[provider]], model
    return None


def enabled_providers(db: Session, user: User) -> list[str]:
    return [p for p in ("anthropic", "openai", "deepseek", "gemini", "runpod") if provider_credentials(db, user, p)]


def _make_client(provider: str, api_key: str, model: str) -> TextClient:
    if provider == "anthropic":
        return AnthropicClient(api_key, model)
    if provider == "deepseek":
        return DeepSeekClient(api_key, model)
    return OpenAIClient(api_key, model)


# Tests replace this to run the whole pipeline without network calls.
client_factory: Callable[[str, str, str], TextClient] = _make_client


def text_provider_order(db: Session, user: User) -> list[str]:
    """Which LLM drives the agent when several keys are enabled.

    The most recently saved enabled text key wins, so adding (or re-saving)
    a DeepSeek key makes DeepSeek the agent's model; disable a key to take it
    out of rotation. Server-environment keys come after the user's own.
    """
    rows = db.scalars(select(ProviderKey).where(ProviderKey.user_id == user.id, ProviderKey.is_enabled.is_(True),
                                                ProviderKey.provider.in_(TEXT_PROVIDERS))
                      .order_by(ProviderKey.updated_at.desc(), ProviderKey.id.desc())).all()
    order = [r.provider for r in rows]
    return order + [p for p in TEXT_PROVIDERS if p not in order]


def text_client(db: Session, user: User) -> TextClient:
    for provider in text_provider_order(db, user):
        creds = provider_credentials(db, user, provider)
        if creds:
            return client_factory(provider, creds[0], creds[1] or DEFAULT_MODELS[provider])
    raise NoProviderError("No enabled Anthropic, OpenAI or DeepSeek key. Add one under Provider keys.")


def text_model_for(db: Session, user: User) -> str | None:
    for provider in text_provider_order(db, user):
        creds = provider_credentials(db, user, provider)
        if creds:
            return creds[1] or DEFAULT_MODELS[provider]
    return None


# ---- parsing model output --------------------------------------------------

_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def extract_json(text: str) -> dict[str, Any] | None:
    """Pull the first JSON object out of a model reply (fenced or bare)."""
    for m in _FENCE.finditer(text):
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None


_CODE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.S)


def extract_code(text: str) -> str | None:
    blocks = _CODE.findall(text)
    return max(blocks, key=len).strip() + "\n" if blocks else None
