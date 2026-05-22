"""Twelve Data provider — REST API with a free tier.

Free tier: 800 requests/day, 8 req/min. API key required (env
``TWELVE_DATA_API_KEY`` by default). Twelve Data covers Indian equities
on both NSE (``:NSE``) and BSE (``:BSE``) — pass ``exchange="NSE"`` or
``"BSE"`` at construction time.

Capabilities
------------
- LAST_PRICE, QUOTE (last/open/high/low/prev_close/volume + 52w hi/lo)
- OHLCV_DAILY
- DELAYED on free tier (real-time costs more)
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

_BASE = "https://api.twelvedata.com"


class TwelveDataProvider(MarketDataProvider):
    """Twelve Data REST API."""

    def __init__(
        self,
        api_key: str | None = None,
        exchange: str = "NSE",
        timeout: float = 10.0,
    ) -> None:
        self._api_key = api_key or os.environ.get("TWELVE_DATA_API_KEY")
        self._exchange = exchange.upper()
        self._timeout = float(timeout)

    @property
    def info(self) -> ProviderInfo:
        caps = (
            ProviderCapability.LAST_PRICE
            | ProviderCapability.QUOTE
            | ProviderCapability.OHLCV_DAILY
            | ProviderCapability.DELAYED
        )
        if self._exchange == "NSE":
            caps |= ProviderCapability.SUPPORTS_NSE
        elif self._exchange == "BSE":
            caps |= ProviderCapability.SUPPORTS_BSE
        return ProviderInfo(
            name="twelvedata",
            description="Twelve Data REST API for global equities.",
            capabilities=caps,
            requires_api_key=True,
            homepage="https://twelvedata.com/",
            notes="Set TWELVE_DATA_API_KEY. Free tier: 800/day, 8/min.",
        )

    def _fetch(self, path: str, params: dict[str, str]) -> dict:
        if not self._api_key:
            raise ProviderError(
                "Twelve Data API key missing. "
                "Set TWELVE_DATA_API_KEY or pass api_key=...",
            )
        params = {**params, "apikey": self._api_key}
        url = f"{_BASE}{path}?{urllib.parse.urlencode(params)}"
        try:
            with urllib.request.urlopen(url, timeout=self._timeout) as resp:  # noqa: S310
                body = resp.read().decode("utf-8", errors="replace")
        except HTTPError as e:
            raise ProviderError(f"twelvedata HTTP {e.code}") from e
        except (URLError, TimeoutError) as e:
            raise ProviderError(f"twelvedata fetch failed: {e}") from e
        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            raise ProviderError(f"twelvedata non-JSON: {body[:200]}") from e
        if isinstance(data, dict) and data.get("status") == "error":
            code = data.get("code")
            msg = data.get("message", "unknown")
            if code == 404:
                # genuinely unknown symbol — treat as None
                return {}
            raise ProviderError(f"twelvedata error {code}: {msg}")
        return data

    def get_quote(self, symbol: str) -> MarketQuote | None:
        data = self._fetch(
            "/quote",
            {"symbol": symbol.upper(), "exchange": self._exchange},
        )
        if not data:
            return None
        try:
            last = float(data.get("close") or 0)
        except (TypeError, ValueError):
            return None
        if last <= 0:
            return None

        return MarketQuote(
            last=last,
            timestamp=_parse_dt(data.get("datetime")) or datetime.now(),
            open=_f(data.get("open")),
            high=_f(data.get("high")),
            low=_f(data.get("low")),
            prev_close=_f(data.get("previous_close")),
            volume=_i(data.get("volume")),
            source="twelvedata",
            is_real_time=False,
        )

    def get_history(
        self,
        symbol: str,
        start: date,
        end: date,
        interval: str = "1d",
    ) -> list[OHLCV]:
        td_interval = {"1d": "1day", "1h": "1h", "5m": "5min", "1m": "1min"}.get(
            interval,
        )
        if td_interval is None:
            return []
        data = self._fetch(
            "/time_series",
            {
                "symbol": symbol.upper(),
                "exchange": self._exchange,
                "interval": td_interval,
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "outputsize": "5000",
            },
        )
        out: list[OHLCV] = []
        for row in data.get("values", []) or []:
            try:
                ts = _parse_dt(row.get("datetime"))
                if ts is None:
                    continue
                out.append(
                    OHLCV(
                        timestamp=ts,
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=int(float(row.get("volume") or 0)),
                    )
                )
            except (KeyError, ValueError):
                continue
        return sorted(out, key=lambda b: b.timestamp)


def _parse_dt(s: object) -> datetime | None:
    if not isinstance(s, str):
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


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
