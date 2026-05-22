"""Provider registry — lookup by stable name.

The registry is a thin map from a stable, lowercased name (``"yfinance"``,
``"jugaad-data"``, ``"stooq"``, …) to a factory that builds the provider.
Used by:

- The CLI (``papertrade-india providers list``) to enumerate what's
  available and what's installed.
- Config-driven setup so users can declare provider chains in env / TOML
  without hard-coding imports in their app.
- Tests to discover what to monkeypatch.

Why factories instead of instances? Most providers are cheap to build,
but a few (Finnhub, Twelve Data) read API keys at construction time —
deferring instantiation to "the caller asked for it" keeps the import
side-effect free.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from .base import MarketDataProvider, ProviderInfo

logger = logging.getLogger(__name__)


ProviderFactory = Callable[..., MarketDataProvider]


@dataclass
class _RegistryEntry:
    info: ProviderInfo
    factory: ProviderFactory
    available: bool


class ProviderRegistry:
    """Name → factory map for every known provider.

    Unknown providers raise :class:`KeyError`. Use :meth:`available` to
    list only those whose third-party deps are installed.
    """

    def __init__(self) -> None:
        self._entries: dict[str, _RegistryEntry] = {}

    def register(
        self,
        name: str,
        info: ProviderInfo,
        factory: ProviderFactory,
        available: bool = True,
    ) -> None:
        self._entries[name.lower()] = _RegistryEntry(
            info=info, factory=factory, available=available,
        )

    def get(self, name: str, **kwargs: object) -> MarketDataProvider:
        entry = self._entries.get(name.lower())
        if entry is None:
            raise KeyError(f"Unknown provider: {name!r}")
        if not entry.available:
            raise RuntimeError(
                f"Provider {name!r} is registered but its dependencies "
                f"are not installed. Notes: {entry.info.notes}",
            )
        return entry.factory(**kwargs)

    def info(self, name: str) -> ProviderInfo:
        entry = self._entries.get(name.lower())
        if entry is None:
            raise KeyError(f"Unknown provider: {name!r}")
        return entry.info

    def names(self, *, only_available: bool = False) -> list[str]:
        return sorted(
            n for n, e in self._entries.items()
            if (not only_available) or e.available
        )

    def all(self) -> dict[str, ProviderInfo]:
        return {n: e.info for n, e in self._entries.items()}

    def available(self) -> dict[str, ProviderInfo]:
        return {n: e.info for n, e in self._entries.items() if e.available}


def _build_default_registry() -> ProviderRegistry:
    """Populate the registry with every known provider.

    Optional providers are registered as ``available=False`` when their
    third-party imports fail, so the CLI can show "yes installed / not
    installed" without crashing.
    """
    reg = ProviderRegistry()

    # Always available — pure-Python or already in the install dep set.
    from .jugaad import JugaadDataProvider
    from .nse_bhavcopy import NSEBhavcopyProvider
    from .stooq import StooqProvider
    from .yfinance import YFinanceProvider

    reg.register(
        "yfinance",
        YFinanceProvider().info,
        lambda **kw: YFinanceProvider(**kw),
        available=_check_import("yfinance"),
    )
    reg.register(
        "jugaad-data",
        JugaadDataProvider().info,
        lambda **kw: JugaadDataProvider(**kw),
        available=_check_import("jugaad_data"),
    )
    reg.register(
        "stooq",
        StooqProvider().info,
        lambda **kw: StooqProvider(**kw),
        available=True,  # stdlib only
    )
    reg.register(
        "nse-bhavcopy",
        NSEBhavcopyProvider().info,
        lambda **kw: NSEBhavcopyProvider(**kw),
        available=True,  # stdlib only
    )

    # Optional providers — register even when deps are missing, so
    # ``providers list`` shows them as "(install <pkg>)".
    try:
        from .nsepython import NSEPythonProvider  # noqa: F401
        reg.register(
            "nsepython",
            NSEPythonProvider().info,
            lambda **kw: NSEPythonProvider(**kw),
            available=_check_import("nsepython"),
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("nsepython provider unavailable: %s", e)

    try:
        from .alphavantage import AlphaVantageProvider
        reg.register(
            "alphavantage",
            AlphaVantageProvider().info,
            lambda **kw: AlphaVantageProvider(**kw),
            available=True,  # stdlib only — auth checked at call time
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("alphavantage unavailable: %s", e)

    try:
        from .twelvedata import TwelveDataProvider
        reg.register(
            "twelvedata",
            TwelveDataProvider().info,
            lambda **kw: TwelveDataProvider(**kw),
            available=True,
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("twelvedata unavailable: %s", e)

    try:
        from .finnhub import FinnhubProvider
        reg.register(
            "finnhub",
            FinnhubProvider().info,
            lambda **kw: FinnhubProvider(**kw),
            available=True,
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("finnhub unavailable: %s", e)

    return reg


def _check_import(module_name: str) -> bool:
    """Light import probe so we can flag unavailable deps."""
    try:
        __import__(module_name)
    except Exception:  # noqa: BLE001
        return False
    return True


# Module-level default registry. Built once on import.
default_registry = _build_default_registry()
