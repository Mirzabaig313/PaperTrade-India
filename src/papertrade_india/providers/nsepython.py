"""nsepython provider — another NSE-direct fallback.

``nsepython`` is the actively-maintained spiritual successor to a few
older NSE scrapers. Like ``jugaad-data`` it scrapes the public NSE site
directly, but its API stays a bit closer to the live JSON shape and it
generally exposes bid/ask/volume for liquid names.

Optional dep: install with ``pip install nsepython``.
"""

from __future__ import annotations

import logging
from datetime import datetime

from .base import (
    MarketDataProvider,
    MarketQuote,
    ProviderCapability,
    ProviderError,
    ProviderInfo,
)

logger = logging.getLogger(__name__)


class NSEPythonProvider(MarketDataProvider):
    """NSE direct via the ``nsepython`` package."""

    @property
    def info(self) -> ProviderInfo:
        return ProviderInfo(
            name="nsepython",
            description="Direct NSE quotes via the nsepython package.",
            capabilities=(
                ProviderCapability.LAST_PRICE
                | ProviderCapability.QUOTE
                | ProviderCapability.SUPPORTS_NSE
                | ProviderCapability.DELAYED
            ),
            requires_api_key=False,
            homepage="https://github.com/aeron7/nsepython",
            notes="Scraper. Treat failures as expected.",
        )

    def get_quote(self, symbol: str) -> MarketQuote | None:
        try:
            from nsepython import nsefetch  # type: ignore
        except ImportError as e:
            raise ProviderError("nsepython is not installed") from e

        url = (
            "https://www.nseindia.com/api/quote-equity?symbol="
            f"{symbol.upper()}"
        )
        try:
            data = nsefetch(url)
        except Exception as e:  # noqa: BLE001
            logger.warning("nsepython failed for %s: %s", symbol, e)
            raise ProviderError(f"nsepython failed for {symbol}: {e}") from e

        if not isinstance(data, dict):
            return None
        price_info = data.get("priceInfo") or {}
        if not price_info:
            return None
        try:
            last = float(price_info.get("lastPrice"))
        except (TypeError, ValueError):
            return None

        ohl = price_info.get("intraDayHighLow") or {}
        return MarketQuote(
            last=last,
            timestamp=datetime.now(),
            open=_f(price_info.get("open")),
            high=_f(ohl.get("max")),
            low=_f(ohl.get("min")),
            prev_close=_f(price_info.get("previousClose")),
            volume=_i((data.get("preOpenMarket") or {}).get("totalTradedVolume")),
            source="nsepython",
            is_real_time=False,
        )


def _f(x: object) -> float | None:
    try:
        if x is None:
            return None
        v = float(x)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def _i(x: object) -> int | None:
    f = _f(x)
    return int(f) if f is not None else None
