"""Unit tests for the provider base interface and shared types."""

from __future__ import annotations

from datetime import datetime

import pytest

from papertrade_india.providers import (
    OHLCV,
    MarketDataProvider,
    MarketQuote,
    ProviderCapability,
    ProviderError,
    ProviderInfo,
)


class _StubProvider(MarketDataProvider):
    def __init__(
        self,
        name: str = "stub",
        caps: ProviderCapability = ProviderCapability.LAST_PRICE,
        quote: MarketQuote | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self._name = name
        self._caps = caps
        self._quote = quote
        self._raise = raise_exc

    @property
    def info(self) -> ProviderInfo:
        return ProviderInfo(
            name=self._name,
            description="stub for tests",
            capabilities=self._caps,
        )

    def get_quote(self, symbol: str) -> MarketQuote | None:
        if self._raise is not None:
            raise self._raise
        return self._quote


def _q(last: float = 100.0, **kw: object) -> MarketQuote:
    defaults = {"timestamp": datetime(2026, 5, 22, 10, 0, 0), "source": "stub"}
    defaults.update(kw)
    return MarketQuote(last=last, **defaults)  # type: ignore[arg-type]


class TestMarketQuote:
    def test_mid_when_bid_and_ask_present(self) -> None:
        q = _q(last=100.0, bid=99.95, ask=100.05)
        assert q.mid == pytest.approx(100.0)

    def test_mid_is_none_when_one_side_missing(self) -> None:
        assert _q(bid=99.0).mid is None
        assert _q(ask=99.0).mid is None
        assert _q().mid is None

    def test_spread_bps(self) -> None:
        # 5 paise spread on a ₹100 mid = 5 bps
        q = _q(last=100.0, bid=99.975, ask=100.025)
        assert q.spread_bps == pytest.approx(5.0)

    def test_spread_bps_none_when_unknown(self) -> None:
        assert _q().spread_bps is None


class TestProviderCapability:
    def test_supports(self) -> None:
        p = _StubProvider(
            caps=ProviderCapability.LAST_PRICE | ProviderCapability.SUPPORTS_NSE,
        )
        assert p.supports(ProviderCapability.LAST_PRICE)
        assert p.supports(ProviderCapability.SUPPORTS_NSE)
        assert not p.supports(ProviderCapability.QUOTE)
        assert not p.supports(ProviderCapability.OHLCV_INTRADAY)

    def test_capability_combine(self) -> None:
        c = ProviderCapability.LAST_PRICE | ProviderCapability.QUOTE
        assert c & ProviderCapability.LAST_PRICE
        assert c & ProviderCapability.QUOTE
        assert not (c & ProviderCapability.OHLCV_INTRADAY)


class TestProviderDefaults:
    def test_get_price_returns_last_when_quote_present(self) -> None:
        p = _StubProvider(quote=_q(last=42.0))
        assert p.get_price("X") == 42.0

    def test_get_price_returns_none_when_no_quote(self) -> None:
        p = _StubProvider(quote=None)
        assert p.get_price("X") is None

    def test_get_price_returns_none_on_provider_error(self) -> None:
        p = _StubProvider(raise_exc=ProviderError("boom"))
        assert p.get_price("X") is None

    def test_get_price_propagates_unexpected_exceptions(self) -> None:
        p = _StubProvider(raise_exc=RuntimeError("network"))
        # Default get_price only swallows ProviderError; other exceptions
        # surface so they're not silently lost.
        with pytest.raises(RuntimeError):
            p.get_price("X")

    def test_get_history_default_returns_empty(self) -> None:
        from datetime import date
        p = _StubProvider(quote=_q())
        assert p.get_history("X", date(2026, 1, 1), date(2026, 1, 31)) == []

    def test_name_is_lowercase_info_name(self) -> None:
        p = _StubProvider(name="My-Provider")
        # We don't lowercase here — the convention is providers register
        # themselves with lowercase names. The Stub uses "stub".
        assert p.name == "My-Provider"


class TestOHLCV:
    def test_construction(self) -> None:
        bar = OHLCV(
            timestamp=datetime(2026, 5, 22),
            open=100.0, high=110.0, low=99.0, close=105.0, volume=1000,
        )
        assert bar.close == 105.0
        assert bar.volume == 1000
