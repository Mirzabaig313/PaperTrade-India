"""Unit tests for tick / lot / band rules and the order-book simulator."""

from __future__ import annotations

import pytest

from papertrade_india import (
    LotSizeViolation,
    OrderBookConfig,
    OrderBookSimulator,
    PriceBandViolation,
    TickSizeViolation,
    round_to_tick,
)
from papertrade_india.domain.models import OrderSide
from papertrade_india.domain.rules.tick_lot_band import (
    is_aligned_to_tick,
    validate_band,
    validate_lot,
    validate_tick,
)

# ── Tick alignment ──────────────────────────────────────────────────


class TestTickRounding:
    def test_round_to_nearest_tick(self) -> None:
        assert round_to_tick(2940.327, 0.05) == 2940.35
        assert round_to_tick(2940.32, 0.05) == 2940.30
        assert round_to_tick(2940.50, 0.05) == 2940.50

    def test_round_to_tick_handles_decimal_drift(self) -> None:
        # Classic IEEE float trap: 0.1 + 0.2 ≠ 0.3
        assert round_to_tick(0.1 + 0.2, 0.05) == 0.30

    def test_round_to_tick_zero_inputs(self) -> None:
        assert round_to_tick(0.0, 0.05) == 0.0
        assert round_to_tick(100.0, 0.0) == 0.0

    def test_is_aligned_with_drift_tolerance(self) -> None:
        assert is_aligned_to_tick(2940.05, 0.05)
        assert is_aligned_to_tick(2940.0, 0.05)
        assert not is_aligned_to_tick(2940.07, 0.05)


class TestValidateTick:
    def test_aligned_price_passes(self) -> None:
        validate_tick(2940.05, 0.05, "limit_price")

    def test_misaligned_raises(self) -> None:
        with pytest.raises(TickSizeViolation, match="2940.07"):
            validate_tick(2940.07, 0.05, "limit_price")

    def test_none_price_passes(self) -> None:
        validate_tick(None, 0.05, "limit_price")

    def test_zero_tick_disables(self) -> None:
        validate_tick(2940.327, 0.0, "limit_price")  # no raise


# ── Lot size ─────────────────────────────────────────────────────────


class TestValidateLot:
    def test_lot_one_always_passes(self) -> None:
        validate_lot(7, 1)
        validate_lot(123, 1)

    def test_multiple_passes(self) -> None:
        validate_lot(50, 25)
        validate_lot(75, 25)

    def test_non_multiple_raises(self) -> None:
        with pytest.raises(LotSizeViolation, match="multiple"):
            validate_lot(60, 25)

    def test_fractional_qty_raises(self) -> None:
        with pytest.raises(LotSizeViolation, match="fractional"):
            validate_lot(0.5, 25)


# ── Price band ───────────────────────────────────────────────────────


class TestValidateBand:
    def test_inside_band_passes(self) -> None:
        validate_band(price=105.0, prev_close=100.0, band_pct=0.10)
        validate_band(price=95.0, prev_close=100.0, band_pct=0.10)

    def test_above_band_raises(self) -> None:
        with pytest.raises(PriceBandViolation, match="outside"):
            validate_band(price=111.0, prev_close=100.0, band_pct=0.10)

    def test_below_band_raises(self) -> None:
        with pytest.raises(PriceBandViolation):
            validate_band(price=89.0, prev_close=100.0, band_pct=0.10)

    def test_no_prev_close_skips(self) -> None:
        validate_band(price=99999, prev_close=None, band_pct=0.10)

    def test_zero_band_disables(self) -> None:
        validate_band(price=99999, prev_close=100.0, band_pct=0.0)


# ── Synthetic order book ─────────────────────────────────────────────


class TestOrderBookSynthesis:
    def test_book_levels_descend_on_bid_ascend_on_ask(self) -> None:
        sim = OrderBookSimulator(OrderBookConfig(enabled=True, levels=5))
        book = sim.synthesize(
            symbol="X", last=100.0, bid=99.95, ask=100.05,
            adv=1_000_000, tick_size=0.05,
        )
        assert len(book.bids) == 5
        assert len(book.asks) == 5
        for i in range(4):
            assert book.bids[i].price > book.bids[i + 1].price
            assert book.asks[i].price < book.asks[i + 1].price

    def test_book_size_decays_geometrically(self) -> None:
        sim = OrderBookSimulator(OrderBookConfig(
            enabled=True, levels=3, depth_pct_of_adv=0.01, shape_decay=0.5,
        ))
        book = sim.synthesize(
            symbol="X", last=100.0, bid=99.95, ask=100.05,
            adv=10_000, tick_size=0.05,
        )
        # depth_pct=1% of 10k = 100 at touch; halves each level.
        assert book.asks[0].size == 100
        assert book.asks[1].size == 50
        assert book.asks[2].size == 25

    def test_book_falls_back_when_bid_ask_missing(self) -> None:
        sim = OrderBookSimulator(OrderBookConfig(enabled=True))
        book = sim.synthesize(
            symbol="X", last=100.0, bid=None, ask=None,
            adv=10_000, tick_size=0.05,
        )
        # Should still produce a valid two-sided book.
        assert book.best_bid is not None
        assert book.best_ask is not None
        assert book.best_ask > book.best_bid


