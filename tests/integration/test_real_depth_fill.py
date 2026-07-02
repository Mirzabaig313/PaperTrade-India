"""End-to-end: a provider quote with real L2 depth drives the fill.

Feeds a has_depth=True MarketQuote through the full broker fill path
(quickstart → market order → maybe_apply_book_impact → book_from_levels
→ walk_book) and asserts the fill walks the real ladder, rather than
just testing the simulator in isolation.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from papertrade_india import PriceFeed, quickstart
from papertrade_india.providers import (
    MarketDataProvider,
    MarketQuote,
    ProviderCapability,
    ProviderInfo,
)


class _DepthProvider(MarketDataProvider):
    """Stub returning a fixed two-level book on each side."""

    @property
    def info(self) -> ProviderInfo:
        return ProviderInfo(
            name="depthstub",
            description="test depth",
            capabilities=(
                ProviderCapability.LAST_PRICE
                | ProviderCapability.QUOTE
                | ProviderCapability.REAL_TIME
            ),
        )

    def get_quote(self, symbol: str) -> MarketQuote | None:
        return MarketQuote(
            last=100.0,
            timestamp=datetime.now(),
            bid=100.0,
            ask=100.5,
            volume=1_000_000,
            source="depthstub",
            is_real_time=True,
            bids=((100.0, 50), (99.5, 60)),
            asks=((100.5, 40), (101.0, 70)),
        )


@pytest.fixture()
def broker(tmp_path):
    feed = PriceFeed(providers=[_DepthProvider()], short_cache_ttl_seconds=0)
    return quickstart(
        db_path=str(tmp_path / "depth.db"),
        symbol_master=None,
        enforce_market_hours=False,
        enforce_fresh_prices=False,
        price_feed=feed,
    )


def test_market_buy_walks_real_depth(broker) -> None:
    # Buy 60 vs asks [(100.5,40),(101.0,70)] → 40@100.5 + 20@101.0.
    # VWAP = (40*100.5 + 20*101.0) / 60 = 100.6667.
    order = broker.buy("X", 60)
    assert order.status.value == "filled"
    # Fill walked past the touch into the second real level.
    assert 100.5 < order.filled_avg_price < 101.0
    assert order.filled_avg_price == pytest.approx(100.6667, abs=0.01)


def test_small_market_buy_fills_at_touch(broker) -> None:
    # Buy 10 (< best-ask size 40) → all at the touch, no walk.
    order = broker.buy("X", 10)
    assert order.status.value == "filled"
    assert order.filled_avg_price == pytest.approx(100.5, abs=1e-6)
