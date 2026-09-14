"""Окно «рекламного дня».

Facebook считает день по часовому поясу рекламного кабинета, Keitaro отдаёт
статистику по своему (у нас — Москва). Автоконтроль должен идти от 00:00 по
таймзоне кабинета, поэтому день строится в таймзоне кабинета, а в Keitaro
уходит уже пересчитанным в его собственный пояс.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc

# Кабинеты в CRM подписаны как «GMT-7» / «GMT+5» — Facebook же отдаёт
# timezone_name вида America/Los_Angeles. Поддерживаем оба написания.
_GMT_RE = re.compile(r"^\s*(?:GMT|UTC)\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?\s*$", re.I)


class UnknownTimezone(ValueError):
    """Таймзона кабинета не распознана."""


def resolve_tz(name: str | None) -> ZoneInfo | timezone:
    """Превращает название таймзоны кабинета в объект tzinfo."""
    if not name or not name.strip():
        raise UnknownTimezone("у кабинета не задана таймзона")
    raw = name.strip()

    match = _GMT_RE.match(raw)
    if match:
        sign = 1 if match.group(1) == "+" else -1
        hours = int(match.group(2))
        minutes = int(match.group(3) or 0)
        if hours > 14 or minutes > 59:
            raise UnknownTimezone(f"некорректное смещение: {raw}")
        return timezone(sign * timedelta(hours=hours, minutes=minutes), raw.upper())

    try:
        return ZoneInfo(raw)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise UnknownTimezone(f"неизвестная таймзона кабинета: {raw}") from exc


@dataclass(frozen=True)
class DayWindow:
    """Сутки рекламного кабинета, выраженные в абсолютном времени."""

    account_day: date          # календарная дата в поясе кабинета
    account_tz_name: str
    start_utc: datetime        # 00:00 кабинета в UTC
    end_utc: datetime          # правая граница окна в UTC (now либо 24:00 кабинета)
    is_partial: bool           # True, если день ещё не закончился

    def contains(self, moment: datetime) -> bool:
        return self.start_utc <= _as_utc(moment) < self.end_utc


def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def account_today(tz_name: str, now: datetime | None = None) -> date:
    """Текущая календарная дата в поясе кабинета."""
    tz = resolve_tz(tz_name)
    now_utc = _as_utc(now or datetime.now(UTC))
    return now_utc.astimezone(tz).date()


def day_window(
    tz_name: str,
    now: datetime | None = None,
    day: date | None = None,
) -> DayWindow:
    """Окно от 00:00 по таймзоне кабинета.

    Для текущего дня правая граница — «сейчас»: статистика считается за уже
    прожитую часть суток. Для прошедшего дня берутся полные сутки.
    """
    tz = resolve_tz(tz_name)
    now_utc = _as_utc(now or datetime.now(UTC))
    local_now = now_utc.astimezone(tz)
    target = day or local_now.date()

    start_local = datetime.combine(target, datetime.min.time(), tzinfo=tz)
    next_local = datetime.combine(target + timedelta(days=1), datetime.min.time(), tzinfo=tz)
    start_utc = start_local.astimezone(UTC)
    end_of_day_utc = next_local.astimezone(UTC)

    if now_utc < end_of_day_utc:
        # День ещё идёт: правая граница — текущий момент, но не левее старта.
        end_utc = max(now_utc, start_utc)
        partial = True
    else:
        end_utc = end_of_day_utc
        partial = False

    return DayWindow(
        account_day=target,
        account_tz_name=tz_name,
        start_utc=start_utc,
        end_utc=end_utc,
        is_partial=partial,
    )


def to_tracker_range(window: DayWindow, tracker_tz_name: str) -> tuple[str, str]:
    """Границы окна как локальное время трекера: 'YYYY-MM-DD HH:MM:SS'.

    Момент времени не меняется — меняется только то, в каком поясе он записан.
    Именно это и нужно: Keitaro живёт по Москве, а день мы отмеряем по кабинету.
    """
    tracker_tz = resolve_tz(tracker_tz_name)
    fmt = "%Y-%m-%d %H:%M:%S"
    start = window.start_utc.astimezone(tracker_tz).strftime(fmt)
    end = window.end_utc.astimezone(tracker_tz).strftime(fmt)
    return start, end


def describe_offset(tz_name: str, now: datetime | None = None) -> str:
    """«GMT-7 · сейчас 18:38» — подпись кабинета, как в CRM."""
    try:
        tz = resolve_tz(tz_name)
    except UnknownTimezone:
        return "—"
    moment = _as_utc(now or datetime.now(UTC)).astimezone(tz)
    offset = moment.utcoffset() or timedelta(0)
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    hours, minutes = divmod(abs(total_minutes), 60)
    label = f"GMT{sign}{hours}" + (f":{minutes:02d}" if minutes else "")
    return f"{label} · сейчас {moment:%H:%M}"
