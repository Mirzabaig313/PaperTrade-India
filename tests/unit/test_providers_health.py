"""Unit tests for the circuit-breaker provider wrapper."""

from __future__ import annotations

import time
from datetime import datetime

from papertrade_india.providers import (
    CircuitBreakerProvider,
    MarketDataProvider,
    MarketQuote,
    ProviderCapability,
    ProviderError,
    ProviderInfo,
)


class _Toggle(MarketDataProvider):
    """Provider that fails or succeeds on demand."""

    def __init__(self) -> None:
        self.fail = False
        self.calls = 0
        self.exception_class: type[BaseException] = ProviderError

    @property
    def info(self) -> ProviderInfo:
        return ProviderInfo(
            name="toggle",
            description="test stub",
            capabilities=ProviderCapability.LAST_PRICE,
        )

    def get_quote(self, symbol: str) -> MarketQuote | None:
        self.calls += 1
        if self.fail:
            raise self.exception_class("boom")
        return MarketQuote(
            last=100.0, timestamp=datetime.now(), source="toggle",
        )


def test_breaker_starts_closed() -> None:
    p = CircuitBreakerProvider(_Toggle())
    assert not p.health.is_open
    assert p.get_quote("X") is not None


def test_breaker_opens_after_threshold_failures() -> None:
    inner = _Toggle()
    p = CircuitBreakerProvider(inner, failure_threshold=3, open_seconds=10.0)
    inner.fail = True
    for _ in range(3):
        assert p.get_quote("X") is None
    assert p.health.is_open
    # Subsequent call should fast-fail without touching inner.
    inner.calls = 0
    assert p.get_quote("X") is None
    assert inner.calls == 0  # short-circuited


def test_breaker_recovers_via_half_open_probe() -> None:
    inner = _Toggle()
    p = CircuitBreakerProvider(
        inner,
        failure_threshold=2,
        open_seconds=0.05,  # very short for the test
    )
    inner.fail = True
    p.get_quote("X")
    p.get_quote("X")
    assert p.health.is_open

    # Wait until OPEN window elapses.
    time.sleep(0.06)
    inner.fail = False
    quote = p.get_quote("X")  # probe attempt
    assert quote is not None
    assert not p.health.is_open  # back to CLOSED


def test_breaker_handles_unexpected_exception_types() -> None:
    inner = _Toggle()
    inner.exception_class = RuntimeError
    p = CircuitBreakerProvider(inner, failure_threshold=2)
    inner.fail = True
    # The breaker swallows unexpected exceptions too — it's a wrapper,
    # not a re-raiser. Returning None is the contract.
    assert p.get_quote("X") is None
    assert p.get_quote("X") is None
    assert p.health.is_open


def test_unknown_symbol_does_not_open_breaker() -> None:
    inner = _Toggle()
    # Override to always return None (== "unknown symbol")
    inner.get_quote = lambda symbol: None  # type: ignore[assignment]
    p = CircuitBreakerProvider(inner, failure_threshold=2)
    for _ in range(5):
        assert p.get_quote("UNKNOWN") is None
    assert not p.health.is_open  # None ≠ failure


def test_health_failure_rate_tracks_window() -> None:
    inner = _Toggle()
    p = CircuitBreakerProvider(inner, window_size=10, failure_threshold=100)
    inner.fail = False
    for _ in range(7):
        p.get_quote("X")
    inner.fail = True
    for _ in range(3):
        p.get_quote("X")
    # 3 failures out of 10 = 0.3
    assert p.health.failure_rate == 0.3


def test_reset_recovers_state() -> None:
    inner = _Toggle()
    p = CircuitBreakerProvider(inner, failure_threshold=2)
    inner.fail = True
    p.get_quote("X")
    p.get_quote("X")
    assert p.health.is_open
    p.reset()
    assert not p.health.is_open
    assert p.health.consecutive_failures == 0
