"""jugaad-data provider — direct NSE scraping.

Community-maintained scraper of the NSE site. Fragile by design (any
markup change can break it), but useful as a fallback when Yahoo
rate-limits. NSE-only — there's no BSE equivalent in jugaad.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from .base import (
    OHLCV,
    MarketDataProvider,
    MarketQuote,
    ProviderCapability,
    ProviderError,
    ProviderInfo,
)

logger = logging.getLogger(__name__)


class JugaadDataProvider(MarketDataProvider):
    """NSE direct via the ``jugaad-data`` package.

    Install with the optional extra: ``pip install 'papertrade-india[jugaad]'``.
    """

    @property
    def info(self) -> ProviderInfo:
        return ProviderInfo(
            name="jugaad-data",
            description="Community NSE scraper via jugaad-data.",
            capabilities=(
                ProviderCapability.LAST_PRICE
                | ProviderCapability.QUOTE
                | ProviderCapability.OHLCV_DAILY
                | ProviderCapability.SUPPORTS_NSE
                | ProviderCapability.DELAYED
            ),
            requires_api_key=False,
            homepage="https://github.com/jugaad-py/jugaad-data",
            notes="NSE only. Scraper — treat failures as expected.",
        )

    def get_quote(self, symbol: str) -> MarketQuote | None:
        try:
            from jugaad_data.nse import NSELive  # type: ignore
        except ImportError as e:
            raise ProviderError(
                "jugaad-data is not installed. "
                "Install with: pip install 'papertrade-india[jugaad]'"
            ) from e

        try:
            n = NSELive()
            data = n.stock_quote(symbol)
        except Exception as e:  # noqa: BLE001 — scraper volatility
            logger.warning("jugaad-data failed for %s: %s", symbol, e)
            raise ProviderError(f"jugaad-data failed for {symbol}: {e}") from e

        try:
            price_info = data.get("priceInfo") or {}
            last = float(price_info["lastPrice"])
        except (KeyError, TypeError, ValueError):
            return None  # symbol unknown to NSE

        # ``iNavValue`` exists in some NSE responses but isn't a true bid;
        # we leave bid/ask out and let consumers detect missing fields.
        ohlc = price_info.get("intraDayHighLow") or {}
        day_high = _safe_get(ohlc, "max")
        day_low = _safe_get(ohlc, "min")
        prev_close = _safe_get(price_info, "previousClose")
        day_open = _safe_get(price_info, "open")

        return MarketQuote(
            last=last,
            timestamp=datetime.now(),
            bid=None,
            ask=None,
            open=day_open,
            high=day_high,
            low=day_low,
            prev_close=prev_close,
            source="jugaad-data",
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
        try:
            from jugaad_data.nse import stock_df  # type: ignore
        except ImportError as e:
            raise ProviderError("jugaad-data is not installed") from e

        try:
            df = stock_df(
                symbol=symbol,
                from_date=start,
                to_date=end + timedelta(days=0),
                series="EQ",
            )
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"jugaad-data history failed: {e}") from e

        out: list[OHLCV] = []
        for _, row in df.iterrows():
            try:
                ts = row["DATE"]
                ts = ts if isinstance(ts, datetime) else datetime.combine(
                    ts, datetime.min.time(),
                )
                out.append(
                    OHLCV(
                        timestamp=ts,
                        open=float(row["OPEN"]),
                        high=float(row["HIGH"]),
                        low=float(row["LOW"]),
                        close=float(row["CLOSE"]),
                        volume=int(row.get("VOLUME", 0) or 0),
                    )
                )
            except Exception:  # noqa: BLE001
                continue
        return out


def _safe_get(d: dict, key: str) -> float | None:
    try:
        v = d.get(key)
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None
