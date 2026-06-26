"""Integration tests: T+1 settlement + intraday auto-square-off."""

from __future__ import annotations

from datetime import datetime, time

import pytest

from papertrade_india import (
    InsufficientSharesError,
    LimitOrderWatcher,
    ProductType,
    SettlementConfig,
    SettlementMode,
)
from papertrade_india.infrastructure.clock import ReplayClock


@pytest.fixture()
def t1_broker(tmp_path, price_feed):
    """A broker in T+1 mode with a controlled clock."""
    from papertrade_india import IndiaPaperBroker
    clock = ReplayClock(start_at=datetime(2026, 5, 22, 10, 0, 0))
    return IndiaPaperBroker(
        initial_capital=1_000_000,
        db_path=tmp_path / "t1.db",
        price_feed=price_feed,
        enforce_market_hours=False,
        settlement_config=SettlementConfig(mode=SettlementMode.T_PLUS_1),
        clock=clock,
    )


# ── T+1 BUY blocks same-day SELL (delivery) ─────────────────────────


def test_buy_then_sell_same_day_blocked_under_t1(t1_broker) -> None:
    t1_broker.buy("RELIANCE", 10)
    # Default product = DELIVERY.
    with pytest.raises(InsufficientSharesError, match="deliverable"):
        t1_broker.sell("RELIANCE", 10)


def test_buy_then_sell_intraday_allowed(t1_broker) -> None:
    t1_broker.buy("RELIANCE", 10, product_type=ProductType.INTRADAY)
    # Intraday round-trip is allowed even on same day.
    order = t1_broker.sell(
        "RELIANCE", 10, product_type=ProductType.INTRADAY,
    )
    assert order.filled_qty == 10


def test_partial_sell_within_deliverable_qty(t1_broker) -> None:
    # Open with 20, then buy another 10 today (in flight).
    t1_broker.buy("RELIANCE", 20)
    # Settle T+1 manually so the 20 is deliverable.
    t1_broker.clock.set(datetime(2026, 5, 26, 9, 30))
    t1_broker.settle_due()
    # Buy 10 more today (T+1 in flight).
    t1_broker.buy("RELIANCE", 10)
    # We hold 30, only 20 deliverable.
    order = t1_broker.sell("RELIANCE", 20)  # OK
    assert order.filled_qty == 20
    with pytest.raises(InsufficientSharesError):
        t1_broker.sell("RELIANCE", 5)  # nothing left after the 20


# ── Settle-due rolls pending rows ─────────────────────────────────


def test_settle_due_clears_pending(t1_broker) -> None:
    t1_broker.buy("RELIANCE", 5)
    with t1_broker.persistence.read() as conn:
        rows_before = conn.execute(
            "SELECT COUNT(*) AS n FROM pending_settlements WHERE status='pending'",
        ).fetchone()["n"]
    assert rows_before == 1

    # Move clock forward past T+1.
    t1_broker.clock.set(datetime(2026, 5, 26, 10, 0))
    n = t1_broker.settle_due()
    assert n == 1

    with t1_broker.persistence.read() as conn:
        rows_after = conn.execute(
            "SELECT COUNT(*) AS n FROM pending_settlements WHERE status='pending'",
        ).fetchone()["n"]
    assert rows_after == 0


# ── Auto-square-off intraday ─────────────────────────────────────


def test_square_off_closes_intraday_positions(tmp_path, price_feed) -> None:
    from papertrade_india import IndiaPaperBroker
    clock = ReplayClock(start_at=datetime(2026, 5, 22, 10, 0))
    broker = IndiaPaperBroker(
        initial_capital=1_000_000,
        db_path=tmp_path / "intraday.db",
        price_feed=price_feed,
        enforce_market_hours=False,
        settlement_config=SettlementConfig(
            mode=SettlementMode.T_PLUS_1,
            auto_square_off_at=time(15, 15),
        ),
        clock=clock,
    )
    broker.buy("RELIANCE", 5, product_type=ProductType.INTRADAY)
    broker.buy("INFY", 3, product_type=ProductType.INTRADAY)
    # Plus a delivery position that should NOT get squared off.
    broker.buy("TCS", 2, product_type=ProductType.DELIVERY)

    n = broker.square_off_intraday()
    assert n == 2  # RELIANCE + INFY

    positions = {p.symbol for p in broker.get_positions()}
    assert "RELIANCE" not in positions
    assert "INFY" not in positions
    assert "TCS" in positions  # delivery untouched


def test_watcher_auto_square_off_at_clock_time(
    tmp_path, price_feed,
) -> None:
    from papertrade_india import IndiaPaperBroker
    clock = ReplayClock(start_at=datetime(2026, 5, 22, 14, 0))
    broker = IndiaPaperBroker(
        initial_capital=1_000_000,
        db_path=tmp_path / "watcher.db",
        price_feed=price_feed,
        enforce_market_hours=False,
        settlement_config=SettlementConfig(
            mode=SettlementMode.T_PLUS_1,
            auto_square_off_at=time(15, 15),
        ),
        clock=clock,
    )
    broker.buy("RELIANCE", 5, product_type=ProductType.INTRADAY)

    w = LimitOrderWatcher(
        broker, interval_seconds=0.1, auto_square_off_intraday=True,
    )
    # Pre-square-off: tick should not close.
    w.tick()
    assert any(p.symbol == "RELIANCE" for p in broker.get_positions())

    # Move clock past 15:15.
    clock.set(datetime(2026, 5, 22, 15, 16))
    w.tick()
    assert not any(p.symbol == "RELIANCE" for p in broker.get_positions())
