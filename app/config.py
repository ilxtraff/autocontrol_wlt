"""Настройки приложения. Всё читается из окружения с разумными дефолтами."""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from functools import lru_cache


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "да"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _load_dotenv() -> None:
    """Мини-загрузчик .env, чтобы не тащить зависимость ради пяти строк."""
    path = os.getenv("AC_ENV_FILE", ".env")
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


@dataclass(frozen=True)
class Settings:
    secret_key: str
    database_url: str
    bind_host: str
    bind_port: int

    bootstrap_login: str
    bootstrap_password: str
    bootstrap_role: str

    tick_seconds: int
    recovery_hours: int
    run_engine_in_web: bool
    human_resume_check: bool
    dry_run: bool

    # Движок сам находит новые адсеты в кабинетах и ставит их под контроль.
    auto_import_adsets: bool
    auto_import_minutes: int
    auto_import_all_statuses: bool

    keitaro_timezone: str
    keitaro_adset_field: str
    keitaro_timeout: int

    fb_api_version: str
    fb_timeout: int

    # За HTTPS-прокси куку стоит отдавать только по защищённому соединению.
    cookie_secure: bool = field(default=False)

    # Секунды. Окно, в котором решение по адсету не повторяется (защита от дребезга).
    decision_cooldown: int = field(default=300)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    _load_dotenv()
    return Settings(
        secret_key=os.getenv("AC_SECRET_KEY") or secrets.token_urlsafe(48),
        database_url=os.getenv("AC_DATABASE_URL", "sqlite:///./autocontrol.db"),
        bind_host=os.getenv("AC_BIND_HOST", "0.0.0.0"),
        bind_port=_int("AC_BIND_PORT", 8000),
        bootstrap_login=os.getenv("AC_BOOTSTRAP_LOGIN", "admin"),
        bootstrap_password=os.getenv("AC_BOOTSTRAP_PASSWORD", ""),
        bootstrap_role=os.getenv("AC_BOOTSTRAP_ROLE", "admin"),
        tick_seconds=_int("AC_TICK_SECONDS", 600),
        recovery_hours=_int("AC_RECOVERY_HOURS", 3),
        run_engine_in_web=_bool("AC_RUN_ENGINE_IN_WEB", True),
        human_resume_check=_bool("AC_HUMAN_RESUME_CHECK", True),
        dry_run=_bool("AC_DRY_RUN", False),
        auto_import_adsets=_bool("AC_AUTO_IMPORT_ADSETS", True),
        auto_import_minutes=_int("AC_AUTO_IMPORT_MINUTES", 60),
        auto_import_all_statuses=_bool("AC_AUTO_IMPORT_ALL_STATUSES", False),
        keitaro_timezone=os.getenv("AC_KEITARO_TIMEZONE", "Europe/Moscow"),
        keitaro_adset_field=os.getenv("AC_KEITARO_ADSET_FIELD", "sub_id_6"),
        keitaro_timeout=_int("AC_KEITARO_TIMEOUT", 45),
        fb_api_version=os.getenv("AC_FB_API_VERSION", "v21.0"),
        fb_timeout=_int("AC_FB_TIMEOUT", 45),
        cookie_secure=_bool("AC_COOKIE_SECURE", False),
    )
