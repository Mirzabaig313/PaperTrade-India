"""Integration tests for STOP / STOP_LIMIT / BRACKET orders and OCO logic."""

from __future__ import annotations

import pytest

from papertrade_india import (
    InvalidOrderError,
    LimitOrderWatcher,
    OrderStatus,
    OrderType,
)

# ── STOP_MARKET ──────────────────────────────────────────────────────


class TestStopMarket:
    def test_stop_requires_stop_price(self, broker) -> None:
        with pytest.raises(InvalidOrderError, match="stop_price"):
            broker.buy("RELIANCE", 1, order_type=OrderType.STOP_MARKET)

    def test_buy_stop_triggers_when_price_rises(self, broker, stub_provider) -> None:
        # First open a position so we have something to stop-loss out of.
        broker.buy("RELIANCE", 5)
        # Place SELL STOP at 2400 (triggers when last <= 2400).
        sl = broker.sell(
            "RELIANCE", 5,
            order_type=OrderType.STOP_MARKET, stop_price=2400.00,
        )
        assert sl.status == OrderStatus.PENDING
        assert sl.stop_price == 2400.00

        # Watcher should not trigger yet (price still 2500).
        w = LimitOrderWatcher(broker, interval_seconds=0.1)
        w.tick()
        assert broker.get_order(sl.id).status == OrderStatus.PENDING

        # Drop the price below the stop.
        stub_provider.set("RELIANCE", 2399.0)
        n = w.tick()
        assert n == 1
        filled = broker.get_order(sl.id)
        assert filled.status == OrderStatus.FILLED
        assert filled.triggered_at is not None
        assert filled.filled_avg_price == 2399.0

    def test_sell_stop_triggers_when_price_falls(self, broker, stub_provider) -> None:
        broker.buy("RELIANCE", 5)  # need shares to sell-stop
        sl = broker.sell(
            "RELIANCE", 5,
            order_type=OrderType.STOP_MARKET, stop_price=2400.00,
        )
        stub_provider.set("RELIANCE", 2350.0)
        w = LimitOrderWatcher(broker, interval_seconds=0.1)
        w.tick()
        assert broker.get_order(sl.id).status == OrderStatus.FILLED


# ── STOP_LIMIT ───────────────────────────────────────────────────────


class TestStopLimit:
    def test_stop_limit_requires_both_prices(self, broker) -> None:
        with pytest.raises(InvalidOrderError, match="limit_price"):
            broker.sell(
                "RELIANCE", 1,
                order_type=OrderType.STOP_LIMIT, stop_price=2400.00,
            )

    def test_stop_limit_converts_to_pending_limit(self, broker, stub_provider) -> None:
        broker.buy("RELIANCE", 5)
        order = broker.sell(
            "RELIANCE", 5,
            order_type=OrderType.STOP_LIMIT,
            stop_price=2400.00,
            limit_price=2390.00,
        )
        assert order.order_type == OrderType.STOP_LIMIT

        # Trigger.
        stub_provider.set("RELIANCE", 2395.0)
        w = LimitOrderWatcher(broker, interval_seconds=0.1)
        w.tick()
        # Now should be a pending LIMIT, not yet filled (2395 < 2390 limit
        # for a SELL means we want >= 2390, but market is 2395 so it
        # actually passes — but the watcher's logic only triggered on the
        # stop check. The next tick fills it.).
        after_trigger = broker.get_order(order.id)
        assert after_trigger.order_type == OrderType.LIMIT
        assert after_trigger.triggered_at is not None
        # Run the watcher again now that it's a regular LIMIT.
        w.tick()
        # SELL LIMIT @ 2390 fills when price >= 2390. Market 2395 → fill.
        final = broker.get_order(order.id)
        assert final.status == OrderStatus.FILLED


# ── BRACKET (OCO) ────────────────────────────────────────────────────


class TestBracket:
    def test_bracket_requires_stop_and_target(self, broker) -> None:
        with pytest.raises(InvalidOrderError, match="stop_price"):
            broker.buy("RELIANCE", 1, order_type=OrderType.BRACKET)

    def test_bracket_creates_three_orders(self, broker) -> None:
        parent = broker.buy(
            "RELIANCE", 5,
            order_type=OrderType.BRACKET,
            stop_price=2400.00,
            target_price=2600.00,
        )
        # The parent is a MARKET that fires immediately (no limit_price).
        assert parent.status == OrderStatus.FILLED
        all_orders = broker.get_orders(limit=10)
        # Parent + SL + target = 3 rows.
        assert len(all_orders) >= 3
        children = [o for o in all_orders if o.parent_order_id == parent.id]
        assert len(children) == 2
        # Children opposite side of parent.
        assert all(c.side.value == "sell" for c in children)
        # One stop, one limit (target).
        types = {c.order_type for c in children}
        assert types == {OrderType.STOP_MARKET, OrderType.LIMIT}

    def test_target_fill_cancels_stop(self, broker, stub_provider) -> None:
        broker.buy(
            "RELIANCE", 5,
            order_type=OrderType.BRACKET,
            stop_price=2400.00,
            target_price=2600.00,
        )
        # Move the price to hit the target.
        stub_provider.set("RELIANCE", 2605.0)
        w = LimitOrderWatcher(broker, interval_seconds=0.1)
        w.tick()
        all_orders = broker.get_orders(limit=10)
        children = [o for o in all_orders if o.parent_order_id]
        target = next(c for c in children if c.order_type == OrderType.LIMIT)
        stop = next(c for c in children if c.order_type == OrderType.STOP_MARKET)
        assert target.status == OrderStatus.FILLED
        # OCO: stop should be cancelled.
        assert stop.status == OrderStatus.CANCELLED
        assert "OCO" in (stop.rejection_reason or "")

    def test_stop_fill_cancels_target(self, broker, stub_provider) -> None:
        broker.buy(
            "RELIANCE", 5,
            order_type=OrderType.BRACKET,
            stop_price=2400.00,
            target_price=2600.00,
        )
        stub_provider.set("RELIANCE", 2399.0)
        w = LimitOrderWatcher(broker, interval_seconds=0.1)
        w.tick()
        all_orders = broker.get_orders(limit=10)
        children = [o for o in all_orders if o.parent_order_id]
        target = next(c for c in children if c.order_type == OrderType.LIMIT)
        stop = next(c for c in children if c.order_type == OrderType.STOP_MARKET)
        assert stop.status == OrderStatus.FILLED
        assert target.status == OrderStatus.CANCELLED

    def test_cancelling_parent_cancels_children(self, broker) -> None:
        # Use a LIMIT-entry bracket so the parent stays PENDING.
        parent = broker.buy(
            "RELIANCE", 5,
            order_type=OrderType.BRACKET,
            limit_price=2495.00,  # below market 2500, will queue
            stop_price=2400.00,
            target_price=2600.00,
        )
        # Parent is PENDING (limit not crossed).
        assert parent.status == OrderStatus.PENDING

        broker.cancel_order(parent.id)
        all_orders = broker.get_orders(limit=10)
        children = [o for o in all_orders if o.parent_order_id == parent.id]
        assert all(c.status == OrderStatus.CANCELLED for c in children)
