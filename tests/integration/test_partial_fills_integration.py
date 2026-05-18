"""Integration tests for partial fills against a real broker + watcher."""

from __future__ import annotations

import pytest

from papertrade_india import (
    IndiaPaperBroker,
    LimitOrderWatcher,
    OrderStatus,
    OrderType,
    PartialFillConfig,
)

pytestmark = pytest.mark.integration


def _broker_with_partials(tmp_path, price_feed, cap: int):
    return IndiaPaperBroker(
        initial_capital=10_000_000,
        db_path=tmp_path / "partial.db",
        account_id="partial",
        price_feed=price_feed,
        partial_fill_config=PartialFillConfig(enabled=True, max_per_tick=cap),
        enforce_market_hours=False,
    )


def test_limit_buy_partially_fills_across_ticks(tmp_path, price_feed, stub_provider):
    """A 10-share buy with cap=3 fills 3 + 3 + 3 + 1 over 4 ticks."""
    stub_provider.set("RELIANCE", 1000)
    broker = _broker_with_partials(tmp_path, price_feed, cap=3)

    order = broker.buy(
        "RELIANCE", 10,
        order_type=OrderType.LIMIT, limit_price=1000.0,
    )
    assert order.status == OrderStatus.PENDING

    watcher = LimitOrderWatcher(broker, interval_seconds=999)

    # Tick 1: fills 3 shares. Status flips to PARTIALLY_FILLED.
    fills = watcher.tick()
    assert fills == 1
    after = broker.get_order(order.id)
    assert after.status == OrderStatus.PARTIALLY_FILLED
    assert after.filled_qty == 3
    pos = broker.get_position("RELIANCE")
    assert pos is not None and pos.qty == 3

    # Tick 2 + 3: fills 3 + 3.
    watcher.tick()
    watcher.tick()
    after = broker.get_order(order.id)
    assert after.filled_qty == 9
    assert after.status == OrderStatus.PARTIALLY_FILLED

    # Tick 4: fills the last 1 share. Order is FILLED.
    watcher.tick()
    after = broker.get_order(order.id)
    assert after.status == OrderStatus.FILLED
    assert after.filled_qty == 10
    assert broker.get_position("RELIANCE").qty == 10


def test_partial_fill_avg_price_is_volume_weighted(
    tmp_path, price_feed, stub_provider,
):
    """When the price moves between slices, ``filled_avg_price`` should
    be volume-weighted across slices.

    Concretely: 4-share buy at limit 1000, cap=2.
      - Tick 1: market at 1000, fill 2 @ 1000.
      - Tick 2: market drops to 950 (still <= limit). The watcher's
        cross-check (``price <= limit_price``) is satisfied at 950, so
        the slice fills at 950. Volume-weighted avg: (2*1000 + 2*950)/4 = 975.
    """
    stub_provider.set("RELIANCE", 1000)
    broker = _broker_with_partials(tmp_path, price_feed, cap=2)

    order = broker.buy(
        "RELIANCE", 4,
        order_type=OrderType.LIMIT, limit_price=1000.0,
    )

    watcher = LimitOrderWatcher(broker, interval_seconds=999)
    watcher.tick()  # fill 2 @ 1000

    stub_provider.set("RELIANCE", 950)
    watcher.tick()  # fill 2 @ 950 (price below limit; watcher fills at last)

    after = broker.get_order(order.id)
    assert after.status == OrderStatus.FILLED
    assert after.filled_qty == 4
    # Volume-weighted across the two slices.
    assert after.filled_avg_price == pytest.approx(975.0)


def test_cancel_partially_filled_order(tmp_path, price_feed, stub_provider):
    """A PARTIALLY_FILLED order can be cancelled. Already-filled qty
    stays in the position; the rest is dropped."""
    stub_provider.set("RELIANCE", 1000)
    broker = _broker_with_partials(tmp_path, price_feed, cap=3)

    order = broker.buy(
        "RELIANCE", 10,
        order_type=OrderType.LIMIT, limit_price=1000.0,
    )
    LimitOrderWatcher(broker, interval_seconds=999).tick()
    assert broker.get_order(order.id).status == OrderStatus.PARTIALLY_FILLED

    # Cancel mid-way.
    assert broker.cancel_order(order.id) is True

    after = broker.get_order(order.id)
    assert after.status == OrderStatus.CANCELLED
    assert after.filled_qty == 3  # what was already filled stays
    pos = broker.get_position("RELIANCE")
    assert pos is not None and pos.qty == 3  # 7 unfilled shares dropped


def test_partial_fills_preserve_cash_invariant(
    tmp_path, price_feed, stub_provider,
):
    """The cash ledger invariant must hold across all partial fills."""
    stub_provider.set("INFY", 1500)
    broker = _broker_with_partials(tmp_path, price_feed, cap=2)
    broker.buy(
        "INFY", 8,
        order_type=OrderType.LIMIT, limit_price=1500.0,
    )

    watcher = LimitOrderWatcher(broker, interval_seconds=999)
    for _ in range(4):
        watcher.tick()

    assert broker.verify_cash_invariant()


def test_disabled_config_falls_back_to_full_fill(
    tmp_path, price_feed, stub_provider,
):
    """``PartialFillConfig(enabled=False)`` keeps the legacy all-or-
    nothing behavior."""
    stub_provider.set("RELIANCE", 1000)
    broker = IndiaPaperBroker(
        initial_capital=1_000_000,
        db_path=tmp_path / "nopart.db",
        account_id="nopart",
        price_feed=price_feed,
        partial_fill_config=PartialFillConfig(enabled=False),
        enforce_market_hours=False,
    )
    broker.buy(
        "RELIANCE", 5,
        order_type=OrderType.LIMIT, limit_price=1000.0,
    )
    LimitOrderWatcher(broker, interval_seconds=999).tick()
    pos = broker.get_position("RELIANCE")
    assert pos.qty == 5  # full fill in one tick
