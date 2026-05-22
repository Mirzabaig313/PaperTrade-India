"""Integration tests for the rest of the realism suite:
   - Mark-to-bid for unrealized P&L
   - Latency simulation
   - Random rejection
   - Order-book impact on market fills
"""

from __future__ import annotations

from datetime import datetime

import pytest

from papertrade_india import (
    IndiaPaperBroker,
    LatencyConfig,
    OrderBookConfig,
    PriceFeed,
    RandomBrokerRejection,
    RejectionConfig,
    RejectScenario,
)
from papertrade_india.providers import (
    MarketDataProvider,
    MarketQuote,
    ProviderCapability,
    ProviderInfo,
)


class _StubRichProvider(MarketDataProvider):
    """Provider that returns full :class:`MarketQuote`s, not just floats."""

    def __init__(self, quotes: dict[str, MarketQuote]) -> None:
        self.quotes = quotes
        self.calls = 0

    @property
    def info(self) -> ProviderInfo:
        return ProviderInfo(
            name="stub-rich",
            description="test stub returning rich MarketQuotes",
            capabilities=ProviderCapability.LAST_PRICE | ProviderCapability.QUOTE,
        )

    def get_quote(self, symbol: str) -> MarketQuote | None:
        self.calls += 1
        return self.quotes.get(symbol)


def _broker_with_rich_quotes(
    tmp_path,
    quotes: dict[str, MarketQuote],
    **broker_kwargs,
) -> IndiaPaperBroker:
    feed = PriceFeed(providers=[_StubRichProvider(quotes)], short_cache_ttl_seconds=0)
    return IndiaPaperBroker(
        initial_capital=1_000_000,
        db_path=tmp_path / "rich.db",
        price_feed=feed,
        enforce_market_hours=False,
        **broker_kwargs,
    )


# ── Mark-to-bid ──────────────────────────────────────────────────────


class TestMarkToBid:
    def test_default_marks_off_bid(self, tmp_path) -> None:
        # As of the realism flip, ``mark_to_bid`` is True by default —
        # this matches what real brokers do for unrealized P&L on longs.
        quotes = {
            "RELIANCE": MarketQuote(
                last=2500.0, timestamp=datetime.now(),
                bid=2495.0, ask=2505.0, source="t",
            ),
        }
        broker = _broker_with_rich_quotes(tmp_path, quotes)
        broker.buy("RELIANCE", 1)
        pos = broker.get_position("RELIANCE")
        assert pos.mark_basis == "bid"
        assert pos.current_price == 2495.0

    def test_opt_out_marks_off_last(self, tmp_path) -> None:
        # Pass ``mark_to_bid=False`` to fall back to legacy last-price marking.
        quotes = {
            "RELIANCE": MarketQuote(
                last=2500.0, timestamp=datetime.now(),
                bid=2495.0, ask=2505.0, source="t",
            ),
        }
        broker = _broker_with_rich_quotes(tmp_path, quotes, mark_to_bid=False)
        broker.buy("RELIANCE", 1)
        pos = broker.get_position("RELIANCE")
        assert pos.mark_basis == "last"
        assert pos.current_price == 2500.0

    def test_mark_to_bid_uses_bid_for_long(self, tmp_path) -> None:
        quotes = {
            "RELIANCE": MarketQuote(
                last=2500.0, timestamp=datetime.now(),
                bid=2495.0, ask=2505.0, source="t",
            ),
        }
        broker = _broker_with_rich_quotes(tmp_path, quotes, mark_to_bid=True)
        broker.buy("RELIANCE", 1)
        pos = broker.get_position("RELIANCE")
        assert pos.mark_basis == "bid"
        assert pos.current_price == 2495.0

    def test_mark_to_bid_falls_back_to_last_without_bid(self, tmp_path) -> None:
        # Quote has no bid/ask — should land on "last".
        quotes = {
            "RELIANCE": MarketQuote(
                last=2500.0, timestamp=datetime.now(), source="t",
            ),
        }
        broker = _broker_with_rich_quotes(tmp_path, quotes, mark_to_bid=True)
        broker.buy("RELIANCE", 1)
        pos = broker.get_position("RELIANCE")
        assert pos.mark_basis == "last"


# ── Latency ──────────────────────────────────────────────────────────


