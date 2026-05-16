"""Price feed with fallback chain.

We layer providers so a transient yfinance outage doesn't take the broker
down:

    yfinance  →  jugaad-data  →  cached last-known

Every successful fetch updates the persistent cache (TTL-bounded) and the
short-lived in-memory cache that absorbs rapid repeat calls (e.g.
``get_positions()`` for many holdings in one tick).

Fail behavior: if every provider returns ``None`` AND the persistent cache
is expired, ``PriceFeed.get_price`` raises ``PriceUnavailableError``. The
broker treats this as fatal for new orders; for valuation of existing
positions it falls back to ``avg_cost`` (i.e. shows zero unrealized P&L).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Protocol

from .exceptions import PriceUnavailableError

logger = logging.getLogger(__name__)


class PriceProvider(Protocol):
    """Anything that can return a last/spot price for a symbol."""

    def get_price(self, symbol: str) -> float | None:
        ...


# ── Concrete providers ────────────────────────────────────────────────


class YFinanceProvider:
    """Primary: Yahoo Finance via ``yfinance``.

    NSE symbols use the ``.NS`` suffix (``RELIANCE.NS``); BSE uses ``.BO``.
    yfinance is the path of least resistance — no API key, no signup —
    but Yahoo can rate-limit or change response shapes without notice.
    """

    def __init__(self, exchange_suffix: str = "NS") -> None:
        self.suffix = exchange_suffix

    def get_price(self, symbol: str) -> float | None:
        try:
            # Lazy import: keeps the rest of the package importable when
            # yfinance is unavailable (e.g. minimal CI containers).
            import yfinance as yf

            ticker = yf.Ticker(f"{symbol}.{self.suffix}")
            # ``fast_info`` is faster than ``info`` but occasionally
            # returns dicts missing keys; fall through to history.
            try:
                fi = ticker.fast_info
                price = (
                    fi.get("lastPrice")
                    or fi.get("last_price")
                    or fi.get("previousClose")
                    or fi.get("previous_close")
                )
                if price:
                    return float(price)
            except Exception:  # noqa: BLE001 — fast_info shape is volatile
                pass

            hist = ticker.history(period="1d")
            if not hist.empty:
                return float(hist["Close"].iloc[-1])
        except Exception as e:  # noqa: BLE001 — network/lib volatility
            logger.warning("YFinance failed for %s: %s", symbol, e)
        return None


class JugaadDataProvider:
    """Fallback: ``jugaad-data`` scrapes NSE directly. NSE only.

    jugaad-data is community-maintained and fragile by nature (it parses
    the NSE site). Use only as fallback — the community keeps it working
    for most common symbols, but treat failures as expected.
    """

    def get_price(self, symbol: str) -> float | None:
        try:
            from jugaad_data.nse import NSELive  # type: ignore

            n = NSELive()
            data = n.stock_quote(symbol)
            return float(data["priceInfo"]["lastPrice"])
        except ImportError:
            logger.debug(
                "jugaad-data not installed; skipping fallback. "
                "Install with: pip install 'papertrade-india[jugaad]'"
            )
            return None
        except Exception as e:  # noqa: BLE001 — scraper is volatile
            logger.warning("jugaad-data failed for %s: %s", symbol, e)
            return None


class CachedLastKnownProvider:
    """Final fallback: most recently fetched price for each symbol."""

    def __init__(self, ttl_seconds: int = 3600) -> None:
        self._cache: dict[str, tuple[float, datetime]] = {}
        self.ttl = ttl_seconds

    def update(self, symbol: str, price: float) -> None:
        self._cache[symbol] = (price, datetime.now())

    def get_price(self, symbol: str) -> float | None:
        entry = self._cache.get(symbol)
        if entry is None:
            return None
        price, fetched_at = entry
        if (datetime.now() - fetched_at).total_seconds() > self.ttl:
            return None
        return price


# ── Coordinator ───────────────────────────────────────────────────────


class PriceFeed:
    """Multi-provider price feed with fallback chain.

    Tries each provider in order, returns the first non-``None`` price.
    Logs every fallback so degradation is visible.
    """

    def __init__(
        self,
        providers: list[PriceProvider] | None = None,
        cache_ttl_seconds: int = 60 * 60,
        short_cache_ttl_seconds: float = 5.0,
    ) -> None:
        self.cache = CachedLastKnownProvider(ttl_seconds=cache_ttl_seconds)
        # Order matters — primary first.
        self.providers = (
            providers
            if providers is not None
            else [YFinanceProvider("NS"), JugaadDataProvider()]
        )
        # In-memory short-lived cache to avoid hammering yfinance for
        # multiple positions in the same ``get_positions()`` call.
        self._short_cache: dict[str, tuple[float, float]] = {}
        self._short_cache_ttl = short_cache_ttl_seconds

    def get_price(self, symbol: str) -> float:
        # Short cache check
        entry = self._short_cache.get(symbol)
        if entry is not None:
            price, t = entry
            if (time.time() - t) < self._short_cache_ttl:
                return price

        for provider in self.providers:
            try:
                price = provider.get_price(symbol)
            except Exception as e:  # noqa: BLE001 — defensive
                logger.warning(
                    "Provider %s raised: %s", type(provider).__name__, e,
                )
                price = None
            if price is not None:
                self.cache.update(symbol, price)
                self._short_cache[symbol] = (price, time.time())
                return price

        # Last resort: long-lived cache
        cached = self.cache.get_price(symbol)
        if cached is not None:
            logger.warning(
                "Using cached price for %s — all live providers failed",
                symbol,
            )
            return cached

        raise PriceUnavailableError(
            f"Cannot fetch price for {symbol} — "
            f"all providers failed and no cached value is available"
        )

    def prime(self, symbol: str, price: float) -> None:
        """Seed the cache (useful in tests, or with EOD bhavcopy data)."""
        self.cache.update(symbol, price)
        self._short_cache[symbol] = (price, time.time())
