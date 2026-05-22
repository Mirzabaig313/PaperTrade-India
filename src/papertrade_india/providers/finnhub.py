"""Finnhub provider — REST API with a free tier (60 req/min).

Sets a real-time-ish (~quote-level) feed on the free tier for many
exchanges, including BSE and NSE. API key from env ``FINNHUB_API_KEY``
by default.

Indian symbols: append ``.NS`` for NSE, ``.BO`` for BSE on Finnhub.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request
from datetime import datetime
from urllib.error import HTTPError, URLError

from .base import (
    MarketDataProvider,
    MarketQuote,
    ProviderCapability,
    ProviderError,
    ProviderInfo,
)

logger = logging.getLogger(__name__)

_BASE = "https://finnhub.io/api/v1"


class FinnhubProvider(MarketDataProvider):
    """Finnhub REST API."""

    def __init__(
        self,
        api_key: str | None = None,
        suffix: str = "NS",
        timeout: float = 10.0,
    ) -> None:
        self._api_key = api_key or os.environ.get("FINNHUB_API_KEY")
        self._suffix = suffix.lstrip(".")
        self._timeout = float(timeout)

    @property
    def info(self) -> ProviderInfo:
        caps = (
            ProviderCapability.LAST_PRICE
            | ProviderCapability.QUOTE
            | ProviderCapability.REAL_TIME
        )
        if self._suffix.upper() == "NS":
            caps |= ProviderCapability.SUPPORTS_NSE
        elif self._suffix.upper() == "BO":
            caps |= ProviderCapability.SUPPORTS_BSE
        return ProviderInfo(
            name="finnhub",
            description="Finnhub REST API.",
            capabilities=caps,
            requires_api_key=True,
            homepage="https://finnhub.io/",
            notes="Set FINNHUB_API_KEY. Free tier: 60 req/min.",
        )

    def get_quote(self, symbol: str) -> MarketQuote | None:
        if not self._api_key:
            raise ProviderError(
                "Finnhub API key missing. Set FINNHUB_API_KEY or pass api_key=...",
            )
        params = urllib.parse.urlencode(
            {"symbol": f"{symbol.upper()}.{self._suffix}", "token": self._api_key},
        )
        url = f"{_BASE}/quote?{params}"
        try:
            with urllib.request.urlopen(url, timeout=self._timeout) as resp:  # noqa: S310
                body = resp.read().decode("utf-8", errors="replace")
        except HTTPError as e:
            if e.code in (401, 403):
                raise ProviderError(f"finnhub auth failed (HTTP {e.code})") from e
            raise ProviderError(f"finnhub HTTP {e.code}") from e
        except (URLError, TimeoutError) as e:
            raise ProviderError(f"finnhub fetch failed: {e}") from e

        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            raise ProviderError(f"finnhub non-JSON: {body[:200]}") from e

        # Finnhub returns ``{"c": 0, ...}`` for unknown symbols.
        try:
            last = float(data.get("c") or 0)
        except (TypeError, ValueError):
            return None
        if last <= 0:
            return None

        timestamp = (
            datetime.fromtimestamp(int(data["t"]))
            if isinstance(data.get("t"), (int, float)) and data["t"] > 0
            else datetime.now()
        )
        return MarketQuote(
            last=last,
            timestamp=timestamp,
            open=_f(data.get("o")),
            high=_f(data.get("h")),
            low=_f(data.get("l")),
            prev_close=_f(data.get("pc")),
            source="finnhub",
            is_real_time=True,  # Finnhub is generally near-real-time
        )


def _f(x: object) -> float | None:
    try:
        if x is None:
            return None
        v = float(x)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None
