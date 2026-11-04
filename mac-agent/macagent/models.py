"""Which model Relay uses, chosen from the phone, with automatic failover.

* Keys for any number of providers live in $RELAY_HOME/env (chmod 600), the same
  file the installer writes. They are never returned: the app sees the last 4
  characters only, and they never enter a model's context.
* The active provider and its main (tasks) and fast (chat, quick tasks) models are
  picked in Settings and kept in the database, so the choice survives restarts.
* Failover: when a call fails because a key ran out of credit, was rejected, or hit
  a rate limit, Relay marks that provider as resting, switches to the next one in
  your order that has a key, retries the same step there, and tells you.
"""

from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path

import httpx

from .llm import BASE_URLS, LLMError, make_adapter, make_router_adapter

log = logging.getLogger("relay.models")

# Presets shown in the app; any other model id can be typed in.
PROVIDERS: dict[str, dict] = {
    "gemini": {"label": "Gemini", "env": "GEMINI_API_KEY", "attr": "gemini_api_key", "hint": "AIza…",
               "main": "gemini-3.8-flash", "fast": "gemini-3.5-flash-lite",
               "models": ["gemini-3.8-flash", "gemini-3.5-flash-lite"],
               "note": "Fast and low cost.", "get_key": "https://aistudio.google.com/apikey"},
    "deepseek": {"label": "DeepSeek", "env": "DEEPSEEK_API_KEY", "attr": "deepseek_api_key", "hint": "sk-…",
                 "main": "deepseek-chat", "fast": "deepseek-chat", "models": ["deepseek-chat", "deepseek-reasoner"],
                 "note": "Cheapest per token; a little slower.", "get_key": "https://platform.deepseek.com/api_keys"},
    "openai": {"label": "OpenAI", "env": "OPENAI_API_KEY", "attr": "openai_api_key", "hint": "sk-proj-…",
               "main": "gpt-5.4-mini", "fast": "gpt-5.4-nano", "models": ["gpt-5.4-mini", "gpt-5.4-nano"],
               "note": "Fast, low cost.", "get_key": "https://platform.openai.com/api-keys"},
    "openrouter": {"label": "OpenRouter", "env": "OPENROUTER_API_KEY", "attr": "openrouter_api_key", "hint": "sk-or-…",
                   "main": "anthropic/claude-opus-5.5", "fast": "anthropic/claude-haiku-5.5",
                   "models": ["anthropic/claude-opus-5.5", "anthropic/claude-haiku-5.5"],
                   "note": "One key, hundreds of models: type any OpenRouter model id.",
                   "get_key": "https://openrouter.ai/settings/keys"},
    "anthropic": {"label": "Anthropic (Claude)", "env": "ANTHROPIC_API_KEY", "attr": "anthropic_api_key",
                  "hint": "sk-ant-…", "main": "claude-opus-5-5", "fast": "claude-haiku-5-5",
                  "models": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"],
                  "note": "Smartest, and the only one that can click the screen. Costs the most.",
                  "get_key": "https://console.anthropic.com/settings/keys"},
    "local": {"label": "On this Mac (Ollama)", "env": None, "attr": None, "hint": "",
              "main": "qwen3:8b", "fast": "qwen3:8b", "models": ["qwen3:8b", "qwen3:32b"],
              "note": "Free and private; slower and less capable.", "get_key": "https://ollama.com/download"},
}
DEFAULT_ORDER = ["gemini", "deepseek", "openai", "openrouter", "anthropic", "local"]

# What "this key can't be used right now" looks like across providers.
EXHAUSTED = re.compile(r"\b(401|402|403|429)\b|rate.?limit|quota|credit|insufficient|billing|exceeded|"
                       r"rejected the api key|invalid.{0,20}key|unauthori[sz]ed|resource.?exhausted|balance", re.I)
REST_S = {"rate": 120, "other": 3600}  # a rate limit clears soon; credit or auth problems need you


def _reason(r: httpx.Response) -> str:
    """The human part of an API error body."""
    try:
        d = r.json()
        e = d.get("error", d) if isinstance(d, dict) else d
        if isinstance(e, list) and e:
            e = e[0].get("error", e[0])
        msg = e.get("message") if isinstance(e, dict) else str(e)
        return str(msg or r.text)[:160]
    except Exception:
        return r.text[:160]


def short(model: str) -> str:
    return model.split("/", 1)[-1]


