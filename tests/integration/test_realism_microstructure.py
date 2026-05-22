"""Integration tests: tick / lot / band enforcement at the broker boundary."""

from __future__ import annotations

import pytest

from papertrade_india import (
    Exchange,
    IndiaPaperBroker,
    LotSizeViolation,
    MicrostructureConfig,
    OrderType,
    PriceBandViolation,
    TickSizeViolation,
)


def _seed_symbol(
    broker: IndiaPaperBroker,
    symbol: str = "RELIANCE",
    *,
    tick_size: float | None = None,
    lot_size: int = 1,
    daily_band_pct: float | None = None,
) -> None:
    """Seed the symbol master with explicit microstructure metadata."""
    with broker.persistence.transaction() as conn:
        broker.symbol_master.upsert(
            conn,
            symbol=symbol,
            exchange=Exchange.NSE,
            lot_size=lot_size,
            tick_size=tick_size,
            daily_band_pct=daily_band_pct,
        )


class TestTickEnforcement:
    def test_misaligned_limit_price_rejected(self, broker, stub_provider) -> None:
        _seed_symbol(broker, tick_size=0.05)
        with pytest.raises(TickSizeViolation):
            broker.buy(
                "RELIANCE", 1, order_type=OrderType.LIMIT, limit_price=2940.07,
            )

    def test_aligned_limit_price_accepted(self, broker) -> None:
        _seed_symbol(broker, tick_size=0.05)
        order = broker.buy(
            "RELIANCE", 1, order_type=OrderType.LIMIT, limit_price=2940.05,
        )
        assert order.limit_price == 2940.05

    def test_market_order_skips_tick_check(self, broker) -> None:
        # Market orders don't carry a price, so tick is N/A.
        _seed_symbol(broker, tick_size=0.05)
        order = broker.buy("RELIANCE", 1)
        assert order.filled_qty == 1

    def test_disable_tick_enforcement(self, tmp_path, price_feed) -> None:
        # Build a separate broker with the check disabled.
        broker = IndiaPaperBroker(
            initial_capital=1_000_000,
            db_path=tmp_path / "no_tick.db",
            price_feed=price_feed,
            enforce_market_hours=False,
            microstructure_config=MicrostructureConfig(enforce_tick_size=False),
        )
        _seed_symbol(broker, tick_size=0.05)
        order = broker.buy(
            "RELIANCE", 1, order_type=OrderType.LIMIT, limit_price=2940.07,
        )
        assert order.limit_price == 2940.07


class TestLotEnforcement:
    def test_non_multiple_qty_rejected(self, broker) -> None:
        _seed_symbol(broker, lot_size=25)
        with pytest.raises(LotSizeViolation, match="multiple"):
            broker.buy("RELIANCE", 30)

    def test_multiple_qty_accepted(self, broker) -> None:
        _seed_symbol(broker, lot_size=25)
        order = broker.buy("RELIANCE", 50)
        assert order.qty == 50

    def test_lot_one_passes_anything(self, broker) -> None:
        # Default lot=1 (no override).
        _seed_symbol(broker, lot_size=1)
        order = broker.buy("RELIANCE", 7)
        assert order.qty == 7


class TestBandEnforcement:
    def test_limit_outside_band_rejected(self, broker, stub_provider) -> None:
        # We need a prev_close — patch the rich quote.
        from datetime import datetime

        from papertrade_india.providers import MarketQuote

        def fake_market_quote(symbol):
            return MarketQuote(
                last=2500.0,
                timestamp=datetime.now(),
                prev_close=2500.0,
                source="test",
            )

        broker.price_feed.get_market_quote = fake_market_quote  # type: ignore[assignment]

        _seed_symbol(broker, tick_size=0.05, daily_band_pct=0.05)
        # +5% band → max 2625, +10% asks for rejection.
        with pytest.raises(PriceBandViolation):
            broker.buy(
                "RELIANCE", 1,
                order_type=OrderType.LIMIT, limit_price=2750.00,
            )

    def test_limit_inside_band_passes(self, broker, stub_provider) -> None:
        from datetime import datetime

        from papertrade_india.providers import MarketQuote

        def fake_market_quote(symbol):
            return MarketQuote(
                last=2500.0,
                timestamp=datetime.now(),
                prev_close=2500.0,
                source="test",
            )

        broker.price_feed.get_market_quote = fake_market_quote  # type: ignore[assignment]

        _seed_symbol(broker, tick_size=0.05, daily_band_pct=0.05)
        order = broker.buy(
            "RELIANCE", 1,
            order_type=OrderType.LIMIT, limit_price=2600.00,
        )
        assert order.limit_price == 2600.00

    def test_no_prev_close_skips_band(self, broker) -> None:
        # Default price feed stub doesn't return prev_close → check skipped.
        _seed_symbol(broker, tick_size=0.05, daily_band_pct=0.05)
        # This price is way outside any reasonable band, but with no
        # prev_close the check is skipped.
        order = broker.buy(
            "RELIANCE", 1,
            order_type=OrderType.LIMIT, limit_price=10_000.00,
        )
        assert order.limit_price == 10_000.00


class TestUnregisteredSymbols:
    def test_falls_back_to_default_tick(self, broker) -> None:
        # No symbol master entry at all. Default tick = 0.05; price 2940.07
        # should still be rejected.
        with pytest.raises(TickSizeViolation):
            broker.buy(
                "UNKNOWNCO", 1,
                order_type=OrderType.LIMIT, limit_price=2940.07,
            )
