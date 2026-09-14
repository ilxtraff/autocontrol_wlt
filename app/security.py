"""Пароли и сессионные куки. Только стандартная библиотека."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from app.config import get_settings

_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_KEY_LEN = 32

SESSION_COOKIE = "ac_session"
SESSION_TTL = 60 * 60 * 12  # 12 часов


# --------------------------------------------------------------------------- #
#  Пароли
# --------------------------------------------------------------------------- #

def hash_password(password: str) -> str:
    if not password:
        raise ValueError("пустой пароль")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
        dklen=_KEY_LEN,
    )
    return "scrypt${}${}${}${}${}".format(
        _SCRYPT_N, _SCRYPT_R, _SCRYPT_P,
        base64.b64encode(salt).decode(), base64.b64encode(digest).decode(),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=int(n), r=int(r), p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# --------------------------------------------------------------------------- #
#  Подписанные куки
# --------------------------------------------------------------------------- #

def _sign(payload: bytes) -> bytes:
    key = get_settings().secret_key.encode("utf-8")
    return hmac.new(key, payload, hashlib.sha256).digest()


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(raw: str) -> bytes:
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


def issue_session(user_id: int, ttl: int = SESSION_TTL) -> str:
    payload = json.dumps(
        {"uid": user_id, "exp": int(time.time()) + ttl}, separators=(",", ":")
    ).encode("utf-8")
    return f"{_b64e(payload)}.{_b64e(_sign(payload))}"


def read_session(token: str | None) -> dict[str, Any] | None:
    if not token or "." not in token:
        return None
    body, _, signature = token.partition(".")
    try:
        payload = _b64d(body)
        if not hmac.compare_digest(_b64d(signature), _sign(payload)):
            return None
        data = json.loads(payload)
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("exp", 0) < time.time():
        return None
    return data
