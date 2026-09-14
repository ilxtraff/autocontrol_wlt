"""Форматирование чисел и дат — ровно в том виде, в каком это показывает CRM."""
from __future__ import annotations

from datetime import datetime, timezone

UTC = timezone.utc


def money(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"${value:,.{digits}f}".replace(",", " ")


def rate(value: float | None) -> str:
    """uCPC — четыре знака, как в журнале решений."""
    if value is None:
        return "—"
    return f"${value:.4f}"


def integer(value: int | float | None) -> str:
    if value is None:
        return "—"
    return f"{int(value):,}".replace(",", " ")


def when(moment: datetime | None) -> str:
    """«14.09, 03:29»."""
    if moment is None:
        return "—"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.strftime("%d.%m, %H:%M")


def since(moment: datetime | None, now: datetime | None = None) -> str:
    """«2 ч 14 мин» — сколько адсет уже выключен."""
    if moment is None:
        return "—"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    now = now or datetime.now(UTC)
    delta = now - moment
    total = int(delta.total_seconds())
    if total < 0:
        return "—"
    hours, remainder = divmod(total, 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours} ч {minutes} мин"
    return f"{minutes} мин"