class TestWalkBook:
    def _book(self) -> OrderBookSimulator:
        sim = OrderBookSimulator(OrderBookConfig(
            enabled=True, levels=5, depth_pct_of_adv=0.01, shape_decay=0.5,
        ))
        return sim

    def test_small_buy_clears_at_touch(self) -> None:
        sim = self._book()
        book = sim.synthesize(
            symbol="X", last=100.0, bid=99.95, ask=100.05,
            adv=10_000, tick_size=0.05,  # top size = 100
        )
        fill = sim.walk_book(book, OrderSide.BUY, qty=50)
        assert fill.fully_filled
        assert fill.filled_qty == 50
        assert fill.avg_price == 100.05
        assert fill.impact_bps == pytest.approx(5.0, abs=0.5)

    def test_large_buy_walks_multiple_levels(self) -> None:
        sim = self._book()
        book = sim.synthesize(
            symbol="X", last=100.0, bid=99.95, ask=100.05,
            adv=10_000, tick_size=0.05,
        )
        # Levels: 100 @ 100.05, 50 @ 100.10, 25 @ 100.15, 12 @ 100.20, 6 @ 100.25
        fill = sim.walk_book(book, OrderSide.BUY, qty=160)
        # Should consume top three levels fully then partially the 4th.
        assert fill.filled_qty == 160
        assert fill.avg_price > 100.05  # paid up
        assert fill.avg_price < 100.20

    def test_walk_runs_dry(self) -> None:
        sim = self._book()
        book = sim.synthesize(
            symbol="X", last=100.0, bid=99.95, ask=100.05,
            adv=1_000, tick_size=0.05,
        )
        # Tiny ADV ⇒ tiny book. Order much bigger than total depth.
        fill = sim.walk_book(book, OrderSide.BUY, qty=10_000)
        assert not fill.fully_filled
        assert fill.filled_qty < 10_000


class TestQueuePosition:
    def test_join_queue_returns_seed_size(self) -> None:
        sim = OrderBookSimulator(OrderBookConfig(
            enabled=True, levels=3, depth_pct_of_adv=0.01,
        ))
        book = sim.synthesize(
            symbol="X", last=100.0, bid=99.95, ask=100.05,
            adv=10_000, tick_size=0.05,
        )
        ahead = sim.join_queue("X", OrderSide.BUY, 99.95, book)
        assert ahead == 100  # top-of-book size

    def test_subsequent_joins_stack(self) -> None:
        sim = OrderBookSimulator(OrderBookConfig(
            enabled=True, levels=3, depth_pct_of_adv=0.01,
        ))
        book = sim.synthesize(
            symbol="X", last=100.0, bid=99.95, ask=100.05,
            adv=10_000, tick_size=0.05,
        )
        sim.join_queue("X", OrderSide.BUY, 99.95, book)
        ahead2 = sim.join_queue("X", OrderSide.BUY, 99.95, book)
        # Second order joins behind the first.
        assert ahead2 > 100

    def test_observe_trade_drains_queue(self) -> None:
        sim = OrderBookSimulator(OrderBookConfig(
            enabled=True, levels=3, depth_pct_of_adv=0.01,
        ))
        book = sim.synthesize(
            symbol="X", last=100.0, bid=99.95, ask=100.05,
            adv=10_000, tick_size=0.05,
        )
        sim.join_queue("X", OrderSide.BUY, 99.95, book)
        # A sell hits the bid, eating queue at 99.95.
        sim.observe_trade("X", OrderSide.SELL, 99.95, qty=30)
        assert sim.queue_position("X", OrderSide.BUY, 99.95, 0.05) == 70


class TestAlmgrenImpact:
    def test_zero_when_no_adv(self) -> None:
        sim = OrderBookSimulator()
        assert sim.almgren_impact_bps(qty=1000, adv=None) == 0.0
        assert sim.almgren_impact_bps(qty=1000, adv=0.0) == 0.0

    def test_square_root_scaling(self) -> None:
        sim = OrderBookSimulator(OrderBookConfig(
            almgren_coeff_bps=50.0, almgren_exponent=0.5,
        ))
        # 100% of ADV = 50 bps cost
        assert sim.almgren_impact_bps(qty=100, adv=100) == pytest.approx(50.0)
        # 25% of ADV = 50 * sqrt(0.25) = 25 bps
        assert sim.almgren_impact_bps(qty=25, adv=100) == pytest.approx(25.0)
