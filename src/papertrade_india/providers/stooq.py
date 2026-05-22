"""Stooq.com EOD provider.

Stooq publishes free end-of-day CSVs over plain HTTP — no API key.
Useful as a reference / backtest source and as another fallback in the
chain. End-of-day only, so :attr:`MarketQuote.is_real_time` is always
``False``.

Symbol convention: NSE → ``<symbol>.in`` (lowercase). E.g. ``RELIANCE``
→ ``reliance.in``. BSE coverage is partial; we attempt the same suffix.
"""

from __future__ import annotations

import csv
import io
import logging
import urllib.parse
import urllib.request
from datetime import date, datetime
from urllib.error import URLError

from .base import (
    OHLCV,
    MarketDataProvider,
    MarketQuote,
    ProviderCapability,
    ProviderError,
    ProviderInfo,
)

logger = logging.getLogger(__name__)

_QUOTE_URL = "https://stooq.com/q/l/"


class StooqProvider(MarketDataProvider):
    """End-of-day quotes from stooq.com.

    Parameters
    ----------
    suffix:
        Symbol suffix appended to the query. Default ``".in"`` for NSE.
    timeout:
        HTTP timeout in seconds.
    """

    def __init__(self, suffix: str = ".in", timeout: float = 5.0) -> None:
        self._suffix = suffix
        self._timeout = float(timeout)

    @property
    def info(self) -> ProviderInfo:
        return ProviderInfo(
            name="stooq",
            description="End-of-day OHLCV CSVs from stooq.com (no API key).",
            capabilities=(
                ProviderCapability.LAST_PRICE
                | ProviderCapability.OHLCV_DAILY
                | ProviderCapability.SUPPORTS_NSE
                | ProviderCapability.DELAYED
            ),
            requires_api_key=False,
            homepage="https://stooq.com",
            notes="Free EOD data — no real-time prices.",
        )

    def _ticker(self, symbol: str) -> str:
        return f"{symbol.lower()}{self._suffix}"

    def get_quote(self, symbol: str) -> MarketQuote | None:
        params = urllib.parse.urlencode({"s": self._ticker(symbol), "f": "sd2t2ohlcv", "h": "", "e": "csv"})
        url = f"{_QUOTE_URL}?{params}"
        try:
            with urllib.request.urlopen(url, timeout=self._timeout) as resp:  # noqa: S310 — fixed host
                body = resp.read().decode("utf-8", errors="replace")
        except (URLError, TimeoutError) as e:
            raise ProviderError(f"stooq fetch failed: {e}") from e
        except Exception as e:  # noqa: BLE001 — defensive
            raise ProviderError(f"stooq fetch failed: {e}") from e

        reader = csv.DictReader(io.StringIO(body))
        rows = list(reader)
        if not rows:
            return None
        row = rows[0]
        # Stooq returns "N/D" when the symbol is unknown.
        last_raw = row.get("Close") or row.get("close")
        if not last_raw or last_raw.upper() == "N/D":
            return None
        try:
            last = float(last_raw)
            day_open = _maybe_float(row.get("Open") or row.get("open"))
            day_high = _maybe_float(row.get("High") or row.get("high"))
            day_low = _maybe_float(row.get("Low") or row.get("low"))
            volume_raw = row.get("Volume") or row.get("volume")
            volume = int(float(volume_raw)) if volume_raw and volume_raw.upper() != "N/D" else None
        except ValueError as e:
            raise ProviderError(f"stooq malformed row: {row}") from e

        ts = _parse_date_time(row.get("Date"), row.get("Time"))
        return MarketQuote(
            last=last,
            timestamp=ts,
            open=day_open,
            high=day_high,
            low=day_low,
            volume=volume,
            source="stooq",
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
        params = urllib.parse.urlencode(
            {
                "s": self._ticker(symbol),
                "i": "d",
                "d1": start.strftime("%Y%m%d"),
                "d2": end.strftime("%Y%m%d"),
            },
        )
        url = f"https://stooq.com/q/d/l/?{params}"
        try:
            with urllib.request.urlopen(url, timeout=self._timeout) as resp:  # noqa: S310
                body = resp.read().decode("utf-8", errors="replace")
        except (URLError, TimeoutError) as e:
            raise ProviderError(f"stooq history fetch failed: {e}") from e

        reader = csv.DictReader(io.StringIO(body))
        out: list[OHLCV] = []
        for row in reader:
            try:
                out.append(
                    OHLCV(
                        timestamp=datetime.strptime(row["Date"], "%Y-%m-%d"),
                        open=float(row["Open"]),
                        high=float(row["High"]),
                        low=float(row["Low"]),
                        close=float(row["Close"]),
                        volume=int(float(row.get("Volume") or 0)),
                    )
                )
            except (KeyError, ValueError):
                continue
        return out


def _maybe_float(s: str | None) -> float | None:
    if not s or s.upper() == "N/D":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_date_time(d: str | None, t: str | None) -> datetime:
    """Parse stooq's ``Date`` + ``Time`` columns, falling back to now."""
    if d:
        try:
            if t:
                return datetime.strptime(f"{d} {t}", "%Y-%m-%d %H:%M:%S")
            return datetime.strptime(d, "%Y-%m-%d")
        except ValueError:
            pass
    return datetime.now()
