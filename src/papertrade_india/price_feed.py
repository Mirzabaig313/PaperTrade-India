"""Price feed — coordinates the chain of market-data providers.

Backwards-compat layer over :mod:`papertrade_india.providers`. Existing
callers see the same surface they always have:

  - :class:`PriceProvider` Protocol (any object with a ``get_price`` method)
  - :class:`YFinanceProvider`, :class:`JugaadDataProvider`,
    :class:`CachedLastKnownProvider`
  - :class:`PriceFeed` with ``get_price`` and ``get_quote``
  - :class:`Quote` (legacy quote dataclass)

New code should prefer :mod:`papertrade_india.providers` directly:

  >>> from papertrade_india.providers import (
  ...     YFinanceProvider, MedianAggregation, CompositeProvider,
  ...     CircuitBreakerProvider, MarketQuote,
  ... )

The bridge:

- :class:`PriceFeed` accepts both legacy ``PriceProvider`` objects (only
  ``get_price``) and the new :class:`MarketDataProvider` ABC, so old
  test stubs keep working.
- :class:`Quote` and :class:`MarketQuote` are interchangeable in spirit;
  :class:`Quote` is the narrower view (``price``, ``source``,
  ``fetched_at``, ``is_stale``) the broker already consumes.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from .exceptions import PriceUnavailableError
from .providers import (
    CachedLastKnownProvider,
    InMemoryShortCache,
    JugaadDataProvider,
    MarketDataProvider,
    MarketQuote,
    YFinanceProvider,
)
from .providers.base import ProviderError as _ProviderError

logger = logging.getLogger(__name__)


# ── Legacy types kept for back-compat ────────────────────────────────


@dataclass(frozen=True)
class Quote:
    """A price observation with provenance (legacy view).

    Newer code should prefer :class:`papertrade_india.providers.MarketQuote`,
    which carries bid/ask/volume/OHLC. ``Quote`` keeps the narrower
    surface the broker already consumes (price + source + fetched_at +
    is_stale) so existing callers don't need to migrate.
    """

    price: float
    source: str
    fetched_at: datetime
    is_stale: bool


class PriceProvider(Protocol):
    """Anything with a ``get_price(symbol) -> float | None``.

    Both legacy stubs and the new :class:`MarketDataProvider` satisfy
    this. Kept for backwards compatibility with ``PriceFeed(providers=[...])``.
    """

    def get_price(self, symbol: str) -> float | None:
        ...


# Legacy aliases — kept for ``from papertrade_india import ...``
__all__ = [
    "Quote",
    "PriceProvider",
    "PriceFeed",
    "YFinanceProvider",
    "JugaadDataProvider",
    "CachedLastKnownProvider",
]


# ── Coordinator ───────────────────────────────────────────────────────


class PriceFeed:
    """Multi-provider price feed with fallback chain.

    Tries each provider in order, returns the first non-``None`` price.
    Logs every fallback so degradation is visible in the structured log.

    The class accepts both legacy-shaped ``PriceProvider`` objects and
    new :class:`MarketDataProvider` instances. When a provider is the
    new shape, ``get_quote`` calls flow through ``provider.get_quote()``
    (which carries source/timestamp/staleness directly); when it's the
    legacy shape we fabricate a quote with ``source=type(provider).__name__``
    and ``fetched_at=now``.
    """

    def __init__(
        self,
        providers: list[PriceProvider | MarketDataProvider] | None = None,
        cache_ttl_seconds: int = 60 * 60,
        short_cache_ttl_seconds: float = 5.0,
    ) -> None:
        self.cache = CachedLastKnownProvider(ttl_seconds=cache_ttl_seconds)
        self.providers: list[PriceProvider | MarketDataProvider] = (
            providers
            if providers is not None
            else [YFinanceProvider("NS"), JugaadDataProvider()]
        )
        self._short_cache = InMemoryShortCache(ttl_seconds=short_cache_ttl_seconds)

    def get_price(self, symbol: str) -> float:
        """Backwards-compatible bare-float accessor."""
        return self.get_quote(symbol).price

    def get_quote(self, symbol: str) -> Quote:
        """Fetch a price with provenance.

        Tries the short cache, then each live provider in order, then
        the long-lived cache. Always returns a :class:`Quote` or raises
        :class:`PriceUnavailableError` — never silently degrades.
        """
        # Short cache check — counts as fresh, source = "short_cache".
        cached = self._short_cache.get(symbol)
        if cached is not None:
            price, t = cached
            return Quote(
                price=price,
                source="short_cache",
                fetched_at=datetime.fromtimestamp(t),
                is_stale=False,
            )

        for provider in self.providers:
            quote = self._call_provider(provider, symbol)
            if quote is not None:
                self.cache.update(symbol, quote.last)
                self._short_cache.put(symbol, quote.last)
                # Quote.is_stale historically means "served from the
                # long cache", *not* "delayed feed". A live yfinance
                # quote is delayed but not stale in this sense.
                return Quote(
                    price=quote.last,
                    source=quote.source,
                    fetched_at=quote.timestamp,
                    is_stale=False,
                )

        # Last resort: long-lived cache.
        cached_quote = self.cache.get_quote(symbol)
        if cached_quote is not None:
            logger.warning(
                "Using cached price for %s — all live providers failed "
                "(cache age: %s)",
                symbol, datetime.now() - cached_quote.timestamp,
            )
            return Quote(
                price=cached_quote.last,
                source=cached_quote.source,
                fetched_at=cached_quote.timestamp,
                is_stale=True,
            )

        raise PriceUnavailableError(
            f"Cannot fetch price for {symbol} — "
            f"all providers failed and no cached value is available",
        )

    def prime(self, symbol: str, price: float) -> None:
        """Seed the cache (useful in tests, or with EOD bhavcopy data)."""
        self.cache.update(symbol, price)
        self._short_cache.put(symbol, price)

    # ── Internals ─────────────────────────────────────────────────────

    def _call_provider(
        self,
        provider: PriceProvider | MarketDataProvider,
        symbol: str,
    ) -> MarketQuote | None:
        """Call ``provider`` and normalize its return into a MarketQuote.

        Accepts both legacy ``get_price(symbol) -> float | None`` providers
        and new ``MarketDataProvider``s.
        """
        # New-style provider: prefer get_quote when present.
        if isinstance(provider, MarketDataProvider):
            try:
                return provider.get_quote(symbol)
            except _ProviderError as e:
                logger.warning("provider %s raised: %s", provider.name, e)
                return None
            except Exception as e:  # noqa: BLE001 — defensive
                logger.warning(
                    "provider %s raised unexpectedly: %s",
                    provider.name, e,
                )
                return None

        # Legacy-style provider: thin shim.
        try:
            price = provider.get_price(symbol)
        except Exception as e:  # noqa: BLE001 — defensive
            logger.warning(
                "Provider %s raised: %s", type(provider).__name__, e,
            )
            return None
        if price is None:
            return None
        return MarketQuote(
            last=float(price),
            timestamp=datetime.now(),
            source=type(provider).__name__,
            is_real_time=False,
        )

    # ── Convenience for new code ─────────────────────────────────────

    def get_market_quote(self, symbol: str) -> MarketQuote:
        """Return the rich :class:`MarketQuote` (bid/ask/volume/OHLC).

        Companion to :meth:`get_quote`. Useful for callers that want
        the full quote shape without going through the legacy
        :class:`Quote` adapter.
        """
        for provider in self.providers:
            quote = self._call_provider(provider, symbol)
            if quote is not None:
                self.cache.update(symbol, quote.last)
                self._short_cache.put(symbol, quote.last)
                return quote
        cached_quote = self.cache.get_quote(symbol)
        if cached_quote is not None:
            return cached_quote
        raise PriceUnavailableError(
            f"Cannot fetch price for {symbol} — all providers failed "
            f"and no cached value is available",
        )


# ``time`` is only used to keep the legacy import surface stable for any
# downstream that imported the module with ``import time``-side effects.
_ = time
