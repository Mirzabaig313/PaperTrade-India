"""Tests for resilient_feed — the multi-provider, low-lag, self-healing
price chain (use-all-providers wiring).

Proves the three properties we actually care about:
  1. First-wins: returns the first live quote, ignoring later providers.
  2. Self-healing over time: a flaky provider's circuit breaker opens
     after repeated failures, while the chain keeps serving from the
     healthy fallback.
  3. Last-resort: when every provider is down, PriceFeed's long cache
     still answers (flagged not-real-time).
"""

from __future__ import annotations

from datetime import datetime

import pytest

from papertrade_india import PriceFeed, PriceUnavailableError, resilient_feed
from papertrade_india.providers import (
    CircuitBreakerProvider,
    MarketDataProvider,
    MarketQuote,
    ProviderCapability,
    ProviderError,
    ProviderInfo,
)


class _Provider(MarketDataProvider):
    """Configurable fake: returns a fixed quote, returns None, or raises."""

    def __init__(self, name: str, *, last: float | None = None, raises: bool = False, real_time: bool = True) -> None:
        self._name = name
        self._last = last
        self._raises = raises
        self._real_time = real_time
        self.calls = 0

    @property
    def info(self) -> ProviderInfo:
        return ProviderInfo(
            name=self._name,
            description="fake",
            capabilities=ProviderCapability.LAST_PRICE | ProviderCapability.QUOTE,
        )

    def get_quote(self, symbol: str) -> MarketQuote | None:
        self.calls += 1
        if self._raises:
            raise ProviderError(f"{self._name} down")
        if self._last is None:
            return None
        return MarketQuote(
            last=self._last, timestamp=datetime.now(), source=self._name,
            is_real_time=self._real_time,
        )


def test_first_wins_returns_first_live_quote() -> None:
    primary = _Provider("primary", last=100.0)
    secondary = _Provider("secondary", last=999.0)
    feed = resilient_feed([primary, secondary], short_cache_ttl_seconds=0)

    q = feed.get_market_quote("RELIANCE")
    assert q.last == 100.0
    assert q.source == "primary"
    # Secondary is never consulted when primary answers.
    assert secondary.calls == 0


def test_wraps_new_providers_in_circuit_breaker() -> None:
    feed = resilient_feed([_Provider("p", last=1.0)], short_cache_ttl_seconds=0)
    assert isinstance(feed.providers[0], CircuitBreakerProvider)


def test_flaky_provider_breaker_opens_chain_stays_up() -> None:
    flaky = _Provider("flaky", raises=True)
    healthy = _Provider("healthy", last=50.0)
    feed = resilient_feed([flaky, healthy], short_cache_ttl_seconds=0)
    breaker = feed.providers[0]
    assert isinstance(breaker, CircuitBreakerProvider)

    # Drive enough calls to trip the breaker (default threshold 5).
    for _ in range(6):
        q = feed.get_market_quote("RELIANCE")
        assert q.last == 50.0  # healthy fallback always answers

    # The flaky source has self-ejected; quality stays stable over time.
    assert breaker.health.is_open is True


def test_all_down_falls_back_to_long_cache() -> None:
    down = _Provider("down", raises=True)
    feed = resilient_feed([down], short_cache_ttl_seconds=0)
    feed.prime("RELIANCE", 123.0)  # seed long cache

    q = feed.get_market_quote("RELIANCE")
    assert q.last == 123.0
    assert q.is_real_time is False  # served from cache, not a live feed


def test_all_down_no_cache_raises() -> None:
    down = _Provider("down", raises=True)
    feed = resilient_feed([down], short_cache_ttl_seconds=0)
    with pytest.raises(PriceUnavailableError):
        feed.get_market_quote("NOSUCH")


def test_no_wrap_passes_providers_through() -> None:
    p = _Provider("p", last=1.0)
    feed = resilient_feed([p], wrap_circuit_breaker=False, short_cache_ttl_seconds=0)
    assert feed.providers[0] is p


def test_returns_pricefeed_instance() -> None:
    feed = resilient_feed([_Provider("p", last=1.0)])
    assert isinstance(feed, PriceFeed)


def test_short_cache_preserves_delayed_provenance() -> None:
    # Regression: a delayed quote cached in the short cache must NOT be
    # reported as real-time on the next call (that would defeat the
    # broker's enforce_real_time guard). Uses a non-zero short-cache TTL.
    delayed = _Provider("delayed", last=100.0, real_time=False)
    feed = resilient_feed([delayed], short_cache_ttl_seconds=30.0)

    first = feed.get_quote("RELIANCE")
    assert first.is_real_time is False
    assert delayed.calls == 1

    second = feed.get_quote("RELIANCE")  # served from short cache
    assert second.source == "short_cache"
    assert second.is_real_time is False  # provenance preserved
    assert delayed.calls == 1  # cache hit, provider not re-called


def test_short_cache_preserves_realtime_provenance() -> None:
    live = _Provider("live", last=100.0, real_time=True)
    feed = resilient_feed([live], short_cache_ttl_seconds=30.0)
    feed.get_quote("RELIANCE")
    second = feed.get_quote("RELIANCE")
    assert second.source == "short_cache"
    assert second.is_real_time is True
