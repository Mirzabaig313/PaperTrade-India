"""Unit tests for the provider registry."""

from __future__ import annotations

from datetime import datetime

import pytest

from papertrade_india.providers import (
    MarketDataProvider,
    MarketQuote,
    ProviderCapability,
    ProviderInfo,
    ProviderRegistry,
    default_registry,
)


class _Dummy(MarketDataProvider):
    @property
    def info(self) -> ProviderInfo:
        return ProviderInfo(
            name="dummy",
            description="test",
            capabilities=ProviderCapability.LAST_PRICE,
        )

    def get_quote(self, symbol: str) -> MarketQuote | None:
        return MarketQuote(last=1.0, timestamp=datetime.now(), source="dummy")


def test_register_and_get() -> None:
    reg = ProviderRegistry()
    reg.register("dummy", _Dummy().info, lambda: _Dummy())
    p = reg.get("dummy")
    assert isinstance(p, _Dummy)


def test_get_unknown_raises() -> None:
    reg = ProviderRegistry()
    with pytest.raises(KeyError):
        reg.get("nope")


def test_get_unavailable_raises() -> None:
    reg = ProviderRegistry()
    reg.register(
        "dummy",
        _Dummy().info,
        lambda: _Dummy(),
        available=False,
    )
    with pytest.raises(RuntimeError, match="dependencies"):
        reg.get("dummy")


def test_names_filter_only_available() -> None:
    reg = ProviderRegistry()
    reg.register("a", _Dummy().info, lambda: _Dummy(), available=True)
    reg.register("b", _Dummy().info, lambda: _Dummy(), available=False)
    assert reg.names() == ["a", "b"]
    assert reg.names(only_available=True) == ["a"]


def test_default_registry_has_core_providers() -> None:
    names = set(default_registry.names())
    # These are always registered (deps are stdlib or always-installed).
    assert "yfinance" in names
    assert "jugaad-data" in names
    assert "stooq" in names
    assert "nse-bhavcopy" in names


def test_default_registry_info_is_well_formed() -> None:
    info = default_registry.info("stooq")
    assert info.name == "stooq"
    assert info.requires_api_key is False
    assert ProviderCapability.LAST_PRICE in info.capabilities


def test_case_insensitive_lookup() -> None:
    reg = ProviderRegistry()
    reg.register("MixedCase", _Dummy().info, lambda: _Dummy())
    # Lookup should work regardless of case.
    assert reg.get("mixedcase") is not None
    assert reg.get("MIXEDCASE") is not None
