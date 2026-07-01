"""Data-provider subpackage for papertrade-india.

This package owns every external market-data source the broker can talk
to. The headline split is:

- :mod:`base` — the :class:`MarketDataProvider` ABC, capability flags,
  and the rich data types (``MarketQuote``, ``OHLCV``) that providers
  surface.
- One module per provider (yfinance, jugaad-data, nsepython, stooq,
  alpha vantage, twelve data, finnhub, NSE bhavcopy). Each module
  implements ``MarketDataProvider`` and is optional at install time —
  the package imports cleanly even when third-party deps are missing.
- :mod:`health` — circuit-breaker wrapper that tracks per-provider
  failure rates and trips out failing providers without restart.
- :mod:`composite` — fan-out that aggregates quotes from multiple
  providers (median by default) for higher fidelity than first-wins.
- :mod:`registry` — name → provider lookup so config and the CLI can
  refer to providers by stable identifiers.

Backwards-compat
----------------
The legacy :class:`papertrade_india.PriceProvider` ``Protocol`` is the
narrow ``get_price(symbol) -> float | None`` contract. Every new
provider here also satisfies that protocol via its ``get_price``
method, so anything that consumes the old protocol keeps working
unchanged.
"""

from __future__ import annotations

from .base import (
    OHLCV,
    MarketDataProvider,
    MarketQuote,
    ProviderCapability,
    ProviderError,
    ProviderInfo,
)
from .cache import CachedLastKnownProvider, InMemoryShortCache
from .composite import CompositeProvider, MedianAggregation
from .health import CircuitBreakerProvider, ProviderHealth
from .jugaad import JugaadDataProvider
from .nse_bhavcopy import NSEBhavcopyProvider
from .registry import ProviderRegistry, default_registry
from .stooq import StooqProvider
from .yfinance import YFinanceProvider

# Optional providers — imported lazily inside try/except so a missing
# third-party dep doesn't break ``from papertrade_india.providers import ...``.
try:
    from .nsepython import NSEPythonProvider
except Exception:  # noqa: BLE001
    NSEPythonProvider = None  # type: ignore[assignment]

try:
    from .alphavantage import AlphaVantageProvider
except Exception:  # noqa: BLE001
    AlphaVantageProvider = None  # type: ignore[assignment]

try:
    from .twelvedata import TwelveDataProvider
except Exception:  # noqa: BLE001
    TwelveDataProvider = None  # type: ignore[assignment]

try:
    from .finnhub import FinnhubProvider
except Exception:  # noqa: BLE001
    FinnhubProvider = None  # type: ignore[assignment]

# Broker-feed providers (real bid/ask + depth). Kite/Dhan need a vendor
# SDK; Upstox is stdlib REST. All are optional and credential-gated.
try:
    from .kite import KiteProvider
except Exception:  # noqa: BLE001
    KiteProvider = None  # type: ignore[assignment]

try:
    from .dhan import DhanProvider
except Exception:  # noqa: BLE001
    DhanProvider = None  # type: ignore[assignment]

try:
    from .upstox import UpstoxProvider
except Exception:  # noqa: BLE001
    UpstoxProvider = None  # type: ignore[assignment]

from .upstox_instruments import UpstoxInstrumentMaster


__all__ = [
    # Base interface
    "MarketDataProvider",
    "MarketQuote",
    "OHLCV",
    "ProviderCapability",
    "ProviderError",
    "ProviderInfo",
    # Cache
    "CachedLastKnownProvider",
    "InMemoryShortCache",
    # Resilience
    "CircuitBreakerProvider",
    "ProviderHealth",
    # Aggregation / lookup
    "CompositeProvider",
    "MedianAggregation",
    "ProviderRegistry",
    "default_registry",
    # Concrete providers (always available)
    "YFinanceProvider",
    "JugaadDataProvider",
    "StooqProvider",
    "NSEBhavcopyProvider",
    # Concrete providers (optional)
    "NSEPythonProvider",
    "AlphaVantageProvider",
    "TwelveDataProvider",
    "FinnhubProvider",
    # Broker-feed providers (optional)
    "KiteProvider",
    "DhanProvider",
    "UpstoxProvider",
    "UpstoxInstrumentMaster",
]