class ModelManager:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        self.listeners: list = []  # called with a message when Relay switches by itself

    # -- keys -----------------------------------------------------------------------
    @property
    def env_path(self) -> Path:
        return Path(self.settings.home) / "env"

    def key(self, provider: str) -> str:
        p = PROVIDERS[provider]
        return getattr(self.settings, p["attr"], "") if p["attr"] else ""

    def has_key(self, provider: str) -> bool:
        if provider == "local":
            return self.local_up()
        return bool(self.key(provider))

    def local_up(self) -> bool:
        """Is a local model server answering? Checked at most every 30 s."""
        now = time.time()
        if now - getattr(self, "_local_at", 0) > 30:
            import urllib.request

            try:
                with urllib.request.urlopen(self.settings.local_base_url.rstrip("/") + "/models", timeout=0.5):
                    self._local = True
            except Exception:
                self._local = False
            self._local_at = now
        return self._local

    def _write_env(self, name: str, value: str | None) -> None:
        path = self.env_path
        lines = path.read_text().splitlines() if path.exists() else []
        lines = [ln for ln in lines if not ln.startswith(f"{name}=")]
        if value is not None:
            lines.append(f"{name}={value}")
        path.write_text("\n".join(lines) + "\n")
        os.chmod(path, 0o600)

    def set_key(self, provider: str, key: str) -> None:
        p = PROVIDERS.get(provider)
        if not p or not p["env"]:
            raise ValueError("That provider takes no key.")
        key = key.strip()
        if not key or len(key) < 12 or any(c.isspace() for c in key):
            raise ValueError("That doesn't look like an API key.")
        self._write_env(p["env"], key)
        os.environ[p["env"]] = key
        setattr(self.settings, p["attr"], key)
        self._usage_update(provider, rest_until=0, last_error="")

    def remove_key(self, provider: str) -> None:
        p = PROVIDERS[provider]
        if not p["env"]:
            return
        self._write_env(p["env"], None)
        os.environ.pop(p["env"], None)
        setattr(self.settings, p["attr"], "")

    async def test_key(self, provider: str, transport=None) -> tuple[bool, str]:
        """List the provider's models with the key: free, and proves the key works."""
        if provider == "local":
            url, headers = self.settings.local_base_url.rstrip("/") + "/models", {}
        elif provider == "anthropic":
            url = "https://api.anthropic.com/v1/models"
            headers = {"x-api-key": self.key(provider), "anthropic-version": "2023-06-01"}
        else:
            url, headers = BASE_URLS[provider] + "/models", {"Authorization": f"Bearer {self.key(provider)}"}
        if provider == "openrouter":
            url = "https://openrouter.ai/api/v1/key"  # /models needs no key, so it would prove nothing
        try:
            async with httpx.AsyncClient(timeout=15, transport=transport) as http:
                r = await http.get(url, headers=headers)
        except httpx.HTTPError as exc:
            return False, f"Could not reach {PROVIDERS[provider]['label']}: {exc}"
        if r.status_code == 200:
            return True, "Key works."
        return False, f"{PROVIDERS[provider]['label']} said no ({r.status_code}): {_reason(r)}"

    async def credits(self, provider: str, transport=None) -> dict | None:
        """Remaining credit, where the provider reports it (OpenRouter does)."""
        if provider != "openrouter" or not self.key(provider):
            return None
        try:
            async with httpx.AsyncClient(timeout=15, transport=transport) as http:
                r = await http.get("https://openrouter.ai/api/v1/credits",
                                   headers={"Authorization": f"Bearer {self.key(provider)}"})
            d = r.json().get("data") or {}
            total, used = float(d.get("total_credits")), float(d.get("total_usage"))
            return {"remaining_usd": round(total - used, 4), "total_usd": total}
        except Exception:
            return None

    # -- choice -----------------------------------------------------------------------
    def prefs(self) -> dict:
        return self.store.get("model_prefs", {})

    def presets(self, provider: str) -> list[str]:
        models = list(PROVIDERS[provider]["models"])
        tuned = os.environ.get("RELAY_MODEL", "")
        if provider == "local" and tuned.startswith("relay-"):
            models.insert(0, tuned)  # the installer's copy with a 16k context
        return models

    def models_for(self, provider: str) -> tuple[str, str]:
        pref = self.prefs().get(provider, {})
        if not pref and provider == self.settings.provider:  # what runs now, from the env file
            return self.settings.model, self.settings.router_model
        first = self.presets(provider)[0] if provider == "local" else PROVIDERS[provider]["main"]
        fast = first if provider == "local" else PROVIDERS[provider]["fast"]
        return pref.get("model") or first, pref.get("router_model") or fast

    def use(self, provider: str, model: str | None = None, router_model: str | None = None, *, save: bool = True) -> None:
        if provider not in PROVIDERS:
            raise ValueError(f"Unknown provider {provider}.")
        if not self.has_key(provider):
            raise ValueError(f"Add a {PROVIDERS[provider]['label']} key first.")
        main, fast = self.models_for(provider)
        main, fast = (model or main).strip(), (router_model or fast).strip()
        self.settings.provider, self.settings.model, self.settings.router_model = provider, main, fast
        if save:
            prefs = self.prefs()
            prefs[provider] = {"model": main, "router_model": fast}
            self.store.put("model_prefs", prefs)
            self.store.put("model_choice", {"provider": provider, "model": main, "router_model": fast})

    def resolve(self, text: str) -> tuple[str, str | None] | None:
        """A name as people say it ("gemini", "gpt nano", "opus", "local") → (provider, main model or None).
        Prefers providers you have a key for."""
        t = " " + re.sub(r"[^a-z0-9.:/-]+", " ", text.lower()) + " "
        usable = [p for p in self.fallback()["order"] if self.has_key(p)]
        # an exact model id first ("gpt-5.4-nano", "anthropic/claude-haiku-5.5")
        for pid in usable:
            for m in self.presets(pid) + [self.models_for(pid)[0]]:
                if f" {m.lower()} " in t or f" {short(m).lower()} " in t:
                    return pid, m
        nick = {"opus": "opus", "sonnet": "sonnet", "haiku": "haiku", "nano": "nano", "mini": "mini",
                "lite": "lite", "reasoner": "reasoner", "32b": "32b"}
        word = next((v for k, v in nick.items() if f" {k} " in t or f"-{k} " in t), None)
        alias = {"gemini": "gemini", "google": "gemini", "deepseek": "deepseek", "openai": "openai", "gpt": "openai",
                 "chatgpt": "openai", "openrouter": "openrouter", "anthropic": "anthropic", "claude": "anthropic",
                 "local": "local", "ollama": "local", "qwen": "local"}
        named = [v for k, v in alias.items() if f" {k} " in t or t.strip().startswith(k)]
        if "claude" in t and "anthropic" not in usable and "openrouter" in usable:
            named = ["openrouter"] + named  # Claude through OpenRouter when there is no Anthropic key
        candidates = [p for p in dict.fromkeys(named) if p in usable] or (usable if word else [])
        for pid in candidates:
            if word:
                hit = next((m for m in self.presets(pid) if word in short(m).lower()), None)
                if hit:
                    return pid, hit
            elif pid in named:
                return pid, None
        return None

    def apply_saved(self) -> None:
        c = self.store.get("model_choice")
        if c and c.get("provider") in PROVIDERS and self.has_key(c["provider"]):
            self.use(c["provider"], c.get("model"), c.get("router_model"), save=False)

    def fallback(self) -> dict:
        f = self.store.get("model_fallback") or {}
        order = [p for p in f.get("order", []) if p in PROVIDERS]
        order += [p for p in DEFAULT_ORDER if p not in order]
        return {"auto": f.get("auto", True), "order": order}

    def set_fallback(self, order: list[str] | None = None, auto: bool | None = None) -> dict:
        f = self.fallback()
        if order is not None:
            f["order"] = [p for p in order if p in PROVIDERS] + [p for p in f["order"] if p not in order]
        if auto is not None:
            f["auto"] = bool(auto)
        self.store.put("model_fallback", f)
        return f

    # -- usage ---------------------------------------------------------------------------
    def _usage_all(self) -> dict:
        u = self.store.get("provider_usage", {})
        today = time.strftime("%Y-%m-%d")
        for v in u.values():
            if v.get("day") != today:
                v.update(day=today, usd=0.0, calls=0)
        return u

    def _usage_update(self, provider: str, **kw) -> None:
        u = self._usage_all()
        v = u.setdefault(provider, {"day": time.strftime("%Y-%m-%d"), "usd": 0.0, "calls": 0})
        v.update(kw)
        self.store.put("provider_usage", u)

    def record(self, provider: str, usd: float = 0.0, error: str | None = None) -> None:
        u = self._usage_all()
        v = u.setdefault(provider, {"day": time.strftime("%Y-%m-%d"), "usd": 0.0, "calls": 0})
        if error is None:
            v["usd"] = round(v.get("usd", 0) + usd, 6)
            v["calls"] = v.get("calls", 0) + 1
        else:
            v["last_error"], v["last_error_at"] = error[:240], time.time()
        self.store.put("provider_usage", u)

    def resting(self, provider: str) -> bool:
        return (self._usage_all().get(provider, {}).get("rest_until") or 0) > time.time()

    # -- failover ---------------------------------------------------------------------
    def failover(self, failed: str, exc: Exception, tried: set[str]) -> str | None:
        """After a call failed on `failed`: switch to the next usable provider, or None."""
        msg = str(exc)
        if not EXHAUSTED.search(msg):
            return None  # a bug or a bad request: switching would not help
        rate = bool(re.search(r"429|rate.?limit", msg, re.I))
        self._usage_update(failed, rest_until=time.time() + REST_S["rate" if rate else "other"])
        f = self.fallback()
        if not f["auto"]:
            return None
        for p in f["order"]:
            if p != failed and p not in tried and self.has_key(p) and not self.resting(p):
                before = PROVIDERS[failed]["label"]
                self.use(p)
                why = "hit its rate limit" if rate else "can't be used right now (credit or key)"
                note = (f"{before} {why}, so I switched to {PROVIDERS[p]['label']} "
                        f"({short(self.settings.model)}). Change it any time in Settings → Models.")
                log.warning(note)
                for fn in self.listeners:
                    try:
                        fn(note)
                    except Exception:
                        log.exception("model switch listener failed")
                return p
        return None

    def make(self, role: str):
        return make_adapter(self.settings) if role == "main" else make_router_adapter(self.settings)

    # -- for the app --------------------------------------------------------------------
    def status(self) -> dict:
        usage, f = self._usage_all(), self.fallback()
        rows = []
        for pid in f["order"]:
            p, k = PROVIDERS[pid], self.key(pid)
            main, fast = self.models_for(pid)
            u = usage.get(pid, {})
            rows.append({"id": pid, "label": p["label"], "note": p["note"], "hint": p["hint"], "get_key": p["get_key"],
                         "needs_key": bool(p["env"]), "has_key": self.has_key(pid), "key_last4": k[-4:] if k else "",
                         "offline": pid == "local" and not self.has_key(pid),
                         "models": self.presets(pid), "model": main, "router_model": fast,
                         "can_see_screen": pid == "anthropic", "today_usd": round(u.get("usd", 0), 4),
                         "today_calls": u.get("calls", 0), "last_error": u.get("last_error", ""),
                         "resting": self.resting(pid), "active": pid == self.settings.provider})
        return {"active": {"provider": self.settings.provider, "label": PROVIDERS.get(self.settings.provider, {}).get(
                    "label", self.settings.provider), "model": self.settings.model, "router_model": self.settings.router_model},
                "providers": rows, "auto": f["auto"], "order": f["order"]}


