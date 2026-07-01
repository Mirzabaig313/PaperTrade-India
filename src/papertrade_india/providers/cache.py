"""Local caches used by the price feed.

Two layers:

- :class:`InMemoryShortCache` — sub-second TTL, absorbs bursts (e.g. one
  ``get_positions()`` call asking for 30 quotes).
- :class:`CachedLastKnownProvider` — hour-scale TTL, last line of
  defense when every live provider is down. Pretends to be a
  :class:`MarketDataProvider` so the composite/health layer can
  treat it uniformly.

Both deliberately live outside the broker so they can be shared,
swapped, or replaced (e.g. with Redis) without touching broker code.
"""

from __future__ import annotations

import time
from datetime import datetime
from threading import RLock

from .base import (
    MarketDataProvider,
    MarketQuote,
    ProviderCapability,
    ProviderInfo,
)


class InMemoryShortCache:
    """Tiny TTL cache for the (provider→quote) hot path.

    Used by :class:`papertrade_india.PriceFeed` to avoid hammering an
    upstream when the broker iterates positions in a single tick.
    Thread-safe via ``RLock`` because the limit-order watcher touches
    it from a background thread.
    """

    def __init__(self, ttl_seconds: float = 5.0) -> None:
        self._ttl = float(ttl_seconds)
        # symbol -> (price, wall_time, is_real_time)
        self._data: dict[str, tuple[float, float, bool]] = {}
        self._lock = RLock()

    @property
    def ttl_seconds(self) -> float:
        return self._ttl

    def get(self, symbol: str) -> tuple[float, float, bool] | None:
        """Return ``(price, wall_t, is_real_time)`` if fresh, else ``None``."""
        with self._lock:
            entry = self._data.get(symbol)
            if entry is None:
                return None
            price, t, is_real_time = entry
            if (time.time() - t) >= self._ttl:
                return None
            return price, t, is_real_time

    def put(self, symbol: str, price: float, is_real_time: bool = True) -> None:
        """Store the latest price + its real-time provenance.

        ``is_real_time`` is carried so a cache hit can't launder a
        delayed quote into a real-time one (which would defeat the
        broker's ``enforce_real_time`` guard).
        """
        with self._lock:
            self._data[symbol] = (price, time.time(), is_real_time)

    def clear(self) -> None:
        """Drop everything (test helper)."""
        with self._lock:
            self._data.clear()


class CachedLastKnownProvider(MarketDataProvider):
    """Long-lived "last known good" cache, surfaced as a provider.

    When every live provider fails, this is what keeps the broker
    answering — at the cost of staleness. The broker uses
    :attr:`MarketQuote.is_real_time` to detect the staleness path
    (``False`` for cache hits) and the ``enforce_fresh_prices`` mode
    rejects fills served from here.
    """

    def __init__(self, ttl_seconds: int = 60 * 60) -> None:
        self._ttl = int(ttl_seconds)
        self._cache: dict[str, tuple[float, datetime]] = {}
        self._lock = RLock()

    @property
    def info(self) -> ProviderInfo:
        return ProviderInfo(
            name="long_cache",
            description="Last-known-price cache used as the final fallback.",
            capabilities=(
                ProviderCapability.LAST_PRICE
                | ProviderCapability.SUPPORTS_NSE
                | ProviderCapability.SUPPORTS_BSE
                | ProviderCapability.DELAYED
            ),
            requires_network=False,
            notes="Returns the most recently fetched live price within TTL.",
        )

    @property
    def ttl(self) -> int:
        return self._ttl

    # ── Lifecycle ─────────────────────────────────────────────────────

    def update(self, symbol: str, price: float) -> None:
        """Record a fresh observation."""
        with self._lock:
            self._cache[symbol] = (float(price), datetime.now())

    def get_price(self, symbol: str) -> float | None:
        entry = self.get_entry(symbol)
        return entry[0] if entry is not None else None

    def get_entry(self, symbol: str) -> tuple[float, datetime] | None:
        """Return ``(price, fetched_at)`` if fresh enough, else ``None``."""
        with self._lock:
            entry = self._cache.get(symbol)
            if entry is None:
                return None
            price, fetched_at = entry
            if (datetime.now() - fetched_at).total_seconds() > self._ttl:
                return None
            return price, fetched_at

    def get_quote(self, symbol: str) -> MarketQuote | None:
        entry = self.get_entry(symbol)
        if entry is None:
            return None
        price, fetched_at = entry
        return MarketQuote(
            last=price,
            timestamp=fetched_at,
            source="long_cache",
            is_real_time=False,
        )

    def clear(self) -> None:
        """Drop everything (test helper)."""
        with self._lock:
            self._cache.clear()
