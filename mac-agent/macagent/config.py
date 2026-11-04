"""Settings: environment variables, overridable per run from the CLI.

Everything Relay stores — the database, screenshots, the pairing secret — lives
under RELAY_HOME (default ~/.relay), created with owner-only permissions.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    return default if not raw else raw in {"1", "true", "yes", "on"}


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


def load_env_file() -> None:
    """KEY=VALUE lines from $RELAY_HOME/env (keep it chmod 600). Real
    environment variables win. This is how the LaunchAgent gets API keys."""
    path = Path(os.environ.get("RELAY_HOME", "~/.relay")).expanduser() / "env"
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env_file()


@dataclass
class Settings:
    home: Path = field(default_factory=lambda: Path(os.environ.get("RELAY_HOME", "~/.relay")).expanduser())
    host: str = field(default_factory=lambda: os.environ.get("RELAY_BIND", "127.0.0.1"))
    port: int = field(default_factory=lambda: _int("RELAY_PORT", 8765))

    # The model. Claude drives screen control; DeepSeek/OpenAI-compatible
    # models can run the non-screen tools (they cannot see screenshots).
    provider: str = field(default_factory=lambda: os.environ.get("RELAY_PROVIDER", "anthropic"))
    model: str = field(default_factory=lambda: os.environ.get("RELAY_MODEL", ""))
    effort: str = field(default_factory=lambda: os.environ.get("RELAY_EFFORT", "medium"))
    anthropic_api_key: str = field(default_factory=lambda: os.environ.get("ANTHROPIC_API_KEY", ""))
    deepseek_api_key: str = field(default_factory=lambda: os.environ.get("DEEPSEEK_API_KEY", ""))
    openai_api_key: str = field(default_factory=lambda: os.environ.get("OPENAI_API_KEY", ""))
    openrouter_api_key: str = field(default_factory=lambda: os.environ.get("OPENROUTER_API_KEY", ""))

    # Layers: a light router model talks to you and dispatches; heavy workers run tasks.
    router_model: str = field(default_factory=lambda: os.environ.get("RELAY_ROUTER_MODEL", ""))
    router_batch_ms: int = field(default_factory=lambda: _int("RELAY_ROUTER_BATCH_MS", 1200))
    router_max_wait_ms: int = field(default_factory=lambda: _int("RELAY_ROUTER_MAX_WAIT_MS", 4000))
    max_workers: int = field(default_factory=lambda: _int("RELAY_WORKERS", 3))
    # This server's own machine: "auto" (this Mac, or a virtual one with --fake-host) or "none" (a cloud loop
    # that only drives machines through daemons).
    local_host: str = field(default_factory=lambda: os.environ.get("RELAY_LOCAL_HOST", "auto"))
    watchdog_interval_s: int = field(default_factory=lambda: _int("RELAY_WATCHDOG_INTERVAL_S", 300))
    watchdog_stall_s: int = field(default_factory=lambda: _int("RELAY_WATCHDOG_STALL_S", 900))
    watchdog_restart: bool = field(default_factory=lambda: _bool("RELAY_WATCHDOG_RESTART", False))

    # Guard rails.
    policy: str = field(default_factory=lambda: os.environ.get("RELAY_POLICY", "balanced"))  # careful|balanced|autonomous
    max_steps: int = field(default_factory=lambda: _int("RELAY_MAX_STEPS", 80))
    daily_cap_usd: float = field(default_factory=lambda: _float("RELAY_DAILY_CAP_USD", 10.0))
    approval_timeout_s: int = field(default_factory=lambda: _int("RELAY_APPROVAL_TIMEOUT_S", 1800))
    screen_max_edge: int = field(default_factory=lambda: _int("RELAY_SCREEN_MAX_EDGE", 1440))
    shell_timeout_s: int = field(default_factory=lambda: _int("RELAY_SHELL_TIMEOUT_S", 120))

    # Phone channels beyond the web app.
    telegram_token: str = field(default_factory=lambda: os.environ.get("RELAY_TELEGRAM_TOKEN", ""))
    ntfy_topic: str = field(default_factory=lambda: os.environ.get("RELAY_NTFY_TOPIC", ""))
    ntfy_server: str = field(default_factory=lambda: os.environ.get("RELAY_NTFY_SERVER", "https://ntfy.sh"))

    fake_host: bool = field(default_factory=lambda: _bool("RELAY_FAKE_HOST", False))
    keep_awake: bool = field(default_factory=lambda: _bool("RELAY_KEEP_AWAKE", True))

    def __post_init__(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        try:
            self.home.chmod(0o700)
        except OSError:
            pass
        if not self.router_model:
            self.router_model = {"anthropic": "claude-haiku-5-5", "deepseek": "deepseek-chat",
                                 "openai": "gpt-4.1-mini", "openrouter": "anthropic/claude-haiku-5.5"}.get(
                self.provider, "claude-haiku-5-5")
        if not self.model:
            self.model = {"anthropic": "claude-opus-5-5", "deepseek": "deepseek-chat", "openai": "gpt-4.1",
                          "openrouter": "anthropic/claude-opus-5.5"}.get(self.provider, "claude-opus-5-5")

    def api_key(self) -> str:
        """The key for the configured provider."""
        return {"anthropic": self.anthropic_api_key, "deepseek": self.deepseek_api_key,
                "openai": self.openai_api_key, "openrouter": self.openrouter_api_key}.get(self.provider, "")

    @property
    def db_path(self) -> Path:
        return self.home / "relay.db"

    @property
    def can_see_screen(self) -> bool:
        return self.provider == "anthropic"


settings = Settings()
