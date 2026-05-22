"""Unit tests for REST/HTTP-based providers, with the network stubbed.

These providers all use ``urllib.request.urlopen`` against fixed hosts.
The tests monkey-patch that one entry point so we can exercise the full
parse path without touching the network.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import pytest

from papertrade_india.providers import (
    NSEBhavcopyProvider,
    ProviderError,
    StooqProvider,
)
from papertrade_india.providers.alphavantage import AlphaVantageProvider
from papertrade_india.providers.finnhub import FinnhubProvider
from papertrade_india.providers.twelvedata import TwelveDataProvider


class _FakeResponse:
    def __init__(self, body: str) -> None:
        self._body = body.encode("utf-8")

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def read(self) -> bytes:
        return self._body


@pytest.fixture()
def fake_urlopen(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    """Patch every provider's ``urllib.request.urlopen`` import to return a fake."""
    captured: dict[str, str] = {}

    def make(body: str):
        def _opener(req_or_url: Any, timeout: float = 0) -> _FakeResponse:
            url = req_or_url.full_url if hasattr(req_or_url, "full_url") else req_or_url
            captured["last_url"] = url
            return _FakeResponse(body)
        return _opener

    yield make, captured


# ── Stooq ────────────────────────────────────────────────────────────


def test_stooq_parses_csv(monkeypatch: pytest.MonkeyPatch) -> None:
    body = (
        "Symbol,Date,Time,Open,High,Low,Close,Volume\n"
        "RELIANCE.IN,2026-05-22,18:00:00,2900.50,2950.00,2880.00,2940.75,1234567\n"
    )
    monkeypatch.setattr(
        "papertrade_india.providers.stooq.urllib.request.urlopen",
        lambda *_a, **_kw: _FakeResponse(body),
    )
    p = StooqProvider()
    q = p.get_quote("RELIANCE")
    assert q is not None
    assert q.last == 2940.75
    assert q.high == 2950.0
    assert q.low == 2880.0
    assert q.volume == 1234567
    assert q.source == "stooq"


def test_stooq_returns_none_on_unknown_symbol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = (
        "Symbol,Date,Time,Open,High,Low,Close,Volume\n"
        "UNKNOWN.IN,N/D,N/D,N/D,N/D,N/D,N/D,N/D\n"
    )
    monkeypatch.setattr(
        "papertrade_india.providers.stooq.urllib.request.urlopen",
        lambda *_a, **_kw: _FakeResponse(body),
    )
    p = StooqProvider()
    assert p.get_quote("UNKNOWN") is None


def test_stooq_raises_on_network_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from urllib.error import URLError

    def _raise(*_a: object, **_kw: object) -> None:
        raise URLError("boom")

    monkeypatch.setattr(
        "papertrade_india.providers.stooq.urllib.request.urlopen", _raise,
    )
    p = StooqProvider()
    with pytest.raises(ProviderError):
        p.get_quote("RELIANCE")


# ── Alpha Vantage ─────────────────────────────────────────────────────


def test_alphavantage_requires_api_key() -> None:
    p = AlphaVantageProvider(api_key=None)
    with pytest.raises(ProviderError, match="API key"):
        p.get_quote("RELIANCE")


def test_alphavantage_parses_global_quote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = json.dumps(
        {
            "Global Quote": {
                "01. symbol": "RELIANCE.BSE",
                "02. open": "2900.0",
                "03. high": "2950.0",
                "04. low": "2880.0",
                "05. price": "2940.0",
                "06. volume": "12345",
                "08. previous close": "2920.0",
            },
        },
    )
    monkeypatch.setattr(
        "papertrade_india.providers.alphavantage.urllib.request.urlopen",
        lambda *_a, **_kw: _FakeResponse(body),
    )
    p = AlphaVantageProvider(api_key="X")
    q = p.get_quote("RELIANCE")
    assert q is not None
    assert q.last == 2940.0
    assert q.prev_close == 2920.0
    assert q.volume == 12345


def test_alphavantage_rate_limit_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = json.dumps({"Note": "API call frequency limit"})
    monkeypatch.setattr(
        "papertrade_india.providers.alphavantage.urllib.request.urlopen",
        lambda *_a, **_kw: _FakeResponse(body),
    )
    p = AlphaVantageProvider(api_key="X")
    with pytest.raises(ProviderError, match="rate-limit"):
        p.get_quote("RELIANCE")


# ── Twelve Data ───────────────────────────────────────────────────────


