"""Tests for the SessionPhase enum and NSECalendar.current_phase()."""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from papertrade_india import IST, NSECalendar, SessionPhase


@pytest.fixture
def cal_no_holidays(tmp_path):
    """Calendar with no holidays loaded — date 2026-01-27 (Tuesday) is
    therefore a clean trading day."""
    holidays_dir = tmp_path / "holidays"
    holidays_dir.mkdir()
    (holidays_dir / "nse_holidays_2026.json").write_text(
        json.dumps({"year": 2026, "holidays": []})
    )
    return NSECalendar(holidays_dir=holidays_dir)


def _at(h: int, m: int, cal: NSECalendar) -> SessionPhase:
    """Return the phase at 2026-01-27 (Tue) HH:MM IST."""
    dt = datetime(2026, 1, 27, h, m, tzinfo=IST)
    return cal.current_phase(dt)


def test_pre_open_window(cal_no_holidays):
    assert _at(9, 0, cal_no_holidays) == SessionPhase.PRE_OPEN
    assert _at(9, 5, cal_no_holidays) == SessionPhase.PRE_OPEN
    assert _at(9, 8, cal_no_holidays) == SessionPhase.PRE_OPEN


def test_gap_between_pre_open_and_regular(cal_no_holidays):
    """09:09–09:14 is neither pre-open nor regular."""
    assert _at(9, 9, cal_no_holidays) == SessionPhase.CLOSED
    assert _at(9, 14, cal_no_holidays) == SessionPhase.CLOSED


def test_regular_window(cal_no_holidays):
    assert _at(9, 15, cal_no_holidays) == SessionPhase.REGULAR
    assert _at(12, 0, cal_no_holidays) == SessionPhase.REGULAR
    assert _at(15, 30, cal_no_holidays) == SessionPhase.REGULAR


def test_gap_between_regular_and_post_close(cal_no_holidays):
    """15:31–15:39 is in the gap."""
    assert _at(15, 31, cal_no_holidays) == SessionPhase.CLOSED
    assert _at(15, 39, cal_no_holidays) == SessionPhase.CLOSED


def test_post_close_window(cal_no_holidays):
    assert _at(15, 40, cal_no_holidays) == SessionPhase.POST_CLOSE
    assert _at(15, 50, cal_no_holidays) == SessionPhase.POST_CLOSE
    assert _at(16, 0, cal_no_holidays) == SessionPhase.POST_CLOSE


def test_after_post_close(cal_no_holidays):
    assert _at(16, 1, cal_no_holidays) == SessionPhase.CLOSED
    assert _at(20, 0, cal_no_holidays) == SessionPhase.CLOSED


def test_weekend_is_closed(cal_no_holidays):
    # 2026-01-25 is a Sunday.
    sunday = datetime(2026, 1, 25, 12, 0, tzinfo=IST)
    assert cal_no_holidays.current_phase(sunday) == SessionPhase.CLOSED


def test_holiday_is_closed_during_what_would_be_regular_hours(tmp_path):
    holidays_dir = tmp_path / "holidays"
    holidays_dir.mkdir()
    (holidays_dir / "nse_holidays_2026.json").write_text(
        json.dumps({"year": 2026, "holidays": ["2026-01-26"]})
    )
    cal = NSECalendar(holidays_dir=holidays_dir)
    monday = datetime(2026, 1, 26, 12, 0, tzinfo=IST)
    assert cal.current_phase(monday) == SessionPhase.CLOSED


def test_is_market_open_only_during_regular(cal_no_holidays):
    """Backwards compat: ``is_market_open`` returns True only during the
    REGULAR phase."""
    assert cal_no_holidays.is_market_open(datetime(2026, 1, 27, 9, 0, tzinfo=IST)) is False
    assert cal_no_holidays.is_market_open(datetime(2026, 1, 27, 9, 15, tzinfo=IST)) is True
    assert cal_no_holidays.is_market_open(datetime(2026, 1, 27, 15, 30, tzinfo=IST)) is True
    assert cal_no_holidays.is_market_open(datetime(2026, 1, 27, 15, 45, tzinfo=IST)) is False


def test_broker_current_session_phase(broker, monkeypatch):
    """Broker exposes its calendar's current phase."""
    # We can't pin wall-clock time without monkeypatching, so verify
    # the wiring with a stub calendar.
    class StubCal:
        def current_phase(self, dt=None):
            return SessionPhase.PRE_OPEN

        def is_market_open(self, dt=None):
            return False

        def next_open(self, dt=None):
            return datetime(2026, 1, 27, 9, 15, tzinfo=IST)

        def is_trading_day(self, d):
            return True

    broker.calendar = StubCal()
    assert broker.current_session_phase() == SessionPhase.PRE_OPEN



# ── Phase-aware MarketClosedError ────────────────────────────────────


def test_market_closed_error_mentions_current_phase(tmp_path, price_feed):
    """The error raised on a closed market should name the actual phase
    (PRE_OPEN / POST_CLOSE / CLOSED), so an autonomous agent can tell
    "wait 7 minutes for REGULAR" from "wait until tomorrow"."""
    from papertrade_india import (
        IndiaPaperBroker,
        MarketClosedError,
        NSECalendar,
        SessionPhase,
    )

    class StubCal(NSECalendar):
        def __init__(self):
            super().__init__()

        def is_market_open(self, dt=None):
            return False

        def current_phase(self, dt=None):
            return SessionPhase.PRE_OPEN

    broker = IndiaPaperBroker(
        initial_capital=100_000,
        db_path=tmp_path / "phase.db",
        account_id="phase",
        price_feed=price_feed,
        calendar=StubCal(),
        enforce_market_hours=True,
    )
    with pytest.raises(MarketClosedError, match="pre_open"):
        broker.buy("RELIANCE", 1)
