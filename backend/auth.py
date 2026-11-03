"""Who is calling.

Two identity sources, either or both enabled by AGENTLAB_AUTH:

* Supabase — the console signs in against Supabase and sends its access token
  as a bearer token. Verified with SUPABASE_JWT_SECRET (HS256 projects), the
  project's JWKS (asymmetric signing keys), or, failing both, by asking
  Supabase's /auth/v1/user endpoint.
* Local — email/password accounts stored here, for running without Supabase.

Browsers cannot set headers on WebSocket handshakes, so sockets pass the same
token as a `?token=` query parameter.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import jwt
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .db import User, get_db
from .security import decode_local_token

_jwks_client: jwt.PyJWKClient | None = None
_remote_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _jwks() -> jwt.PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = jwt.PyJWKClient(f"{settings.supabase_url}/auth/v1/.well-known/jwks.json")
    return _jwks_client


def _verify_supabase(token: str) -> dict[str, Any] | None:
    if not settings.supabase_enabled:
        return None
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        return None
    alg = header.get("alg", "")
    try:
        if alg == "HS256" and settings.supabase_jwt_secret:
            return jwt.decode(token, settings.supabase_jwt_secret, algorithms=["HS256"], audience="authenticated")
        if alg.startswith(("RS", "ES")):
            key = _jwks().get_signing_key_from_jwt(token)
            return jwt.decode(token, key.key, algorithms=[alg], audience="authenticated")
    except jwt.PyJWTError:
        return None
    except Exception:  # JWKS unreachable — fall through to the remote check
        pass

    # Last resort: let Supabase verify the token. Cached briefly, because the
    # console makes many calls per page and each would otherwise round-trip.
    cached = _remote_cache.get(token)
    if cached and cached[0] > time.time():
        return cached[1]
    try:
        resp = httpx.get(
            f"{settings.supabase_url}/auth/v1/user",
            headers={"Authorization": f"Bearer {token}", "apikey": settings.supabase_publishable_key},
            timeout=10,
        )
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    data = resp.json()
    claims = {"sub": data.get("id"), "email": data.get("email"), "user_metadata": data.get("user_metadata") or {}}
    _remote_cache[token] = (time.time() + 60, claims)
    return claims


def user_from_token(token: str | None, db: Session) -> User | None:
    if not token:
        return None
    if settings.local_auth_enabled:
        claims = decode_local_token(token)
        if claims:
            return db.get(User, int(claims["sub"]))
    claims = _verify_supabase(token)
    if not claims or not claims.get("sub"):
        return None
    external_id = f"supabase:{claims['sub']}"
    user = db.scalar(select(User).where(User.external_id == external_id))
    meta = claims.get("user_metadata") or {}
    email = claims.get("email") or ""
    name = (meta.get("name") or meta.get("full_name") or email.split("@")[0] or "operator") if isinstance(meta, dict) else email
    if user is None:
        user = User(external_id=external_id, email=email, name=name, role="admin")
        db.add(user)
        db.commit()
    elif email and user.email != email:
        user.email = email
        db.commit()
    return user


def bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.query_params.get("token")


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    user = user_from_token(bearer_token(request), db)
    if user is None:
        raise HTTPException(status_code=401, detail="Not signed in, or the session expired.")
    return user
