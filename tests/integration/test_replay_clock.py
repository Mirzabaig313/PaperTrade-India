"""Integration tests: ``IndiaPaperBroker`` with a ``ReplayClock``.

A backtest needs the broker to use a deterministic clock so that:
  - Order timestamps are predictable.
  - Market-hour and session-phase checks reflect the simulated time.
  - Limit-order watcher ticks can be driven synchronously.
  - Idempotency cleanup TTLs are testable without sleeping.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from papertrade_india import (
    IST,
    IndiaPaperBroker,
    LimitOrderWatcher,
    OrderType,
    PriceFeed,
    ReplayClock,
)

pytestmark = pytest.mark.integration


def _make_broker(tmp_path, stub_provider, clock=None, **overrides):
    feed = PriceFeed(providers=[stub_provider], short_cache_ttl_seconds=0)
    return IndiaPaperBroker(
        initial_capital=overrides.pop("initial_capital", 1_000_000),
        db_path=tmp_path / overrides.pop("dbname", "replay.db"),
        account_id=overrides.pop("account_id", "replay"),
        price_feed=feed,
        clock=clock,
        **overrides,
    )


def test_order_timestamps_use_replay_clock(tmp_path, stub_provider):
    """Order created_at should be the replay clock, not wall time."""
    start = datetime(2026, 5, 18, 10, 0, tzinfo=IST)
    clock = ReplayClock(start)
    broker = _make_broker(
        tmp_path, stub_provider, clock=clock,
        enforce_market_hours=False,
    )
    stub_provider.set("RELIANCE", 1000)
    order = broker.buy("RELIANCE", 1)
    # The order's created_at should equal the replay clock's value.
    assert order.created_at == start


def test_advancing_clock_changes_timestamps(tmp_path, stub_provider):
    """Two orders separated by an explicit clock advance should have
    different created_at."""
    start = datetime(2026, 5, 18, 10, 0, tzinfo=IST)
    clock = ReplayClock(start)
    broker = _make_broker(
        tmp_path, stub_provider, clock=clock,
        enforce_market_hours=False,
    )
    stub_provider.set("RELIANCE", 1000)

    o1 = broker.buy("RELIANCE", 1)
    clock.advance(timedelta(minutes=5))
    o2 = broker.buy("RELIANCE", 1)

    assert o1.created_at == start
    assert o2.created_at == start + timedelta(minutes=5)


def test_market_hours_respect_replay_clock(tmp_path, stub_provider):
    """A buy at 09:00 (PRE_OPEN) should be rejected; the same buy 30
    minutes later (REGULAR) should fill."""
    from papertrade_india import MarketClosedError

    pre_open = datetime(2026, 5, 19, 9, 0, tzinfo=IST)  # Tuesday
    clock = ReplayClock(pre_open)
    broker = _make_broker(
        tmp_path, stub_provider, clock=clock,
        enforce_market_hours=True,
    )
    stub_provider.set("RELIANCE", 1000)

    with pytest.raises(MarketClosedError, match="pre_open"):
        broker.buy("RELIANCE", 1)

    # Move into REGULAR.
    clock.advance(timedelta(minutes=30))
    order = broker.buy("RELIANCE", 1)
    assert order.filled_avg_price is not None


def test_session_phase_reflects_replay_clock(tmp_path, stub_provider):
    from papertrade_india import SessionPhase

    clock = ReplayClock(datetime(2026, 5, 19, 9, 5, tzinfo=IST))
    broker = _make_broker(
        tmp_path, stub_provider, clock=clock,
        enforce_market_hours=False,
    )
    assert broker.current_session_phase() == SessionPhase.PRE_OPEN

    clock.set(datetime(2026, 5, 19, 12, 0, tzinfo=IST))
    assert broker.current_session_phase() == SessionPhase.REGULAR

    clock.set(datetime(2026, 5, 19, 15, 50, tzinfo=IST))
    assert broker.current_session_phase() == SessionPhase.POST_CLOSE


def test_replay_drives_limit_watcher_ticks(tmp_path, stub_provider):
    """A limit order placed in replay mode fills when the watcher is
    ticked manually after a price change — no wall-clock waiting."""
    clock = ReplayClock(datetime(2026, 5, 19, 12, 0, tzinfo=IST))
    broker = _make_broker(
        tmp_path, stub_provider, clock=clock,
        enforce_market_hours=False,
    )
    stub_provider.set("RELIANCE", 2500)
    broker.buy(
        "RELIANCE", 1,
        order_type=OrderType.LIMIT, limit_price=2400.0,
    )

    watcher = LimitOrderWatcher(broker, interval_seconds=999)
    # Above the limit — no fill.
    assert watcher.tick() == 0

    # Drop the price below the limit and tick again.
    stub_provider.set("RELIANCE", 2400)
    clock.advance(timedelta(seconds=5))
    assert watcher.tick() == 1


def test_dst_irrelevant_ist_consistent(tmp_path, stub_provider):
    """IST doesn't observe DST — replay clock advancing across what
    would be a DST boundary in other timezones produces consistent
    IST timestamps."""
    spring_fwd_us_eq = datetime(2026, 3, 8, 10, 0, tzinfo=IST)
    clock = ReplayClock(spring_fwd_us_eq)
    broker = _make_broker(
        tmp_path, stub_provider, clock=clock,
        enforce_market_hours=False,
    )
    stub_provider.set("RELIANCE", 1000)
    o1 = broker.buy("RELIANCE", 1)
    clock.advance(timedelta(hours=1))
    o2 = broker.buy("RELIANCE", 1)
    assert o2.created_at - o1.created_at == timedelta(hours=1)


def test_idempotency_keys_clean_up_against_replay_clock(
    tmp_path, stub_provider,
):
    """Idempotency cleanup uses ``cleanup_expired(now=...)`` — for
    backtests we want the cleanup to honor replay time."""
    from papertrade_india import idempotency as _idempotency

    clock = ReplayClock(datetime(2026, 5, 19, 12, 0, tzinfo=IST))
    broker = _make_broker(
        tmp_path, stub_provider, clock=clock,
        enforce_market_hours=False,
    )
    stub_provider.set("RELIANCE", 1000)
    broker.buy("RELIANCE", 1, idempotency_key="bt-1")

    # Advance the wall clock conceptually — we still have to call cleanup
    # ourselves because `cleanup_idempotency_keys` defaults to wall now.
    # We pass the replay-clock now via the underlying helper.
    with broker.persistence.transaction() as conn:
        n = _idempotency.cleanup_expired(
            conn, ttl=timedelta(hours=1), now=clock.now() + timedelta(hours=2),
        )
    assert n == 1


def test_clock_property_exposes_the_clock(tmp_path, stub_provider):
    clock = ReplayClock(datetime(2026, 5, 19, 12, 0, tzinfo=IST))
    broker = _make_broker(
        tmp_path, stub_provider, clock=clock,
        enforce_market_hours=False,
    )
    assert broker.clock is clock
