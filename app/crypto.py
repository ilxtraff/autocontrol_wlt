"""Шифрование учётных данных в базе.

Куки и токен — это доступ к социальному аккаунту целиком. В SQLite они лежат
обычным текстом, и любой, кто дотянулся до файла, получает аккаунт. Поэтому
секреты шифруются ключом, выведенным из AC_SECRET_KEY.

Ключ обязан быть постоянным: если он меняется от запуска к запуску,
расшифровать раньше сохранённое уже не выйдет.
"""
from __future__ import annotations

import base64
import hashlib
import logging

from app.config import get_settings

log = logging.getLogger(__name__)

_PREFIX = "enc:v1:"

try:  # pragma: no cover - зависит от окружения
    from cryptography.fernet import Fernet, InvalidToken

    _AVAILABLE = True
    _IMPORT_ERROR = ""
except Exception as _exc:  # pragma: no cover
    # Сломанная сборка cryptography кидает не ImportError, а панику из Rust —
    # ловим широко, иначе приложение падает на импорте.
    Fernet = None  # type: ignore[assignment]
    InvalidToken = Exception  # type: ignore[assignment]
    _AVAILABLE = False
    _IMPORT_ERROR = str(_exc)


class SecretsUnavailable(RuntimeError):
    """Шифровать нечем или нечем расшифровать."""


def is_available() -> bool:
    return _AVAILABLE


def _fernet():
    if not _AVAILABLE:
        raise SecretsUnavailable(
            "пакет cryptography недоступен — выполните pip install -r requirements.txt"
            + (f" ({_IMPORT_ERROR})" if _IMPORT_ERROR else "")
        )
    secret = get_settings().secret_key
    if not secret:
        raise SecretsUnavailable("не задан AC_SECRET_KEY")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return Fernet(key)


def encrypt(value: str) -> str:
    """Шифрует строку. Пустая остаётся пустой — шифровать нечего."""
    if not value:
        return ""
    if value.startswith(_PREFIX):
        return value
    token = _fernet().encrypt(value.encode("utf-8")).decode("ascii")
    return _PREFIX + token


def decrypt(value: str) -> str:
    """Расшифровывает строку. Незашифрованную возвращает как есть.

    Это позволяет пережить обновление на базе, где секреты лежали открыто.
    """
    if not value:
        return ""
    if not value.startswith(_PREFIX):
        return value
    try:
        return _fernet().decrypt(value[len(_PREFIX):].encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise SecretsUnavailable(
            "не удалось расшифровать: AC_SECRET_KEY изменился с момента сохранения. "
            "Верните прежний ключ или введите мультитокены заново."
        ) from exc


def mask(value: str, keep: int = 6) -> str:
    """Хвост секрета для интерфейса и логов."""
    if not value:
        return "—"
    return f"…{value[-keep:]}" if len(value) > keep else "…"
