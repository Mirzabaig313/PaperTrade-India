"""PriceFeed must accept both the legacy ``PriceProvider`` Protocol
shape (objects with ``get_price``) and the new
:class:`MarketDataProvider` ABC. This test pins both paths.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from papertrade_india import PriceFeed, PriceUnavailableError
from papertrade_india.providers import (
    CircuitBreakerProvider,
    CompositeProvider,
    MarketDataProvider,
    MarketQuote,
    MedianAggregation,
    ProviderCapability,
    ProviderError,
    ProviderInfo,
)


class _NewProvider(MarketDataProvider):
    """New-style provider returning a MarketQuote."""

    def __init__(self, last: float, source: str = "new") -> None:
        self._last = last
        self._source = source

    @property
    def info(self) -> ProviderInfo:
        return ProviderInfo(
            name=self._source,
            description="new style",
            capabilities=ProviderCapability.LAST_PRICE,
        )

    def get_quote(self, symbol: str) -> MarketQuote | None:
        return MarketQuote(
            last=self._last,
            timestamp=datetime.now(),
            source=self._source,
        )


class _LegacyProvider:
    """Legacy provider — only ``get_price``, no ``info``/``get_quote``."""

    def __init__(self, last: float | None) -> None:
        self._last = last
        self.calls = 0

    def get_price(self, symbol: str) -> float | None:
        self.calls += 1
        return self._last


def test_pricefeed_accepts_new_provider() -> None:
    feed = PriceFeed(providers=[_NewProvider(2940.0, "new")], short_cache_ttl_seconds=0)
    quote = feed.get_quote("RELIANCE")
    assert quote.price == 2940.0
    assert quote.source == "new"


def test_pricefeed_falls_through_new_to_legacy() -> None:
    """Mixing new and legacy providers in one chain works as expected."""
    new = _NewProvider(0.0, "stale-new")
    # Make the new provider return None to force fall-through.
    new.get_quote = lambda symbol: None  # type: ignore[assignment]

    legacy = _LegacyProvider(123.0)
    feed = PriceFeed(providers=[new, legacy], short_cache_ttl_seconds=0)
    quote = feed.get_quote("X")
    assert quote.price == 123.0
    assert legacy.calls == 1


def test_pricefeed_uses_get_market_quote_for_rich_data() -> None:
    """``get_market_quote`` returns the full :class:`MarketQuote`."""
    new = _NewProvider(150.0, "rich")
    feed = PriceFeed(providers=[new], short_cache_ttl_seconds=0)
    quote = feed.get_market_quote("X")
    assert isinstance(quote, MarketQuote)
    assert quote.last == 150.0
    assert quote.source == "rich"


def test_pricefeed_short_cache_works_with_new_providers() -> None:
    """The hot-path short cache should still absorb repeat calls."""
    new = _NewProvider(99.0, "cached")
    calls = {"n": 0}

    def get_quote(symbol: str) -> MarketQuote:
        calls["n"] += 1
        return MarketQuote(last=99.0, timestamp=datetime.now(), source="cached")

    new.get_quote = get_quote  # type: ignore[assignment]
    feed = PriceFeed(providers=[new], short_cache_ttl_seconds=10.0)
    feed.get_price("X")
    feed.get_price("X")
    feed.get_price("X")
    assert calls["n"] == 1


def test_pricefeed_with_circuit_breaker() -> None:
    """A breaker-wrapped provider plugs into PriceFeed without changes."""
    inner = _NewProvider(50.0, "bp")
    breaker = CircuitBreakerProvider(inner, failure_threshold=2)
    feed = PriceFeed(providers=[breaker], short_cache_ttl_seconds=0)
    assert feed.get_price("X") == 50.0


def test_pricefeed_with_composite_median() -> None:
    a = _NewProvider(99.0, "a")
    b = _NewProvider(100.0, "b")
    c = _NewProvider(101.0, "c")
    composite = CompositeProvider(
        [a, b, c],
        aggregation=MedianAggregation(),
        parallel=False,
    )
    feed = PriceFeed(providers=[composite], short_cache_ttl_seconds=0)
    quote = feed.get_market_quote("X")
    assert quote.last == 100.0


def test_pricefeed_raises_when_all_fail() -> None:
    class _Always(MarketDataProvider):
        @property
        def info(self) -> ProviderInfo:
            return ProviderInfo(
                name="always_none",
                description="returns nothing",
                capabilities=ProviderCapability.LAST_PRICE,
            )

        def get_quote(self, symbol: str) -> MarketQuote | None:
            return None

    feed = PriceFeed(providers=[_Always()], short_cache_ttl_seconds=0)
    with pytest.raises(PriceUnavailableError):
        feed.get_price("X")


def test_pricefeed_swallows_provider_error() -> None:
    class _Bad(MarketDataProvider):
        @property
        def info(self) -> ProviderInfo:
            return ProviderInfo(
                name="bad",
                description="raises",
                capabilities=ProviderCapability.LAST_PRICE,
            )

        def get_quote(self, symbol: str) -> MarketQuote | None:
            raise ProviderError("network gone")

    legacy = _LegacyProvider(7.0)
    feed = PriceFeed(providers=[_Bad(), legacy], short_cache_ttl_seconds=0)
    assert feed.get_price("X") == 7.0
