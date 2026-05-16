"""Shared fixtures.

All fixtures here are hermetic: no real network, no real yfinance, no
real wall-clock dependency. Tests run in-memory or against per-test
SQLite files in ``tmp_path``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make ``src/`` importable when running pytest from the project root,
# even when the package isn't installed (e.g. ``pytest`` from the repo).
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PROJECT_ROOT / "src"
if _SRC.exists() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ── Minimal in-memory price provider ──────────────────────────────────


class StubPriceProvider:
    """Predictable in-memory price provider for tests."""

    def __init__(self, prices: dict[str, float] | None = None) -> None:
        self.prices: dict[str, float] = dict(prices or {})
        self.calls: int = 0

    def set(self, symbol: str, price: float) -> None:
        self.prices[symbol] = price

    def get_price(self, symbol: str) -> float | None:
        self.calls += 1
        return self.prices.get(symbol)


@pytest.fixture
def stub_provider() -> StubPriceProvider:
    return StubPriceProvider(
        {
            "RELIANCE": 2500.0,
            "INFY": 1800.0,
            "TCS": 4000.0,
            "HDFCBANK": 1500.0,
            "ICICIBANK": 1100.0,
        }
    )


@pytest.fixture
def price_feed(stub_provider):
    from papertrade_india import PriceFeed

    # Disable the short cache so tests that mutate the stub price between
    # calls observe the change immediately.
    return PriceFeed(providers=[stub_provider], short_cache_ttl_seconds=0)


@pytest.fixture
def broker(tmp_path, price_feed):
    """A fresh broker with no market-hours enforcement (for unit tests)."""
    from papertrade_india import IndiaPaperBroker

    return IndiaPaperBroker(
        initial_capital=1_000_000.0,
        db_path=tmp_path / "broker.db",
        account_id="test",
        price_feed=price_feed,
        enforce_market_hours=False,
    )
