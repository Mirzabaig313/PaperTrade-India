"""Yahoo Finance provider.

Path of least resistance: no API key, no signup. Yahoo is delayed
~15 minutes for Indian symbols and the response shape can drift
without notice — keep it as one provider in a chain, not the only one.

Capabilities
------------
- LAST_PRICE, QUOTE (bid/ask when ``fast_info`` exposes them), DELAYED.
- OHLCV_DAILY (via ``ticker.history``).
- NSE (.NS) by default; pass ``exchange_suffix="BO"`` for BSE.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import date, datetime

from .base import (
    OHLCV,
    MarketDataProvider,
    MarketQuote,
    ProviderCapability,
    ProviderError,
    ProviderInfo,
)

logger = logging.getLogger(__name__)


class YFinanceProvider(MarketDataProvider):
    """Yahoo Finance via the ``yfinance`` package.

    Parameters
    ----------
    exchange_suffix:
        ``"NS"`` for NSE (default), ``"BO"`` for BSE.
    """

    def __init__(self, exchange_suffix: str = "NS") -> None:
        self.suffix = exchange_suffix.lstrip(".")

    @property
    def info(self) -> ProviderInfo:
        caps = (
            ProviderCapability.LAST_PRICE
            | ProviderCapability.QUOTE
            | ProviderCapability.OHLCV_DAILY
            | ProviderCapability.DELAYED
        )
        if self.suffix.upper() == "NS":
            caps |= ProviderCapability.SUPPORTS_NSE
        elif self.suffix.upper() == "BO":
            caps |= ProviderCapability.SUPPORTS_BSE
        return ProviderInfo(
            name="yfinance",
            description="Yahoo Finance via the yfinance Python package.",
            capabilities=caps,
            requires_api_key=False,
            homepage="https://github.com/ranaroussi/yfinance",
            notes="~15 minute delay for Indian symbols.",
        )

    def get_quote(self, symbol: str) -> MarketQuote | None:
        try:
            import yfinance as yf  # noqa: WPS433 — lazy import is intentional
        except ImportError as e:
            raise ProviderError("yfinance is not installed") from e

        ticker_sym = f"{symbol}.{self.suffix}"
        try:
            ticker = yf.Ticker(ticker_sym)

            # ``fast_info`` is volatile across versions; treat any
            # KeyError/AttributeError as "fast path didn't work".
            last: float | None = None
            bid: float | None = None
            ask: float | None = None
            volume: int | None = None
            prev_close: float | None = None
            day_high: float | None = None
            day_low: float | None = None
            day_open: float | None = None
            try:
                fi = ticker.fast_info
                last = (
                    _f(fi, "lastPrice")
                    or _f(fi, "last_price")
                    or _f(fi, "regularMarketPrice")
                )
                bid = _f(fi, "bid")
                ask = _f(fi, "ask")
                volume = _i(fi, "lastVolume") or _i(fi, "regularMarketVolume")
                prev_close = (
                    _f(fi, "previousClose") or _f(fi, "regularMarketPreviousClose")
                )
                day_high = _f(fi, "dayHigh") or _f(fi, "regularMarketDayHigh")
                day_low = _f(fi, "dayLow") or _f(fi, "regularMarketDayLow")
                day_open = _f(fi, "open") or _f(fi, "regularMarketOpen")
            except Exception:  # noqa: BLE001
                pass

            if last is None:
                # Fall back to the daily history slot.
                hist = ticker.history(period="1d")
                if hist is not None and not hist.empty:
                    last = float(hist["Close"].iloc[-1])
                    if "Volume" in hist.columns:
                        with contextlib.suppress(Exception):
                            volume = int(hist["Volume"].iloc[-1])

            if last is None:
                return None  # genuinely unknown symbol

            return MarketQuote(
                last=float(last),
                timestamp=datetime.now(),
                bid=bid,
                ask=ask,
                open=day_open,
                high=day_high,
                low=day_low,
                prev_close=prev_close,
                volume=volume,
                source="yfinance",
                is_real_time=False,  # Yahoo is delayed
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("yfinance failed for %s: %s", symbol, e)
            raise ProviderError(f"yfinance failed for {symbol}: {e}") from e

    def get_history(
        self,
        symbol: str,
        start: date,
        end: date,
        interval: str = "1d",
    ) -> list[OHLCV]:
        try:
            import yfinance as yf  # noqa: WPS433
        except ImportError as e:
            raise ProviderError("yfinance is not installed") from e

        try:
            ticker = yf.Ticker(f"{symbol}.{self.suffix}")
            hist = ticker.history(start=str(start), end=str(end), interval=interval)
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"yfinance history failed: {e}") from e
        if hist is None or hist.empty:
            return []

        out: list[OHLCV] = []
        for ts, row in hist.iterrows():
            try:
                out.append(
                    OHLCV(
                        timestamp=ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts,
                        open=float(row["Open"]),
                        high=float(row["High"]),
                        low=float(row["Low"]),
                        close=float(row["Close"]),
                        volume=int(row.get("Volume", 0) or 0),
                    )
                )
            except Exception:  # noqa: BLE001 — skip malformed rows
                continue
        return out


def _f(obj: object, key: str) -> float | None:
    """Best-effort float extractor that survives missing keys / Nones."""
    try:
        v = obj.get(key) if hasattr(obj, "get") else getattr(obj, key, None)
        if v is None:
            return None
        v = float(v)
        if v != v or v == float("inf") or v == float("-inf"):  # NaN guard
            return None
        return v if v > 0 else None
    except Exception:  # noqa: BLE001
        return None


def _i(obj: object, key: str) -> int | None:
    f = _f(obj, key)
    return int(f) if f is not None else None