class FailoverAdapter:
    """What workers and the router call. Follows the provider chosen in the app, even mid-task, and moves to
    the next provider when a key runs out."""

    def __init__(self, mgr: ModelManager, role: str):
        self.mgr, self.role = mgr, role
        self._screen: bool | None = None
        self._inner, self._for = None, None

    def _adapter(self):
        want = (self.mgr.settings.provider, self.mgr.settings.model if self.role == "main" else self.mgr.settings.router_model)
        if self._inner is None or self._for != want:
            self._inner, self._for = self.mgr.make(self.role), want
            if self._screen is False:
                self._inner.can_see_screen = False
        return self._inner

    @property
    def can_see_screen(self) -> bool:
        try:
            return self._adapter().can_see_screen
        except LLMError:
            return False

    @can_see_screen.setter
    def can_see_screen(self, value: bool) -> None:
        self._screen = value
        if self._inner is not None and value is False:
            self._inner.can_see_screen = False

    async def step(self, system, history, tools):
        tried: set[str] = set()
        while True:
            provider = self.mgr.settings.provider
            try:
                turn = await self._adapter().step(system, history, tools)
            except LLMError as exc:
                self.mgr.record(provider, error=str(exc))
                tried.add(provider)
                if self.mgr.failover(provider, exc, tried) is None:
                    raise
                continue
            self.mgr.record(provider, turn.cost_usd)
            return turn


__all__ = ["ModelManager", "FailoverAdapter", "PROVIDERS", "DEFAULT_ORDER"]
