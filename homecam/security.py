"""Small, dependency-free authentication helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time


def new_secret_token() -> str:
    return secrets.token_urlsafe(32)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_password(password: str, salt: bytes | None = None) -> tuple[bytes, bytes]:
    if salt is None:
        salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32, maxmem=64 * 1024 * 1024)
    return salt, digest


def verify_password(password: str, salt: bytes, expected: bytes) -> bool:
    try:
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32, maxmem=64 * 1024 * 1024)
    except (ValueError, MemoryError):
        return False
    return hmac.compare_digest(actual, expected)


def make_turn_credentials(secret: bytes, identity: str, ttl: int = 3600, now: int | None = None) -> tuple[str, str, int]:
    """Return coturn REST username, credential and expiry epoch."""
    if now is None:
        now = int(time.time())
    expiry = now + ttl
    username = f"{expiry}:{identity}"
    digest = hmac.new(secret, username.encode("utf-8"), hashlib.sha1).digest()
    credential = base64.b64encode(digest).decode("ascii")
    return username, credential, expiry
