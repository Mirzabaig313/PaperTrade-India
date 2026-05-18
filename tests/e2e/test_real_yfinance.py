"""End-to-end tests against the real yfinance API.

Opt-in via ``RUN_E2E=1`` env var or the ``-m e2e`` pytest marker.
These hit the network. The default ``pytest`` invocation skips them
to keep CI deterministic.

Run with::

    RUN_E2E=1 pytest -m e2e

What they catch
---------------
- yfinance shape regressions (Yahoo changing field names is the most
  common upstream break for this kind of package).
- Symbol-suffix regressions (RELIANCE.NS resolving to nothing).
- Timezone regressions when running in non-IST environments.
"""

from __future__ import annotations

import os

import pytest

from papertrade_india import (
    IndiaPaperBroker,
    PriceFeed,
    PriceUnavailableError,
    YFinanceProvider,
)

# Skip the whole module unless explicitly opted in.
pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.getenv("RUN_E2E") != "1",
        reason="E2E tests require RUN_E2E=1 (network access)",
    ),
]


def test_yfinance_resolves_known_nse_symbol():
    """RELIANCE.NS should always have a quote on a trading day."""
    p = YFinanceProvider("NS")
    price = p.get_price("RELIANCE")
    assert price is not None
    assert price > 0


def test_price_feed_with_real_yfinance(tmp_path):
    """Full broker round-trip: fetch real price, place a paper trade.

    Uses ``enforce_market_hours=False`` so this can run any time. The
    real-vs-fake distinction here is the price source; the order itself
    is paper.
    """
    broker = IndiaPaperBroker(
        initial_capital=1_000_000.0,
        db_path=tmp_path / "e2e.db",
        account_id="e2e",
        price_feed=PriceFeed(providers=[YFinanceProvider("NS")]),
        enforce_market_hours=False,
    )
    order = broker.buy("RELIANCE", 1)
    assert order.filled_avg_price is not None
    assert order.filled_avg_price > 0
    assert order.fees_paid > 0


def test_unknown_symbol_raises_unavailable(tmp_path):
    """A bogus symbol exhausts all providers and raises."""
    feed = PriceFeed(providers=[YFinanceProvider("NS")],
                     short_cache_ttl_seconds=0)
    with pytest.raises(PriceUnavailableError):
        feed.get_price("DEFINITELYNOTASTOCK_XYZ")
