"""Hermetic tests for the Upstox holiday provider + calendar wiring.

Inject API rows so there's no network/disk I/O.
"""

from __future__ import annotations

from datetime import date

from papertrade_india import NSECalendar, UpstoxHolidayProvider

# Shape mirrors the real /v2/market/holidays response rows.
_ROWS = [
    {
        "date": "2026-01-26",
        "description": "Republic Day",
        "holiday_type": "TRADING_HOLIDAY",
        "closed_exchanges": ["NSE", "BSE", "NFO"],
    },
    {
        "date": "2026-03-06",
        "description": "BSE-only settlement day (example)",
        "holiday_type": "SETTLEMENT_HOLIDAY",
        "closed_exchanges": ["BSE"],  # NSE open this day
    },
    {
        "date": "2026-11-08",
        "description": "Diwali Muhurat (special session)",
        "holiday_type": "SPECIAL",
        "closed_exchanges": [],  # neither fully closed
    },
]


def test_closed_dates_filters_by_exchange() -> None:
    nse = UpstoxHolidayProvider(exchange="NSE", rows=_ROWS)
    assert nse.closed_dates() == {date(2026, 1, 26)}

    bse = UpstoxHolidayProvider(exchange="BSE", rows=_ROWS)
    assert bse.closed_dates() == {date(2026, 1, 26), date(2026, 3, 6)}


def test_bad_date_is_skipped() -> None:
    rows = [{"date": "not-a-date", "closed_exchanges": ["NSE"]}]
    assert UpstoxHolidayProvider(rows=rows).closed_dates() == set()


def test_calendar_uses_live_provider() -> None:
    provider = UpstoxHolidayProvider(exchange="NSE", rows=_ROWS)
    cal = NSECalendar(holiday_provider=provider)
    # Republic Day 2026 is a Monday — a weekday that must now be a holiday.
    assert cal.is_holiday(date(2026, 1, 26))
    assert not cal.is_trading_day(date(2026, 1, 26))
    # A normal weekday remains a trading day.
    assert cal.is_trading_day(date(2026, 1, 27))


def test_calendar_survives_provider_failure() -> None:
    class _Boom:
        def closed_dates(self):
            raise RuntimeError("api down")

    # Must not raise — bundled JSON remains the floor.
    cal = NSECalendar(holiday_provider=_Boom())
    assert cal.is_trading_day(date(2026, 1, 27))


def test_default_calendar_is_offline() -> None:
    # No provider → no network, bundled JSON only (hermetic default).
    cal = NSECalendar()
    assert isinstance(cal.is_trading_day(date(2026, 1, 27)), bool)
