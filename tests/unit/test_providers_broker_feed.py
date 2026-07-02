"""Unit tests for the broker-feed providers (Kite / Dhan / Upstox).

These talk to real brokerage APIs, so the tests inject a fake client
(Kite, Dhan) or stub ``urllib`` (Upstox) and exercise the full
parse path — bid/ask + depth mapping, unknown-symbol handling, and
auth/credential errors — without touching the network.
"""

from __future__ import annotations

import json

import pytest

from papertrade_india.providers import (
    DhanProvider,
    KiteProvider,
    ProviderCapability,
    ProviderError,
    UpstoxProvider,
)

# ── Kite ──────────────────────────────────────────────────────────────


class _FakeKite:
    """Stand-in for a configured ``KiteConnect`` client."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.last_instrument: str | None = None

    def quote(self, instrument: str) -> dict:
        self.last_instrument = instrument
        return self._payload


_KITE_QUOTE = {
    "NSE:RELIANCE": {
        "last_price": 2940.0,
        "volume": 1234567,
        "ohlc": {"open": 2900.0, "high": 2950.0, "low": 2880.0, "close": 2920.0},
        "timestamp": "2026-05-22 15:30:00",
        "depth": {
            "buy": [{"price": 2939.5, "quantity": 100, "orders": 3}],
            "sell": [{"price": 2940.5, "quantity": 80, "orders": 2}],
        },
    },
}


def test_kite_parses_quote_with_depth() -> None:
    p = KiteProvider(exchange="NSE", kite=_FakeKite(_KITE_QUOTE))
    q = p.get_quote("RELIANCE")
    assert q is not None
    assert q.last == 2940.0
    assert q.bid == 2939.5
    assert q.ask == 2940.5
    assert q.bid_size == 100
    assert q.ask_size == 80
    assert q.prev_close == 2920.0
    assert q.volume == 1234567
    assert q.source == "kite"
    assert q.is_real_time is True
    # mid/spread derived from real bid/ask
    assert q.mid == pytest.approx(2940.0)
    assert q.spread_bps is not None


def test_kite_unknown_symbol_returns_none() -> None:
    p = KiteProvider(exchange="NSE", kite=_FakeKite({}))
    assert p.get_quote("NOSUCH") is None


def test_kite_missing_credentials_raises() -> None:
    p = KiteProvider(api_key=None, access_token=None)
    with pytest.raises(ProviderError, match="credentials missing"):
        p.get_quote("RELIANCE")


def test_kite_sdk_failure_becomes_provider_error() -> None:
    class _Boom:
        def quote(self, _instrument: str) -> dict:
            raise RuntimeError("TokenException: token expired")

    p = KiteProvider(kite=_Boom())
    with pytest.raises(ProviderError, match="kite quote failed"):
        p.get_quote("RELIANCE")


def test_kite_capabilities_real_time_and_quote() -> None:
    info = KiteProvider(exchange="NSE").info
    assert ProviderCapability.REAL_TIME in info.capabilities
    assert ProviderCapability.QUOTE in info.capabilities
    assert ProviderCapability.SUPPORTS_NSE in info.capabilities
    assert info.requires_api_key is True


# ── Dhan ──────────────────────────────────────────────────────────────


class _FakeDhan:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.last_securities: dict | None = None

    def quote_data(self, securities: dict) -> dict:
        self.last_securities = securities
        return self._payload


_DHAN_RESP = {
    "status": "success",
    "data": {
        "data": {
            "NSE_EQ": {
                "2885": {
                    "last_price": 2940.0,
                    "volume": 555000,
                    "ohlc": {
                        "open": 2900.0, "high": 2950.0,
                        "low": 2880.0, "close": 2920.0,
                    },
                    "depth": {
                        "buy": [{"price": 2939.0, "quantity": 50}],
                        "sell": [{"price": 2941.0, "quantity": 60}],
                    },
                },
            },
        },
    },
}


def test_dhan_parses_quote_with_security_map() -> None:
    p = DhanProvider(
        exchange="NSE",
        security_id_map={"RELIANCE": "2885"},
        dhan=_FakeDhan(_DHAN_RESP),
    )
    q = p.get_quote("RELIANCE")
    assert q is not None
    assert q.last == 2940.0
    assert q.bid == 2939.0
    assert q.ask == 2941.0
    assert q.bid_size == 50
    assert q.ask_size == 60
    assert q.prev_close == 2920.0
    assert q.source == "dhan"
    assert q.is_real_time is True


def test_dhan_unresolved_symbol_returns_none() -> None:
    # No security_id_map entry and no resolver → can't resolve → None.
    p = DhanProvider(security_id_map={}, dhan=_FakeDhan(_DHAN_RESP))
    assert p.get_quote("RELIANCE") is None


def test_dhan_resolver_callable_used() -> None:
    p = DhanProvider(
        resolve=lambda sym, seg: "2885" if sym == "RELIANCE" else None,
        dhan=_FakeDhan(_DHAN_RESP),
    )
    assert p.get_quote("RELIANCE") is not None
    assert p.get_quote("UNKNOWN") is None


def test_dhan_missing_credentials_raises() -> None:
    p = DhanProvider(
        client_id=None, access_token=None,
        security_id_map={"RELIANCE": "2885"},
    )
    with pytest.raises(ProviderError, match="credentials missing"):
        p.get_quote("RELIANCE")


def test_dhan_sdk_failure_becomes_provider_error() -> None:
    class _Boom:
        def quote_data(self, securities: dict) -> dict:
            raise RuntimeError("DhanException: rate limit")

    p = DhanProvider(security_id_map={"RELIANCE": "2885"}, dhan=_Boom())
    with pytest.raises(ProviderError, match="dhan quote failed"):
        p.get_quote("RELIANCE")


def test_dhan_parses_epoch_last_trade_time() -> None:
    from datetime import datetime

    resp = {
        "data": {"data": {"NSE_EQ": {"2885": {
            "last_price": 100.0,
            "last_trade_time": int(datetime(2026, 5, 22, 10, 0, 0).timestamp()),
        }}}},
    }
    p = DhanProvider(security_id_map={"X": "2885"}, dhan=_FakeDhan(resp))
    q = p.get_quote("X")
    assert q is not None
    assert q.timestamp == datetime(2026, 5, 22, 10, 0, 0)


# ── Upstox ────────────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, body: str) -> None:
        self._body = body.encode("utf-8")

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def read(self, *_a: object) -> bytes:
        return self._body


_UPSTOX_BODY = json.dumps(
    {
        "status": "success",
        "data": {
            "NSE_EQ:RELIANCE": {
                "last_price": 2940.0,
                "volume": 777000,
                "ohlc": {
                    "open": 2900.0, "high": 2950.0,
                    "low": 2880.0, "close": 2920.0,
                },
                "depth": {
                    "buy": [
                        {"price": 2939.25, "quantity": 40, "orders": 2},
                        {"price": 2939.00, "quantity": 60, "orders": 3},
                    ],
                    "sell": [
                        {"price": 2940.75, "quantity": 35, "orders": 1},
                        {"price": 2941.00, "quantity": 50, "orders": 2},
                    ],
                },
            },
        },
    },
)


def test_upstox_parses_quote_with_depth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "papertrade_india.providers.upstox.urllib.request.urlopen",
        lambda *_a, **_kw: _FakeResponse(_UPSTOX_BODY),
    )
    p = UpstoxProvider(
        access_token="X",
        instrument_key_map={"RELIANCE": "NSE_EQ|INE002A01018"},
    )
    q = p.get_quote("RELIANCE")
    assert q is not None
    assert q.last == 2940.0
    assert q.bid == 2939.25
    assert q.ask == 2940.75
    assert q.bid_size == 40
    assert q.ask_size == 35
    assert q.prev_close == 2920.0
    assert q.volume == 777000
    assert q.source == "upstox"
    assert q.is_real_time is True
    # Full 5-level ladder parsed (best-first, (price, size)).
    assert q.has_depth is True
    assert q.bids == ((2939.25, 40), (2939.00, 60))
    assert q.asks == ((2940.75, 35), (2941.00, 50))


def test_upstox_sends_non_default_user_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    # Regression: Upstox sits behind Cloudflare, which bans the default
    # Python-urllib User-Agent (Error 1010) before auth is evaluated.
    captured = {}

    def _capture(req, *a, **kw):
        captured["ua"] = req.get_header("User-agent")
        return _FakeResponse(_UPSTOX_BODY)

    monkeypatch.setattr(
        "papertrade_india.providers.upstox.urllib.request.urlopen", _capture,
    )
    p = UpstoxProvider(
        access_token="X",
        instrument_key_map={"RELIANCE": "NSE_EQ|INE002A01018"},
    )
    p.get_quote("RELIANCE")
    assert captured["ua"] is not None
    assert "urllib" not in captured["ua"].lower()


def test_upstox_missing_token_raises() -> None:
    p = UpstoxProvider(
        access_token=None,
        instrument_key_map={"RELIANCE": "NSE_EQ|INE002A01018"},
    )
    with pytest.raises(ProviderError, match="access token missing"):
        p.get_quote("RELIANCE")


def test_upstox_unresolved_symbol_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Token present but no instrument key → can't resolve → None
    # (no network call expected).
    def _boom(*_a: object, **_kw: object) -> None:
        raise AssertionError("should not hit the network")

    monkeypatch.setattr(
        "papertrade_india.providers.upstox.urllib.request.urlopen", _boom,
    )
    p = UpstoxProvider(access_token="X", instrument_key_map={})
    assert p.get_quote("RELIANCE") is None


def test_upstox_empty_data_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "papertrade_india.providers.upstox.urllib.request.urlopen",
        lambda *_a, **_kw: _FakeResponse(json.dumps({"status": "success", "data": {}})),
    )
    p = UpstoxProvider(
        access_token="X",
        instrument_key_map={"RELIANCE": "NSE_EQ|INE002A01018"},
    )
    assert p.get_quote("RELIANCE") is None


def test_upstox_single_entry_wrong_symbol_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Sole block names a different symbol → must not be accepted.
    body = json.dumps(
        {"status": "success", "data": {"NSE_EQ:TCS": {"last_price": 4000.0}}},
    )
    monkeypatch.setattr(
        "papertrade_india.providers.upstox.urllib.request.urlopen",
        lambda *_a, **_kw: _FakeResponse(body),
    )
    p = UpstoxProvider(
        access_token="X",
        instrument_key_map={"RELIANCE": "NSE_EQ|INE002A01018"},
    )
    assert p.get_quote("RELIANCE") is None


def test_upstox_auth_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from urllib.error import HTTPError

    def _raise(*_a: object, **_kw: object) -> None:
        raise HTTPError("u", 401, "Unauthorized", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(
        "papertrade_india.providers.upstox.urllib.request.urlopen", _raise,
    )
    p = UpstoxProvider(
        access_token="X",
        instrument_key_map={"RELIANCE": "NSE_EQ|INE002A01018"},
    )
    with pytest.raises(ProviderError, match="auth failed"):
        p.get_quote("RELIANCE")


def test_upstox_network_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from urllib.error import URLError

    def _raise(*_a: object, **_kw: object) -> None:
        raise URLError("boom")

    monkeypatch.setattr(
        "papertrade_india.providers.upstox.urllib.request.urlopen", _raise,
    )
    p = UpstoxProvider(
        access_token="X",
        instrument_key_map={"RELIANCE": "NSE_EQ|INE002A01018"},
    )
    with pytest.raises(ProviderError, match="fetch failed"):
        p.get_quote("RELIANCE")


# ── Registry wiring ───────────────────────────────────────────────────


def test_registry_lists_broker_feeds() -> None:
    from papertrade_india.providers import default_registry

    names = set(default_registry.names())
    assert {"kite", "dhan", "upstox"} <= names


def test_registry_builds_upstox() -> None:
    from papertrade_india.providers import default_registry

    # Upstox is stdlib-only, so it's always available to construct.
    p = default_registry.get("upstox", access_token="X")
    assert isinstance(p, UpstoxProvider)


# ── is_real_time propagation + enforce_real_time guard ────────────────


from datetime import datetime  # noqa: E402

from papertrade_india.providers import (  # noqa: E402
    MarketDataProvider,
    MarketQuote,
    ProviderInfo,
)


class _StubProvider(MarketDataProvider):
    """Provider that returns a quote with a chosen is_real_time flag."""

    def __init__(self, *, real_time: bool, last: float = 100.0) -> None:
        self._real_time = real_time
        self._last = last

    @property
    def info(self) -> ProviderInfo:
        return ProviderInfo(
            name="stub", description="test",
            capabilities=ProviderCapability.LAST_PRICE | ProviderCapability.QUOTE,
        )

    def get_quote(self, symbol: str) -> MarketQuote | None:
        return MarketQuote(
            last=self._last, timestamp=datetime.now(),
            source="stub", is_real_time=self._real_time,
        )


def _make_pricefeed(real_time: bool):
    from papertrade_india import PriceFeed

    return PriceFeed(
        providers=[_StubProvider(real_time=real_time)],
        short_cache_ttl_seconds=0,
    )


def test_pricefeed_propagates_is_real_time_true() -> None:
    q = _make_pricefeed(real_time=True).get_quote("RELIANCE")
    assert q.is_real_time is True


def test_pricefeed_propagates_is_real_time_false() -> None:
    q = _make_pricefeed(real_time=False).get_quote("RELIANCE")
    assert q.is_real_time is False
    assert q.is_stale is False  # delayed != cache-stale


def _make_broker(tmp_path, *, real_time: bool, enforce: bool):
    from papertrade_india import (
        IndiaPaperBroker,
        OrderBookConfig,
        RejectionConfig,
        SettlementConfig,
        SettlementMode,
        SlippageConfig,
    )

    return IndiaPaperBroker(
        initial_capital=1_000_000.0,
        db_path=tmp_path / "rt.db",
        account_id="test",
        price_feed=_make_pricefeed(real_time=real_time),
        enforce_market_hours=False,
        enforce_fresh_prices=False,
        enforce_real_time=enforce,
        order_book_config=OrderBookConfig(enabled=False),
        settlement_config=SettlementConfig(mode=SettlementMode.T_PLUS_0),
        rejection_config=RejectionConfig(rate=0.0),
        slippage_config=SlippageConfig(bps=0.0),
        mark_to_bid=False,
    )


def test_enforce_real_time_rejects_delayed_feed(tmp_path) -> None:
    from papertrade_india import StalePriceRejected

    broker = _make_broker(tmp_path, real_time=False, enforce=True)
    with pytest.raises(StalePriceRejected, match="delayed feed"):
        broker.buy("RELIANCE", 1)


def test_enforce_real_time_off_allows_delayed_feed(tmp_path) -> None:
    # The non-breaking promise: delayed feed + guard off → normal fill.
    broker = _make_broker(tmp_path, real_time=False, enforce=False)
    order = broker.buy("RELIANCE", 1)
    assert order.status.value == "filled"


def test_enforce_real_time_allows_real_time_feed(tmp_path) -> None:
    broker = _make_broker(tmp_path, real_time=True, enforce=True)
    order = broker.buy("RELIANCE", 1)
    assert order.status.value == "filled"
