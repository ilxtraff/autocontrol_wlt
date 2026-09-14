"""Сутки кабинета и их пересчёт в пояс трекера."""
from datetime import date, datetime, timezone

import pytest

from app.tzwindow import (
    UnknownTimezone, account_today, day_window, describe_offset, resolve_tz,
    to_tracker_range,
)

UTC = timezone.utc


def test_day_starts_at_midnight_in_account_timezone():
    """Кабинет в Лос-Анджелесе: сутки начинаются в 07:00 UTC (летом)."""
    now = datetime(2026, 9, 14, 8, 30, tzinfo=UTC)
    window = day_window("America/Los_Angeles", now=now)
    assert window.start_utc == datetime(2026, 9, 14, 7, 0, tzinfo=UTC)
    assert window.account_day == date(2026, 9, 14)
    assert window.is_partial


def test_keitaro_range_is_the_same_instant_in_moscow():
    """00:00 в Лос-Анджелесе — это 10:00 в Москве, туда и уходит запрос."""
    now = datetime(2026, 9, 14, 8, 30, tzinfo=UTC)
    window = day_window("America/Los_Angeles", now=now)
    assert to_tracker_range(window, "Europe/Moscow") == (
        "2026-09-14 10:00:00",
        "2026-09-14 11:30:00",
    )


def test_account_ahead_of_moscow():
    """Кабинет в GMT+5 уже в новых сутках, когда в Москве ещё вчера."""
    now = datetime(2026, 9, 14, 8, 30, tzinfo=UTC)
    window = day_window("GMT+5", now=now)
    start, end = to_tracker_range(window, "Europe/Moscow")
    assert start == "2026-09-13 22:00:00"
    assert end == "2026-09-14 11:30:00"


def test_gmt_offsets_and_iana_names_agree():
    now = datetime(2026, 9, 14, 8, 30, tzinfo=UTC)
    assert day_window("GMT-7", now=now).start_utc == day_window(
        "America/Los_Angeles", now=now
    ).start_utc


def test_past_day_is_a_full_window():
    now = datetime(2026, 9, 14, 8, 30, tzinfo=UTC)
    window = day_window("America/Los_Angeles", now=now, day=date(2026, 9, 12))
    assert not window.is_partial
    assert (window.end_utc - window.start_utc).total_seconds() == 24 * 3600


def test_dst_shift_is_handled():
    """В ночь перехода на зимнее время в сутках 25 часов — окно это учитывает."""
    now = datetime(2026, 11, 5, 0, 0, tzinfo=UTC)
    window = day_window("America/Los_Angeles", now=now, day=date(2026, 11, 1))
    assert (window.end_utc - window.start_utc).total_seconds() == 25 * 3600


def test_account_today_follows_account_clock():
    now = datetime(2026, 9, 14, 3, 0, tzinfo=UTC)  # 20:00 13-го в Лос-Анджелесе
    assert account_today("America/Los_Angeles", now=now) == date(2026, 9, 13)
    assert account_today("Europe/Moscow", now=now) == date(2026, 9, 14)


def test_unknown_timezone_is_rejected():
    with pytest.raises(UnknownTimezone):
        resolve_tz("Мордор/Барад-дур")
    with pytest.raises(UnknownTimezone):
        resolve_tz("")


def test_describe_offset_matches_crm_label():
    now = datetime(2026, 9, 14, 8, 30, tzinfo=UTC)
    assert describe_offset("GMT-7", now) == "GMT-7 · сейчас 01:30"
