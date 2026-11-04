"""Device pairing and bearer tokens.

A phone gets access by redeeming a short-lived, single-use pairing code shown
on the Mac (terminal QR, or the menu printed at startup). Redeeming it issues
a long random device token; only its SHA-256 is stored, so a leaked database
does not leak working tokens. Devices can be listed and revoked.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections import deque

from .store import Store

PAIRING_TTL_S = 10 * 60
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O/1/I/L


def _h(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def new_pairing_code(store: Store) -> str:
    code = "".join(secrets.choice(_ALPHABET) for _ in range(8))
    store.x("DELETE FROM pairing_codes WHERE expires_at < ?", (time.time(),))
    store.x("INSERT INTO pairing_codes(code_hash,expires_at) VALUES(?,?)", (_h(code), time.time() + PAIRING_TTL_S))
    return code


def redeem_pairing_code(store: Store, code: str, device_name: str) -> str | None:
    code = code.strip().upper().replace("-", "").replace(" ", "")
    row = store.one("SELECT * FROM pairing_codes WHERE code_hash=?", (_h(code),))
    if not row or row["used"] or row["expires_at"] < time.time():
        return None
    claimed = store.x("UPDATE pairing_codes SET used=1 WHERE code_hash=? AND used=0", (_h(code),)).rowcount
    if claimed != 1:  # lost a race to another redemption
        return None
    token = "rly_" + secrets.token_urlsafe(32)
    store.x("INSERT INTO devices(name,token_hash,created_at) VALUES(?,?,?)",
            (device_name.strip()[:60] or "phone", _h(token), time.time()))
    return token


def device_for_token(store: Store, token: str | None) -> dict | None:
    if not token or not token.startswith("rly_"):
        return None
    row = store.one("SELECT id,name,token_hash,revoked FROM devices WHERE token_hash=?", (_h(token),))
    if not row or row["revoked"] or not hmac.compare_digest(row["token_hash"], _h(token)):
        return None
    store.x("UPDATE devices SET last_seen=? WHERE id=?", (time.time(), row["id"]))
    return {"id": row["id"], "name": row["name"]}


def list_devices(store: Store) -> list[dict]:
    return store.all("SELECT id,name,created_at,last_seen FROM devices WHERE revoked=0 ORDER BY id")


def revoke_device(store: Store, device_id: int) -> None:
    store.x("UPDATE devices SET revoked=1 WHERE id=?", (device_id,))


class RateLimiter:
    """Sliding-window limiter for unauthenticated endpoints (pairing)."""

    def __init__(self, limit: int, window_s: float):
        self.limit, self.window = limit, window_s
        self.hits: dict[str, deque] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        q = self.hits.setdefault(key, deque())
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True
