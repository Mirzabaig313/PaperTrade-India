"""Integration tests: AMO, GTT, pre-open auction, margin/pledge guard."""

from __future__ import annotations

from datetime import datetime

import pytest

from papertrade_india import (
    AMOWindowClosedError,
    IndiaPaperBroker,
    LimitOrderWatcher,
    MarginNotSupported,
    OrderStatus,
    OrderType,
    ProductType,
    SettlementConfig,
    SettlementMode,
)
from papertrade_india.clock import ReplayClock
from papertrade_india.market_hours import IST


def _broker_with_clock(tmp_path, price_feed, clock):
    """Broker with market-hours enforcement so AMO/pre-open windows
    actually have meaning. T+0 to keep the test surface narrow."""
    return IndiaPaperBroker(
        initial_capital=1_000_000,
        db_path=tmp_path / "amo.db",
        price_feed=price_feed,
        enforce_market_hours=True,
        clock=clock,
        settlement_config=SettlementConfig(mode=SettlementMode.T_PLUS_0),
    )


# ── Margin / Pledge guard ────────────────────────────────────────────


class TestMarginGuard:
    def test_margin_product_rejected(self, broker) -> None:
        with pytest.raises(MarginNotSupported, match="margin"):
            broker.buy("RELIANCE", 1, product_type=ProductType.MARGIN)

    def test_pledge_product_rejected(self, broker) -> None:
        with pytest.raises(MarginNotSupported, match="pledge"):
            broker.buy("RELIANCE", 1, product_type=ProductType.PLEDGE)

    def test_delivery_and_intraday_still_work(self, broker) -> None:
        broker.buy("RELIANCE", 1, product_type=ProductType.DELIVERY)
        broker.buy("RELIANCE", 1, product_type=ProductType.INTRADAY)


# ── AMO ──────────────────────────────────────────────────────────────


class TestAMO:
    def test_amo_inside_regular_session_rejected(self, tmp_path, price_feed) -> None:
        clock = ReplayClock(datetime(2026, 5, 22, 11, 0, tzinfo=IST))
        broker = _broker_with_clock(tmp_path, price_feed, clock)
        with pytest.raises(AMOWindowClosedError):
            broker.buy("RELIANCE", 1, time_in_force="AMO")

    def test_amo_market_queues_overnight(self, tmp_path, price_feed) -> None:
        # Submit at 18:00 IST (outside session, AMO window).
        clock = ReplayClock(datetime(2026, 5, 22, 18, 0, tzinfo=IST))
        broker = _broker_with_clock(tmp_path, price_feed, clock)
        order = broker.buy("RELIANCE", 1, time_in_force="AMO")
        assert order.status == OrderStatus.PENDING
        assert order.time_in_force == "AMO"
        assert order.order_type == OrderType.MARKET

    def test_amo_fires_at_open_via_watcher(self, tmp_path, price_feed) -> None:
        clock = ReplayClock(datetime(2026, 5, 22, 18, 0, tzinfo=IST))
        broker = _broker_with_clock(tmp_path, price_feed, clock)
        broker.buy("RELIANCE", 1, time_in_force="AMO")

        # Advance to next REGULAR session: Mon 2026-05-25 09:30 IST.
        clock.set(datetime(2026, 5, 25, 9, 30, tzinfo=IST))
        watcher = LimitOrderWatcher(broker, interval_seconds=999)
        watcher.tick()

        # AMO should have fired.
        orders = broker.get_orders(limit=10)
        assert any(
            o.status == OrderStatus.FILLED and o.time_in_force == "AMO"
            for o in orders
        )

    def test_fire_amo_orders_directly(self, tmp_path, price_feed) -> None:
        clock = ReplayClock(datetime(2026, 5, 22, 18, 0, tzinfo=IST))
        broker = _broker_with_clock(tmp_path, price_feed, clock)
        broker.buy("RELIANCE", 1, time_in_force="AMO")
        broker.buy("INFY", 2, time_in_force="AMO")
        # Without changing the clock, fire all AMO orders.
        n = broker.fire_amo_orders()
        assert n == 2
        # Re-firing fires zero (no PENDING AMOs left).
        assert broker.fire_amo_orders() == 0