class TestLatency:
    def test_zero_latency_no_sleep(self, tmp_path, stub_provider, price_feed) -> None:
        broker = IndiaPaperBroker(
            initial_capital=1_000_000,
            db_path=tmp_path / "no_lat.db",
            price_feed=price_feed,
            enforce_market_hours=False,
            latency_config=LatencyConfig(submit_ms_mean=0.0),
        )
        # Just confirm the order completes; timing is too flaky to assert.
        order = broker.buy("RELIANCE", 1)
        assert order.filled_qty == 1

    def test_small_latency_does_not_break_orders(
        self, tmp_path, stub_provider, price_feed,
    ) -> None:
        broker = IndiaPaperBroker(
            initial_capital=1_000_000,
            db_path=tmp_path / "lat.db",
            price_feed=price_feed,
            enforce_market_hours=False,
            latency_config=LatencyConfig(
                submit_ms_mean=5.0, submit_ms_p99=10.0, seed=1,
            ),
        )
        order = broker.buy("RELIANCE", 1)
        assert order.filled_qty == 1


# ── Random rejection ─────────────────────────────────────────────────


class TestRandomRejection:
    def test_zero_rate_never_rejects(self, tmp_path, price_feed) -> None:
        broker = IndiaPaperBroker(
            initial_capital=1_000_000,
            db_path=tmp_path / "no_rej.db",
            price_feed=price_feed,
            enforce_market_hours=False,
            rejection_config=RejectionConfig(rate=0.0),
        )
        for _ in range(20):
            broker.buy("RELIANCE", 1)

    def test_full_rate_always_rejects(self, tmp_path, price_feed) -> None:
        broker = IndiaPaperBroker(
            initial_capital=1_000_000,
            db_path=tmp_path / "rej.db",
            price_feed=price_feed,
            enforce_market_hours=False,
            rejection_config=RejectionConfig(
                rate=1.0,
                scenarios=[RejectScenario.FREEZE_QTY],
                seed=1,
            ),
        )
        with pytest.raises(RandomBrokerRejection, match="freeze_qty"):
            broker.buy("RELIANCE", 1)


# ── Order book impact ────────────────────────────────────────────────


class TestOrderBookImpact:
    def test_enabled_by_default(self, tmp_path) -> None:
        # The order book is on by default. With provider bid/ask in
        # place, a small market order fills at the ask (top of book),
        # not at last.
        quotes = {
            "RELIANCE": MarketQuote(
                last=2500.0, timestamp=datetime.now(),
                bid=2499.95, ask=2500.05, volume=10_000_000,
                source="t",
            ),
        }
        broker = _broker_with_rich_quotes(tmp_path, quotes)
        order = broker.buy("RELIANCE", 1)
        assert order.filled_avg_price == 2500.05

    def test_disabled_by_explicit_opt_out(self, broker) -> None:
        # The shared ``broker`` fixture has the realism layers off.
        order = broker.buy("RELIANCE", 5)
        assert order.filled_avg_price == 2500.0  # the stub last

    def test_enabled_walks_book(self, tmp_path) -> None:
        quotes = {
            "RELIANCE": MarketQuote(
                last=2500.0, timestamp=datetime.now(),
                bid=2499.95, ask=2500.05, volume=10_000,
                source="t",
            ),
        }
        broker = _broker_with_rich_quotes(
            tmp_path, quotes,
            order_book_config=OrderBookConfig(
                enabled=True, levels=5, depth_pct_of_adv=0.01, shape_decay=0.5,
            ),
        )
        # 1% of 10k = 100 at touch. A 250-share order walks past level 0.
        order = broker.buy("RELIANCE", 250)
        # VWAP must be > best ask (2500.05) due to walking.
        assert order.filled_avg_price > 2500.05
        # And not above the 5th level (2500.05 + 4 * 0.05 = 2500.25).
        assert order.filled_avg_price < 2500.25

    def test_queue_position_tracked_for_limits(self, tmp_path) -> None:
        from papertrade_india.models import OrderSide, OrderType
        quotes = {
            "RELIANCE": MarketQuote(
                last=2500.0, timestamp=datetime.now(),
                bid=2499.95, ask=2500.05, volume=10_000,
                source="t",
            ),
        }
        broker = _broker_with_rich_quotes(
            tmp_path, quotes,
            order_book_config=OrderBookConfig(
                enabled=True, levels=3, depth_pct_of_adv=0.01,
            ),
        )
        broker.buy(
            "RELIANCE", 5,
            order_type=OrderType.LIMIT, limit_price=2499.95,
        )
        pos = broker.get_queue_position(
            "RELIANCE", OrderSide.BUY, 2499.95,
        )
        assert pos is not None
        assert pos > 0  # there's other size ahead in the book
