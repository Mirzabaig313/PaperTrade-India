"""NSE Bhavcopy provider — official EOD daily-bar source.

The bhavcopy is NSE's official end-of-day CSV with every traded symbol's
OHLCV. We fetch it once per trading date and cache the parse so repeat
quotes for that date are O(1).

URL pattern (current NSE archive layout, 2024+):
    https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_<DDMMYYYY>.csv

NSE rate-limits and occasionally requires a cookie; this provider keeps
its surface narrow (single-day fetch, in-memory cache) and lets the
circuit breaker handle hostile responses.

Real-time? No — the bhavcopy is published after the session closes.
``MarketQuote.is_real_time`` is ``False`` and ``is_stale`` semantics in
the broker treat it as cache-grade.
"""

from __future__ import annotations

import csv
import io
import logging
import urllib.request
from datetime import date, datetime, timedelta
from threading import RLock
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

_BHAVCOPY_URL = (
    "https://nsearchives.nseindia.com/products/content/"
    "sec_bhavdata_full_{ddmmyyyy}.csv"
)

# NSE's archive blocks default urllib UA; mimic a browser to get past
# the gate without dragging in ``requests`` as a hard dep.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/csv,application/csv,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}


class NSEBhavcopyProvider(MarketDataProvider):
    """Read EOD prices from the official NSE bhavcopy.

    Parameters
    ----------
    timeout:
        HTTP timeout in seconds. Default 10.
    max_lookback_days:
        How many trading days back to walk when today's bhavcopy isn't
        published yet (weekend, holiday, before EOD). Default 7.
    """

    def __init__(
        self,
        timeout: float = 10.0,
        max_lookback_days: int = 7,
    ) -> None:
        self._timeout = float(timeout)
        self._max_lookback = int(max_lookback_days)
        self._cache: dict[date, dict[str, dict[str, float | int]]] = {}
        self._lock = RLock()

    @property
    def info(self) -> ProviderInfo:
        return ProviderInfo(
            name="nse-bhavcopy",
            description="Official NSE EOD bhavcopy CSV.",
            capabilities=(
                ProviderCapability.LAST_PRICE
                | ProviderCapability.OHLCV_DAILY
                | ProviderCapability.SUPPORTS_NSE
                | ProviderCapability.DELAYED
            ),
            requires_api_key=False,
            homepage="https://www.nseindia.com/all-reports",
            notes="Published after market close. Authoritative EOD data.",
        )

    # ── Quote/history ─────────────────────────────────────────────────

    def get_quote(self, symbol: str) -> MarketQuote | None:
        for days_back in range(self._max_lookback):
            d = date.today() - timedelta(days=days_back)
            try:
                table = self._load(d)
            except ProviderError:
                # The dated copy may not exist (weekend/holiday) — try earlier.
                continue
            row = table.get(symbol.upper())
            if row is None:
                continue
            return MarketQuote(
                last=float(row["close"]),
                timestamp=datetime.combine(d, datetime.min.time()),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                prev_close=float(row.get("prev_close") or 0) or None,
                volume=int(row.get("volume") or 0) or None,
                source="nse-bhavcopy",
                is_real_time=False,
            )
        return None

    def get_history(
        self,
        symbol: str,
        start: date,
        end: date,
        interval: str = "1d",
    ) -> list[OHLCV]:
        if interval != "1d":
            return []
        out: list[OHLCV] = []
        d = start
        while d <= end:
            try:
                table = self._load(d)
            except ProviderError:
                d += timedelta(days=1)
                continue
            row = table.get(symbol.upper())
            if row is not None:
                out.append(
                    OHLCV(
                        timestamp=datetime.combine(d, datetime.min.time()),
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=int(row.get("volume") or 0),
                    )
                )
            d += timedelta(days=1)
        return out

    # ── Internals ─────────────────────────────────────────────────────

    def _load(self, d: date) -> dict[str, dict[str, float | int]]:
        with self._lock:
            cached = self._cache.get(d)
            if cached is not None:
                return cached
        url = _BHAVCOPY_URL.format(ddmmyyyy=d.strftime("%d%m%Y"))
        body = self._fetch(url)
        table = self._parse(body)
        with self._lock:
            self._cache[d] = table
        return table

    def _fetch(self, url: str) -> str:
        req = urllib.request.Request(url, headers=_HEADERS)  # noqa: S310 — fixed host
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # noqa: S310
                return resp.read().decode("utf-8", errors="replace")
        except HTTPError as e:
            raise ProviderError(f"NSE bhavcopy HTTP {e.code}: {url}") from e
        except (URLError, TimeoutError) as e:
            raise ProviderError(f"NSE bhavcopy fetch failed: {e}") from e

    @staticmethod
    def _parse(body: str) -> dict[str, dict[str, float | int]]:
        """Parse the NSE bhavcopy CSV into a ``{symbol: row}`` map.

        The columns are space-padded (``" SYMBOL"``) and named in upper
        case in the canonical layout. We only keep the EQ series rows.
        """
        out: dict[str, dict[str, float | int]] = {}
        reader = csv.DictReader(io.StringIO(body))
        for raw in reader:
            row = {k.strip(): (v.strip() if isinstance(v, str) else v) for k, v in raw.items()}
            series = (row.get("SERIES") or "").upper()
            if series and series != "EQ":
                continue
            symbol = (row.get("SYMBOL") or "").upper()
            if not symbol:
                continue
            try:
                close = float(row.get("CLOSE_PRICE") or row.get("CLOSE") or 0.0)
                if close <= 0:
                    continue
                out[symbol] = {
                    "open": float(row.get("OPEN_PRICE") or row.get("OPEN") or 0.0),
                    "high": float(row.get("HIGH_PRICE") or row.get("HIGH") or 0.0),
                    "low": float(row.get("LOW_PRICE") or row.get("LOW") or 0.0),
                    "close": close,
                    "prev_close": float(row.get("PREV_CLOSE") or 0.0),
                    "volume": int(float(row.get("TTL_TRD_QNTY") or row.get("VOLUME") or 0)),
                }
            except (TypeError, ValueError):
                continue
        return out