def test_twelvedata_parses_quote(monkeypatch: pytest.MonkeyPatch) -> None:
    body = json.dumps(
        {
            "symbol": "RELIANCE",
            "exchange": "NSE",
            "datetime": "2026-05-22 15:30:00",
            "open": "2900",
            "high": "2950",
            "low": "2880",
            "close": "2940",
            "previous_close": "2920",
            "volume": "1234567",
        },
    )
    monkeypatch.setattr(
        "papertrade_india.providers.twelvedata.urllib.request.urlopen",
        lambda *_a, **_kw: _FakeResponse(body),
    )
    p = TwelveDataProvider(api_key="X", exchange="NSE")
    q = p.get_quote("RELIANCE")
    assert q is not None
    assert q.last == 2940.0
    assert q.volume == 1234567


def test_twelvedata_unknown_symbol_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = json.dumps({"status": "error", "code": 404, "message": "not found"})
    monkeypatch.setattr(
        "papertrade_india.providers.twelvedata.urllib.request.urlopen",
        lambda *_a, **_kw: _FakeResponse(body),
    )
    p = TwelveDataProvider(api_key="X", exchange="NSE")
    assert p.get_quote("NOSUCH") is None


# ── Finnhub ───────────────────────────────────────────────────────────


def test_finnhub_parses_quote(monkeypatch: pytest.MonkeyPatch) -> None:
    body = json.dumps(
        {
            "c": 2940.0,
            "o": 2900.0,
            "h": 2950.0,
            "l": 2880.0,
            "pc": 2920.0,
            "t": int(datetime(2026, 5, 22, 10, 0, 0).timestamp()),
        },
    )
    monkeypatch.setattr(
        "papertrade_india.providers.finnhub.urllib.request.urlopen",
        lambda *_a, **_kw: _FakeResponse(body),
    )
    p = FinnhubProvider(api_key="X")
    q = p.get_quote("RELIANCE")
    assert q is not None
    assert q.last == 2940.0
    assert q.is_real_time is True


def test_finnhub_unknown_symbol_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = json.dumps({"c": 0, "o": 0, "h": 0, "l": 0, "pc": 0, "t": 0})
    monkeypatch.setattr(
        "papertrade_india.providers.finnhub.urllib.request.urlopen",
        lambda *_a, **_kw: _FakeResponse(body),
    )
    p = FinnhubProvider(api_key="X")
    assert p.get_quote("NOSUCH") is None


def test_finnhub_requires_api_key() -> None:
    p = FinnhubProvider(api_key=None)
    with pytest.raises(ProviderError, match="API key"):
        p.get_quote("RELIANCE")


# ── NSE Bhavcopy ──────────────────────────────────────────────────────


_BHAV_CSV = (
    " SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE,"
    " LAST_PRICE, CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS,"
    " NO_OF_TRADES, DELIV_QTY, DELIV_PER\n"
    "RELIANCE,EQ,22-MAY-2026,2920.0,2900.0,2950.0,2880.0,2940.0,2940.0,2920.0,"
    "1234567,3625.5,5000,500000,40.50\n"
    "TCS,EQ,22-MAY-2026,3500.0,3510.0,3550.0,3490.0,3540.0,3540.0,3520.0,"
    "98765,348.0,2000,30000,30.4\n"
)


def test_bhavcopy_parses_csv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "papertrade_india.providers.nse_bhavcopy.urllib.request.urlopen",
        lambda *_a, **_kw: _FakeResponse(_BHAV_CSV),
    )
    p = NSEBhavcopyProvider(max_lookback_days=1)
    q = p.get_quote("RELIANCE")
    assert q is not None
    assert q.last == 2940.0
    assert q.open == 2900.0
    assert q.volume == 1234567
    assert q.source == "nse-bhavcopy"


def test_bhavcopy_unknown_symbol_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "papertrade_india.providers.nse_bhavcopy.urllib.request.urlopen",
        lambda *_a, **_kw: _FakeResponse(_BHAV_CSV),
    )
    p = NSEBhavcopyProvider(max_lookback_days=1)
    assert p.get_quote("NOSUCH") is None


def test_bhavcopy_caches_per_date(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def _fake(*_a: object, **_kw: object) -> _FakeResponse:
        calls["n"] += 1
        return _FakeResponse(_BHAV_CSV)

    monkeypatch.setattr(
        "papertrade_india.providers.nse_bhavcopy.urllib.request.urlopen", _fake,
    )
    p = NSEBhavcopyProvider(max_lookback_days=1)
    p.get_quote("RELIANCE")
    p.get_quote("TCS")
    p.get_quote("RELIANCE")
    # Three quotes against one cached fetch.
    assert calls["n"] == 1
