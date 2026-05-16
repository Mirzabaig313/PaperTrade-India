"""Unit tests for the multi-provider price feed."""

from __future__ import annotations

import time

import pytest

from papertrade_india import (
    CachedLastKnownProvider,
    PriceFeed,
    PriceUnavailableError,
)


class FailingProvider:
    def __init__(self, exc: Exception | None = None) -> None:
        self.exc = exc
        self.calls = 0

    def get_price(self, symbol: str) -> float | None:
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return None


class StaticProvider:
    def __init__(self, price: float | None) -> None:
        self.price = price
        self.calls = 0

    def get_price(self, symbol: str) -> float | None:
        self.calls += 1
        return self.price


def test_first_provider_wins():
    p1 = StaticProvider(100.0)
    p2 = StaticProvider(200.0)
    feed = PriceFeed(providers=[p1, p2], short_cache_ttl_seconds=0)
    assert feed.get_price("X") == 100.0
    assert p1.calls == 1
    assert p2.calls == 0  # never reached


def test_fallback_when_first_returns_none():
    p1 = StaticProvider(None)
    p2 = StaticProvider(200.0)
    feed = PriceFeed(providers=[p1, p2], short_cache_ttl_seconds=0)
    assert feed.get_price("X") == 200.0
    assert p1.calls == 1
    assert p2.calls == 1


def test_provider_exception_falls_through():
    p1 = FailingProvider(RuntimeError("boom"))
    p2 = StaticProvider(150.0)
    feed = PriceFeed(providers=[p1, p2], short_cache_ttl_seconds=0)
    assert feed.get_price("X") == 150.0
    assert p1.calls == 1
    assert p2.calls == 1


def test_cache_serves_when_all_providers_fail():
    """First call populates cache; second call uses cache."""
    p = StaticProvider(123.0)
    feed = PriceFeed(providers=[p], short_cache_ttl_seconds=0)
    assert feed.get_price("X") == 123.0
    p.price = None  # All live providers fail
    # Long cache (default 1h) should still serve.
    assert feed.get_price("X") == 123.0


def test_raises_when_no_provider_and_no_cache():
    feed = PriceFeed(providers=[StaticProvider(None)], short_cache_ttl_seconds=0)
    with pytest.raises(PriceUnavailableError):
        feed.get_price("UNKNOWN")


def test_short_cache_absorbs_repeat_calls():
    p = StaticProvider(50.0)
    feed = PriceFeed(providers=[p], short_cache_ttl_seconds=10.0)
    feed.get_price("X")
    feed.get_price("X")
    feed.get_price("X")
    # Only one underlying call thanks to the short cache.
    assert p.calls == 1


def test_prime_seeds_cache():
    feed = PriceFeed(providers=[StaticProvider(None)], short_cache_ttl_seconds=10.0)
    feed.prime("ABC", 42.0)
    assert feed.get_price("ABC") == 42.0


def test_long_cache_expires():
    cache = CachedLastKnownProvider(ttl_seconds=0)
    cache.update("X", 1.0)
    # ttl=0 means immediately expired.
    time.sleep(0.01)
    assert cache.get_price("X") is None
