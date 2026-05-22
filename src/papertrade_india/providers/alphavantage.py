"""Alpha Vantage provider — REST API, free tier (5 req/min).

API key from env var ``ALPHA_VANTAGE_API_KEY`` by default. The free
tier is heavily rate-limited; pair with the circuit breaker and reserve
this provider for backtests + occasional live checks.

Capabilities
------------
- LAST_PRICE, QUOTE (last/open/high/low/prev_close/volume)
- OHLCV_DAILY (via TIME_SERIES_DAILY function)
- Indian symbols use the ``.BSE`` suffix on Alpha Vantage; NSE coverage
  is partial. Check :class:`MarketQuote.last is None` for misses.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request
from datetime import date, datetime
from urllib.error import HTTPError, URLError

from .base import (
    OHLCV,
    MarketDataProvider,
    MarketQuote,
    ProviderCapability,
    ProviderError,
    ProviderInfo,
)

logger = logging.getLogger(__name__)

_BASE = "https://www.alphavantage.co/query"


class AlphaVantageProvider(MarketDataProvider):
    """Alpha Vantage REST quotes."""

    def __init__(
        self,
        api_key: str | None = None,
        suffix: str = ".BSE",
        timeout: float = 10.0,
    ) -> None:
        self._api_key = api_key or os.environ.get("ALPHA_VANTAGE_API_KEY")
        self._suffix = suffix
        self._timeout = float(timeout)

    @property
    def info(self) -> ProviderInfo:
        return ProviderInfo(
            name="alphavantage",
            description="Alpha Vantage REST API.",
            capabilities=(
                ProviderCapability.LAST_PRICE
                | ProviderCapability.QUOTE
                | ProviderCapability.OHLCV_DAILY
                | ProviderCapability.SUPPORTS_BSE
                | ProviderCapability.DELAYED
            ),
            requires_api_key=True,
            homepage="https://www.alphavantage.co/",
            notes="Free tier capped at 5 req/min; set ALPHA_VANTAGE_API_KEY.",
        )

    def _ticker(self, symbol: str) -> str:
        return f"{symbol.upper()}{self._suffix}"

    def _fetch_json(self, params: dict[str, str]) -> dict:
        if not self._api_key:
            raise ProviderError(
                "Alpha Vantage API key missing. "
                "Set ALPHA_VANTAGE_API_KEY or pass api_key=..."
            )
        params = {**params, "apikey": self._api_key}
        url = f"{_BASE}?{urllib.parse.urlencode(params)}"
        try:
            with urllib.request.urlopen(url, timeout=self._timeout) as resp:  # noqa: S310
                body = resp.read().decode("utf-8", errors="replace")
        except HTTPError as e:
            raise ProviderError(f"alphavantage HTTP {e.code}") from e
        except (URLError, TimeoutError) as e:
            raise ProviderError(f"alphavantage fetch failed: {e}") from e
        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            raise ProviderError(f"alphavantage non-JSON response: {body[:200]}") from e
        if "Note" in data or "Information" in data:
            raise ProviderError(
                "alphavantage rate-limited: "
                f"{data.get('Note') or data.get('Information')}",
            )
        if "Error Message" in data:
            return {}
        return data

    def get_quote(self, symbol: str) -> MarketQuote | None:
        data = self._fetch_json(
            {"function": "GLOBAL_QUOTE", "symbol": self._ticker(symbol)},
        )
        gq = data.get("Global Quote") or data.get("globalQuote") or {}
        if not gq:
            return None
        try:
            last = float(gq.get("05. price") or 0)
        except (TypeError, ValueError):
            return None
        if last <= 0:
            return None

        return MarketQuote(
            last=last,
            timestamp=datetime.now(),
            open=_f(gq.get("02. open")),
            high=_f(gq.get("03. high")),
            low=_f(gq.get("04. low")),
            prev_close=_f(gq.get("08. previous close")),
            volume=_i(gq.get("06. volume")),
            source="alphavantage",
            is_real_time=False,
        )

    def get_history(
        self,
        symbol: str,
        start: date,
        end: date,
        interval: str = "1d",
    ) -> list[OHLCV]:
        if interval != "1d":
            return []
        data = self._fetch_json(
            {
                "function": "TIME_SERIES_DAILY",
                "symbol": self._ticker(symbol),
                "outputsize": "full",
            },
        )
        series = data.get("Time Series (Daily)") or {}
        out: list[OHLCV] = []
        for date_str, row in series.items():
            try:
                d = datetime.strptime(date_str, "%Y-%m-%d").date()
                if d < start or d > end:
                    continue
                out.append(
                    OHLCV(
                        timestamp=datetime.combine(d, datetime.min.time()),
                        open=float(row["1. open"]),
                        high=float(row["2. high"]),
                        low=float(row["3. low"]),
                        close=float(row["4. close"]),
                        volume=int(float(row["5. volume"])),
                    )
                )
            except (KeyError, ValueError):
                continue
        return sorted(out, key=lambda b: b.timestamp)


def _f(x: object) -> float | None:
    try:
        if x is None:
            return None
        v = float(x)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def _i(x: object) -> int | None:
    try:
        if x is None:
            return None
        return int(float(x))
    except (TypeError, ValueError):
        return None