# ── GTT ──────────────────────────────────────────────────────────────


class TestGTT:
    def test_gtt_survives_day_expiry(self, broker) -> None:
        # Place a regular DAY limit and a GTT limit. expire_stale_day_orders
        # should only kill the DAY one.
        day_order = broker.buy(
            "RELIANCE", 1,
            order_type=OrderType.LIMIT, limit_price=2000.00,
            time_in_force="DAY",
        )
        gtt_order = broker.buy(
            "INFY", 1,
            order_type=OrderType.LIMIT, limit_price=1700.00,
            time_in_force="GTT",
        )
        n = broker.expire_stale_day_orders()
        assert n == 1
        assert broker.get_order(day_order.id).status == OrderStatus.EXPIRED
        assert broker.get_order(gtt_order.id).status == OrderStatus.PENDING

    def test_gtc_alias_also_survives(self, broker) -> None:
        # GTC is accepted as an alias for GTT in the legacy test.
        gtc_order = broker.buy(
            "INFY", 1,
            order_type=OrderType.LIMIT, limit_price=1700.00,
            time_in_force="GTC",
        )
        broker.expire_stale_day_orders()
        assert broker.get_order(gtc_order.id).status == OrderStatus.PENDING


# ── Pre-open auction ─────────────────────────────────────────────────


class TestPreOpenAuction:
    def test_auction_fills_overlapping_limits(self, tmp_path, price_feed,
                                               stub_provider) -> None:
        # Run setup during REGULAR (so we can establish a position with
        # a market order), then jump to PRE_OPEN to test the auction
        # over PENDING limit orders.
        clock = ReplayClock(datetime(2026, 5, 22, 11, 0, tzinfo=IST))
        broker = _broker_with_clock(tmp_path, price_feed, clock)
        stub_provider.set("RELIANCE", 2500.0)

        # Build a position to sell from later.
        broker.buy("RELIANCE", 10)

        # Jump to PRE_OPEN of the next trading day. Place crossing
        # buy and sell limits.
        clock.set(datetime(2026, 5, 25, 9, 5, tzinfo=IST))
        broker.buy(
            "RELIANCE", 5,
            order_type=OrderType.LIMIT, limit_price=2502.00,
            time_in_force="DAY",
        )
        broker.sell(
            "RELIANCE", 5,
            order_type=OrderType.LIMIT, limit_price=2498.00,
            time_in_force="DAY",
        )

        match = broker.run_pre_open_auction()
        assert match.equilibrium_price is not None
        assert match.matched_volume > 0


# ── Watcher fires pre-open auction at REGULAR open ───────────────────


class TestWatcherPreOpen:
    def test_watcher_runs_auction_on_first_regular_tick(
        self, tmp_path, price_feed, stub_provider,
    ) -> None:
        clock = ReplayClock(datetime(2026, 5, 22, 9, 5, tzinfo=IST))
        broker = _broker_with_clock(tmp_path, price_feed, clock)
        stub_provider.set("RELIANCE", 2500.0)

        # Place a limit during PRE_OPEN that would cross at REGULAR open.
        broker.buy(
            "RELIANCE", 1,
            order_type=OrderType.LIMIT, limit_price=2510.00,
        )

        # Advance to REGULAR open and tick.
        clock.set(datetime(2026, 5, 22, 9, 16, tzinfo=IST))
        w = LimitOrderWatcher(broker, interval_seconds=999)
        # First tick runs the auction (no opposing side, nothing to fill).
        # Then the standard limit-fill loop runs (price 2500 ≤ 2510 → fill).
        w.tick()
        assert broker.get_order(
            broker.get_orders(limit=10)[0].id,
        ).status == OrderStatus.FILLED


# ── Adjusted close on YFinance ───────────────────────────────────────


class TestAdjustedClose:
    def test_quote_carries_adjusted_close_when_auto_adjust_true(
        self,
    ) -> None:
        from papertrade_india import YFinanceProvider

        # We can't hit yfinance from tests, but we can confirm the
        # ``auto_adjust`` flag is plumbed into the provider info.
        p = YFinanceProvider(auto_adjust=True)
        assert p.auto_adjust is True

        p2 = YFinanceProvider(auto_adjust=False)
        assert p2.auto_adjust is False
