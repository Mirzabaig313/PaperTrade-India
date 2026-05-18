"""Unit tests for the Clock abstraction."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from papertrade_india import IST, ReplayClock, WallClock


def test_wallclock_returns_aware_datetime():
    clk = WallClock()
    now = clk.now()
    assert now.tzinfo is not None


def test_wallclock_uses_ist_by_default():
    clk = WallClock()
    assert clk.now().tzinfo == IST


def test_replayclock_starts_at_supplied_time():
    start = datetime(2026, 5, 18, 9, 30, tzinfo=IST)
    c = ReplayClock(start)
    assert c.now() == start


def test_replayclock_naive_input_coerced_to_ist():
    naive = datetime(2026, 5, 18, 9, 30)
    c = ReplayClock(naive)
    # tzinfo is set; the wall-clock value is unchanged.
    assert c.now().tzinfo == IST
    assert c.now().hour == 9


def test_replayclock_advance_moves_forward():
    c = ReplayClock(datetime(2026, 5, 18, 10, 0, tzinfo=IST))
    c.advance(timedelta(hours=2, minutes=15))
    assert c.now() == datetime(2026, 5, 18, 12, 15, tzinfo=IST)


def test_replayclock_advance_negative_raises():
    c = ReplayClock(datetime(2026, 5, 18, 10, 0, tzinfo=IST))
    with pytest.raises(ValueError, match="non-negative"):
        c.advance(timedelta(seconds=-1))


def test_replayclock_advance_zero_is_ok():
    c = ReplayClock(datetime(2026, 5, 18, 10, 0, tzinfo=IST))
    c.advance(timedelta(0))
    assert c.now() == datetime(2026, 5, 18, 10, 0, tzinfo=IST)


def test_replayclock_set_to_future_works():
    c = ReplayClock(datetime(2026, 5, 18, 10, 0, tzinfo=IST))
    c.set(datetime(2026, 5, 18, 11, 0, tzinfo=IST))
    assert c.now().hour == 11


def test_replayclock_set_to_past_raises():
    c = ReplayClock(datetime(2026, 5, 18, 10, 0, tzinfo=IST))
    with pytest.raises(ValueError, match="cannot move backwards"):
        c.set(datetime(2026, 5, 18, 9, 0, tzinfo=IST))


def test_replayclock_set_naive_coerced():
    c = ReplayClock(datetime(2026, 5, 18, 10, 0, tzinfo=IST))
    c.set(datetime(2026, 5, 18, 11, 0))  # naive
    assert c.now().tzinfo == IST
