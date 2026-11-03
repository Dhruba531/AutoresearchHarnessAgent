"""Cryptographic helpers: key encryption at rest, passwords, tokens, signed URLs."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time

import jwt
from cryptography.fernet import Fernet, InvalidToken

from .config import settings

LOCAL_ISSUER = "agentlab-local"
LOCAL_TOKEN_TTL_S = 7 * 24 * 3600


def _fernet() -> Fernet:
    digest = hashlib.sha256(("provider-keys:" + settings.secret_key).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def decrypt_secret(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:  # secret key rotated or row corrupted
        raise ValueError("Stored key can no longer be decrypted; save it again.") from exc


def mask_key(plain: str) -> str:
    tail = plain[-4:] if len(plain) >= 8 else ""
    return f"{plain[:3]}…{tail}" if tail else "••••"


# ---- passwords (scrypt, stdlib only) ---------------------------------------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored or not stored.startswith("scrypt$"):
        return False
    _, salt_hex, dk_hex = stored.split("$", 2)
    dk = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1, dklen=32)
    return hmac.compare_digest(dk.hex(), dk_hex)


# ---- local session tokens ---------------------------------------------------

def issue_local_token(user_id: int, email: str) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": str(user_id), "email": email, "iss": LOCAL_ISSUER, "iat": now, "exp": now + LOCAL_TOKEN_TTL_S},
        settings.secret_key,
        algorithm="HS256",
    )


def decode_local_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, settings.secret_key, algorithms=["HS256"], issuer=LOCAL_ISSUER)
    except jwt.PyJWTError:
        return None


# ---- signed artifact URLs ---------------------------------------------------
# <img src> and <a href> cannot carry an Authorization header, so artifact
# links embed a short-lived HMAC signature instead.

def sign_artifact(artifact_id: int, ttl_s: int = 24 * 3600) -> tuple[int, str]:
    exp = int(time.time()) + ttl_s
    msg = f"artifact:{artifact_id}:{exp}".encode()
    sig = hmac.new(settings.secret_key.encode(), msg, hashlib.sha256).hexdigest()
    return exp, sig


def verify_artifact_sig(artifact_id: int, exp: int, sig: str) -> bool:
    if exp < time.time():
        return False
    msg = f"artifact:{artifact_id}:{exp}".encode()
    good = hmac.new(settings.secret_key.encode(), msg, hashlib.sha256).hexdigest()
    return hmac.compare_digest(good, sig)
