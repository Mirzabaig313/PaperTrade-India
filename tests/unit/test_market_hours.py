"""Unit tests for ``NSECalendar``."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest

from papertrade_india import IST, NSECalendar


@pytest.fixture
def cal_with_holidays(tmp_path) -> NSECalendar:
    """Calendar with a known holiday on 2026-01-26 (Republic Day, weekday)."""
    holidays_dir = tmp_path / "holidays"
    holidays_dir.mkdir()
    (holidays_dir / "nse_holidays_2026.json").write_text(
        json.dumps({
            "year": 2026,
            "holidays": ["2026-01-26", "2026-08-15"],
        })
    )
    return NSECalendar(holidays_dir=holidays_dir)


def test_weekend_is_not_trading_day(cal_with_holidays: NSECalendar):
    # 2026-01-24 is a Saturday, 2026-01-25 is a Sunday.
    assert cal_with_holidays.is_trading_day(date(2026, 1, 24)) is False
    assert cal_with_holidays.is_trading_day(date(2026, 1, 25)) is False


def test_weekday_is_trading_day(cal_with_holidays: NSECalendar):
    # 2026-01-27 is a Tuesday and not in the holiday list.
    assert cal_with_holidays.is_trading_day(date(2026, 1, 27)) is True


def test_explicit_holiday_is_not_trading_day(cal_with_holidays: NSECalendar):
    # Republic Day weekday holiday.
    assert cal_with_holidays.is_holiday(date(2026, 1, 26)) is True
    assert cal_with_holidays.is_trading_day(date(2026, 1, 26)) is False


def test_is_market_open_inside_hours(cal_with_holidays: NSECalendar):
    # 2026-01-27 10:00 IST — Tuesday, mid-session.
    dt = datetime(2026, 1, 27, 10, 0, tzinfo=IST)
    assert cal_with_holidays.is_market_open(dt) is True


def test_is_market_open_before_open(cal_with_holidays: NSECalendar):
    dt = datetime(2026, 1, 27, 9, 0, tzinfo=IST)
    assert cal_with_holidays.is_market_open(dt) is False


def test_is_market_open_after_close(cal_with_holidays: NSECalendar):
    dt = datetime(2026, 1, 27, 16, 0, tzinfo=IST)
    assert cal_with_holidays.is_market_open(dt) is False


def test_is_market_open_on_holiday(cal_with_holidays: NSECalendar):
    # Mid-session time, but it's a holiday.
    dt = datetime(2026, 1, 26, 10, 0, tzinfo=IST)
    assert cal_with_holidays.is_market_open(dt) is False


def test_naive_dt_is_assumed_ist(cal_with_holidays: NSECalendar):
    # 10:00 with no tzinfo → interpreted as IST.
    dt = datetime(2026, 1, 27, 10, 0)
    assert cal_with_holidays.is_market_open(dt) is True


def test_next_open_skips_weekends(cal_with_holidays: NSECalendar):
    # 2026-01-23 16:00 (Friday after close) → next open is Monday 09:15.
    dt = datetime(2026, 1, 23, 16, 0, tzinfo=IST)
    nxt = cal_with_holidays.next_open(dt)
    assert nxt.date() == date(2026, 1, 26) or nxt.date() == date(2026, 1, 27)
    # 2026-01-26 is the Republic Day holiday in our fixture, so skip to Tue.
    assert nxt.date() == date(2026, 1, 27)
    assert nxt.hour == 9 and nxt.minute == 15


def test_next_open_today_if_before_open(cal_with_holidays: NSECalendar):
    # 2026-01-27 06:00 — same trading day, before open.
    dt = datetime(2026, 1, 27, 6, 0, tzinfo=IST)
    nxt = cal_with_holidays.next_open(dt)
    assert nxt.date() == date(2026, 1, 27)
    assert nxt.hour == 9 and nxt.minute == 15


def test_missing_dir_loads_no_holidays(tmp_path: Path):
    missing = tmp_path / "does_not_exist"
    cal = NSECalendar(holidays_dir=missing)
    # Weekday Republic Day still treated as trading day (we have no data).
    assert cal.is_trading_day(date(2026, 1, 26)) is True


def test_malformed_file_does_not_break(tmp_path: Path):
    holidays_dir = tmp_path / "holidays"
    holidays_dir.mkdir()
    (holidays_dir / "nse_holidays_2026.json").write_text("not json")
    # Loading a malformed file logs a warning but doesn't raise.
    cal = NSECalendar(holidays_dir=holidays_dir)
    assert cal.is_trading_day(date(2026, 1, 27)) is True


def test_bundled_calendar_loads_holidays():
    """The packaged 2026 holidays file should produce a non-empty set."""
    cal = NSECalendar()
    # We don't pin specific dates here — those move year to year — but
    # verify the loader did its job for the bundled file.
    assert cal.is_holiday(date(2026, 1, 26)) is True


# ── Bundled holiday data smoke tests ──────────────────────────────────


def test_bundled_2026_holiday_dates():
    """Spot-check several known 2026 weekday holidays load correctly.

    A bad refresh PR that drops, mistypes, or scrambles the JSON should
    fail this test loudly. Update the list when the bundled file changes.
    Note: 2026-08-15 (Independence Day) is a Saturday and not in the
    bundled list — weekends are already non-trading days.
    """
    cal = NSECalendar()
    expected_2026 = [
        date(2026, 1, 26),   # Republic Day (Mon)
        date(2026, 5, 1),    # Maharashtra Day (Fri)
        date(2026, 10, 2),   # Gandhi Jayanti (Fri)
        date(2026, 12, 25),  # Christmas (Fri)
    ]
    for d in expected_2026:
        assert cal.is_holiday(d), f"missing 2026 holiday: {d}"
        assert cal.is_trading_day(d) is False


def test_bundled_2027_holiday_dates():
    cal = NSECalendar()
    # Republic Day 2027 falls on a Tuesday and is a fixed-date holiday;
    # 2027-05-01 / 2027-08-15 / 2027-10-02 / 2027-12-25 are all weekends
    # and excluded.
    expected_2027 = [
        date(2027, 1, 26),
    ]
    for d in expected_2027:
        assert cal.is_holiday(d), f"missing 2027 holiday: {d}"


def test_bundled_data_has_no_weekend_holidays():
    """Real NSE holiday lists never contain Saturdays/Sundays — those are
    already non-trading days. If a refresh PR adds one, flag it."""
    cal = NSECalendar()
    weekend_holidays = [
        d for d in cal._holidays if d.weekday() >= 5
    ]
    assert weekend_holidays == [], (
        f"bundled holiday list contains weekends: {weekend_holidays}"
    )
