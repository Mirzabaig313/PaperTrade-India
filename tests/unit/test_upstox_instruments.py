"""Hermetic tests for the Upstox instrument-master resolver.

Inject a records list so there's no network/cache I/O.
"""

from __future__ import annotations

import pytest

from papertrade_india.providers import (
    ProviderError,
    UpstoxInstrumentMaster,
    UpstoxProvider,
)

_RECORDS = [
    {"segment": "NSE_EQ", "instrument_type": "EQ",
     "trading_symbol": "RELIANCE", "instrument_key": "NSE_EQ|INE002A01018"},
    {"segment": "NSE_EQ", "instrument_type": "EQ",
     "trading_symbol": "TCS", "instrument_key": "NSE_EQ|INE467B01029"},
    # Non-equity / other-segment rows must be ignored.
    {"segment": "NSE_FO", "instrument_type": "FUT",
     "trading_symbol": "RELIANCE", "instrument_key": "NSE_FO|12345"},
    {"segment": "NSE_INDEX", "instrument_type": "INDEX",
     "trading_symbol": "NIFTY 50", "instrument_key": "NSE_INDEX|Nifty 50"},
]


def test_resolves_equity_symbol() -> None:
    m = UpstoxInstrumentMaster(records=_RECORDS)
    assert m.resolve("RELIANCE") == "NSE_EQ|INE002A01018"
    assert m.resolve("TCS") == "NSE_EQ|INE467B01029"


def test_case_insensitive() -> None:
    m = UpstoxInstrumentMaster(records=_RECORDS)
    assert m.resolve("reliance") == "NSE_EQ|INE002A01018"


def test_unknown_symbol_returns_none() -> None:
    m = UpstoxInstrumentMaster(records=_RECORDS)
    assert m.resolve("NOSUCH") is None


def test_only_indexes_nse_equity() -> None:
    m = UpstoxInstrumentMaster(records=_RECORDS)
    # The FO RELIANCE row must not clobber the equity one.
    assert m.resolve("RELIANCE").startswith("NSE_EQ|")
    # Non-equity segment isn't resolvable.
    assert m.resolve("NIFTY 50") is None
    assert m.resolve("RELIANCE", segment="NSE_FO") is None


def test_symbols_listing() -> None:
    m = UpstoxInstrumentMaster(records=_RECORDS)
    assert m.symbols() == ["RELIANCE", "TCS"]


def test_wires_into_upstox_provider_resolver() -> None:
    # The master's resolve() must satisfy UpstoxProvider's resolve hook
    # signature (symbol, segment) -> key | None.
    m = UpstoxInstrumentMaster(records=_RECORDS)
    p = UpstoxProvider(access_token="X", resolve=m.resolve)
    # We don't hit the network here; just confirm resolution wiring by
    # checking the private resolver path returns the expected key.
    assert p._instrument_key("RELIANCE") == "NSE_EQ|INE002A01018"
    assert p._instrument_key("NOSUCH") is None


def test_corrupt_cache_triggers_redownload(tmp_path, monkeypatch) -> None:
    import gzip
    import json as _json

    cache = tmp_path / "instr.json.gz"
    cache.write_bytes(b"not a valid gzip file")  # simulate truncated cache

    m = UpstoxInstrumentMaster(cache_path=cache)
    good = gzip.compress(_json.dumps(_RECORDS).encode())
    monkeypatch.setattr(m, "_download", lambda: good)

    # Corrupt cache must be discarded and re-downloaded, not wedge resolve.
    assert m.resolve("RELIANCE") == "NSE_EQ|INE002A01018"
    # And the cache is now repaired (valid gz written atomically).
    assert cache.read_bytes() == good


def test_resolver_outage_propagates_as_provider_error() -> None:
    # A resolver that raises ProviderError (e.g. download failed) must
    # propagate through UpstoxProvider — NOT be swallowed as "unknown".
    def _boom(_sym, _seg):
        raise ProviderError("instrument master download failed")

    p = UpstoxProvider(access_token="X", resolve=_boom)
    with pytest.raises(ProviderError, match="download failed"):
        p._instrument_key("RELIANCE")
