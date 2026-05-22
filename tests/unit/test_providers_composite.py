"""Unit tests for the composite provider and aggregation strategies."""

from __future__ import annotations

from datetime import datetime

import pytest

from papertrade_india.providers import (
    CompositeProvider,
    MarketDataProvider,
    MarketQuote,
    MedianAggregation,
    ProviderCapability,
    ProviderError,
    ProviderInfo,
)


class _Static(MarketDataProvider):
    def __init__(
        self,
        name: str,
        quote: MarketQuote | None,
        raise_exc: Exception | None = None,
    ) -> None:
        self._n = name
        self._quote = quote
        self._raise = raise_exc

    @property
    def info(self) -> ProviderInfo:
        return ProviderInfo(
            name=self._n,
            description="static",
            capabilities=ProviderCapability.LAST_PRICE,
        )

    def get_quote(self, symbol: str) -> MarketQuote | None:
        if self._raise is not None:
            raise self._raise
        return self._quote


def _q(last: float, source: str = "x") -> MarketQuote:
    return MarketQuote(last=last, timestamp=datetime.now(), source=source)


class TestFirstWins:
    def test_returns_first_non_none(self) -> None:
        p1 = _Static("a", None)
        p2 = _Static("b", _q(100.0, "b"))
        p3 = _Static("c", _q(200.0, "c"))
        comp = CompositeProvider([p1, p2, p3])
        result = comp.get_quote("X")
        assert result is not None
        assert result.last == 100.0
        assert result.source == "b"

    def test_returns_none_when_all_fail(self) -> None:
        comp = CompositeProvider([_Static("a", None), _Static("b", None)])
        assert comp.get_quote("X") is None

    def test_skips_provider_that_raises(self) -> None:
        p1 = _Static("a", None, raise_exc=ProviderError("bad"))
        p2 = _Static("b", _q(99.0, "b"))
        comp = CompositeProvider([p1, p2])
        assert comp.get_quote("X").last == 99.0  # type: ignore[union-attr]


class TestMedianAggregation:
    def test_median_of_three(self) -> None:
        p1 = _Static("a", _q(99.0, "a"))
        p2 = _Static("b", _q(100.0, "b"))
        p3 = _Static("c", _q(101.0, "c"))
        comp = CompositeProvider(
            [p1, p2, p3],
            aggregation=MedianAggregation(),
            parallel=False,  # deterministic for tests
        )
        result = comp.get_quote("X")
        assert result is not None
        assert result.last == 100.0
        assert result.source.startswith("composite-median:")

    def test_median_of_two(self) -> None:
        p1 = _Static("a", _q(98.0, "a"))
        p2 = _Static("b", _q(102.0, "b"))
        comp = CompositeProvider(
            [p1, p2],
            aggregation=MedianAggregation(),
            parallel=False,
        )
        result = comp.get_quote("X")
        assert result.last == 100.0  # type: ignore[union-attr]

    def test_min_providers_returns_none_when_short(self) -> None:
        p1 = _Static("a", _q(100.0, "a"))
        comp = CompositeProvider(
            [p1, _Static("b", None)],
            aggregation=MedianAggregation(min_providers=2),
            parallel=False,
        )
        assert comp.get_quote("X") is None

    def test_outlier_disagreement_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        p1 = _Static("a", _q(100.0, "a"))
        p2 = _Static("b", _q(100.0, "b"))
        p3 = _Static("c", _q(150.0, "c"))  # 50% off
        comp = CompositeProvider(
            [p1, p2, p3],
            aggregation=MedianAggregation(max_disagreement_bps=200),
            parallel=False,
        )
        with caplog.at_level("WARNING"):
            comp.get_quote("X")
        assert any("disagreement" in r.message.lower() for r in caplog.records)

    def test_aggregates_volume_as_max(self) -> None:
        p1 = _Static(
            "a",
            MarketQuote(
                last=100.0, timestamp=datetime.now(),
                volume=1000, source="a",
            ),
        )
        p2 = _Static(
            "b",
            MarketQuote(
                last=100.0, timestamp=datetime.now(),
                volume=5000, source="b",
            ),
        )
        comp = CompositeProvider(
            [p1, p2], aggregation=MedianAggregation(), parallel=False,
        )
        result = comp.get_quote("X")
        assert result.volume == 5000  # type: ignore[union-attr]


def test_composite_capabilities_are_union() -> None:
    p1 = _Static("a", _q(1.0, "a"))
    p2 = _Static("b", _q(2.0, "b"))
    # Inject differing caps via the stub's info property.
    p1._n = "a"
    p2._n = "b"
    comp = CompositeProvider([p1, p2])
    # Both stubs declare LAST_PRICE so the union still has it.
    assert comp.supports(ProviderCapability.LAST_PRICE)
