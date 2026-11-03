"""Runtime settings, read once from the environment.

Every knob has a default that works for a single operator on localhost; the
README lists which ones to change before exposing the service to anyone else.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _list(name: str, default: str) -> list[str]:
    return [s.strip() for s in os.environ.get(name, default).split(",") if s.strip()]


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader so `uvicorn backend.app:app` picks up backend/.env.

    Existing environment variables always win, so a deploy can override any
    value without editing the file.
    """
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv(Path(__file__).resolve().parent / ".env")


def _secret(data_dir: Path) -> str:
    """The server secret signs local-auth tokens and artifact URLs and derives
    the key that encrypts provider API keys at rest.

    Generated once and persisted in the data directory when not supplied, so a
    restart does not invalidate every session and stored key.
    """
    explicit = os.environ.get("AGENTLAB_SECRET_KEY", "").strip()
    if explicit:
        return explicit
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / ".secret_key"
    if path.exists():
        return path.read_text().strip()
    value = secrets.token_urlsafe(48)
    path.write_text(value)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return value


@dataclass
class Settings:
    data_dir: Path = field(
        default_factory=lambda: Path(os.environ.get("AGENTLAB_DATA_DIR", "data")).resolve()
    )
    database_url: str = ""
    secret_key: str = ""

    # Auth: "supabase" verifies Supabase access tokens; "local" uses this
    # service's own email/password accounts; "auto" accepts both.
    auth_mode: str = field(default_factory=lambda: os.environ.get("AGENTLAB_AUTH", "auto").lower())
    supabase_url: str = field(
        default_factory=lambda: (
            os.environ.get("SUPABASE_URL") or os.environ.get("VITE_SUPABASE_URL") or ""
        ).rstrip("/")
    )
    supabase_jwt_secret: str = field(default_factory=lambda: os.environ.get("SUPABASE_JWT_SECRET", ""))
    supabase_publishable_key: str = field(
        default_factory=lambda: os.environ.get("SUPABASE_PUBLISHABLE_KEY")
        or os.environ.get("VITE_SUPABASE_PUBLISHABLE_KEY")
        or ""
    )
    allow_registration: bool = field(default_factory=lambda: _bool("AGENTLAB_ALLOW_REGISTRATION", True))

    cors_origins: list[str] = field(
        default_factory=lambda: _list(
            "AGENTLAB_CORS_ORIGINS",
            "http://localhost:8080,http://127.0.0.1:8080,http://localhost:3000,http://127.0.0.1:3000",
        )
    )

    # Spend controls.
    monthly_cap_usd: float = field(default_factory=lambda: _float("AGENTLAB_MONTHLY_CAP_USD", 50.0))
    max_active_runs: int = field(default_factory=lambda: _int("AGENTLAB_MAX_ACTIVE_RUNS", 2))

    # Let every user fall back to keys in the server environment
    # (ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY). Off by default: with
    # it on, anyone who can sign up spends the operator's credits.
    server_keys: bool = field(default_factory=lambda: _bool("AGENTLAB_SERVER_KEYS", False))

    # Code execution: "local" runs in a resource-limited subprocess on this
    # host, "runpod" sends jobs to a RunPod Serverless endpoint, "none" turns
    # execution off.
    execution: str = field(default_factory=lambda: os.environ.get("AGENTLAB_EXECUTION", "local").lower())
    exec_timeout_s: int = field(default_factory=lambda: _int("AGENTLAB_EXEC_TIMEOUT_S", 300))
    exec_memory_mb: int = field(default_factory=lambda: _int("AGENTLAB_EXEC_MEMORY_MB", 2048))
    exec_python: str = field(default_factory=lambda: os.environ.get("AGENTLAB_EXEC_PYTHON", ""))

    # Groundedness: probe cited URLs over the network before export.
    check_urls: bool = field(default_factory=lambda: _bool("AGENTLAB_CHECK_URLS", True))

    region: str = field(default_factory=lambda: os.environ.get("AGENTLAB_REGION", "local"))
    max_upload_mb: int = field(default_factory=lambda: _int("AGENTLAB_MAX_UPLOAD_MB", 10))

    def __post_init__(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not self.database_url:
            self.database_url = os.environ.get(
                "DATABASE_URL", f"sqlite:///{self.data_dir / 'agentlab.db'}"
            )
        if not self.secret_key:
            self.secret_key = _secret(self.data_dir)

    @property
    def supabase_enabled(self) -> bool:
        return self.auth_mode in {"supabase", "auto"} and bool(self.supabase_url)

    @property
    def local_auth_enabled(self) -> bool:
        return self.auth_mode in {"local", "auto"}


settings = Settings()
