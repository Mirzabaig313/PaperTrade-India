"""Zerodha Kite Connect provider — real broker feed with full depth.

This is the highest-fidelity provider for Indian equities short of a
paid tick feed: it returns the live last-traded price, the best bid/ask
with sizes, and the top-of-book depth that the order-book simulator
needs to walk realistic fills.

Why this matters
----------------
yfinance and the other public providers deliver a delayed last-trade
with no bid/ask. With no real spread, :class:`OrderBookSimulator`
falls back to a synthetic book. A Kite quote carries the genuine
bid/ask + depth, so fills land where a real Zerodha order would land.

Auth
----
Kite uses an API key + a daily-rotated access token (obtained via the
login flow). Both are read from the environment by default:

  - ``KITE_API_KEY``
  - ``KITE_ACCESS_TOKEN``

The access token expires every trading day; refreshing it is the
caller's responsibility (run your login flow, then set the env var or
pass ``access_token=...``). When the token is stale, Kite returns a
``TokenException`` which we surface as :class:`ProviderError` so the
fallback chain can move on.

Pricing: Kite Connect is ₹500/month per API key for live + historical
data (free "Personal" tier covers order placement but not market data).

Install: ``pip install papertrade-india[kite]`` (pulls ``kiteconnect``).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime

from .base import (
    MarketDataProvider,
    MarketQuote,
    ProviderCapability,
    ProviderError,
    ProviderInfo,
)

logger = logging.getLogger(__name__)


class KiteProvider(MarketDataProvider):
    """Zerodha Kite Connect market-data provider.

    Parameters
    ----------
    api_key:
        Kite Connect API key. Falls back to ``KITE_API_KEY``.
    access_token:
        Daily access token from the login flow. Falls back to
        ``KITE_ACCESS_TOKEN``.
    exchange:
        ``"NSE"`` (default) or ``"BSE"``. Kite quote keys are
        ``"<EXCHANGE>:<TRADINGSYMBOL>"`` e.g. ``"NSE:RELIANCE"``.
    kite:
        Pre-built ``KiteConnect`` client (mostly for tests / advanced
        callers who already manage the session). When supplied,
        ``api_key`` / ``access_token`` are ignored.
    """

    def __init__(
        self,
        api_key: str | None = None,
        access_token: str | None = None,
        exchange: str = "NSE",
        kite: object | None = None,
    ) -> None:
        self._api_key = api_key or os.environ.get("KITE_API_KEY")
        self._access_token = access_token or os.environ.get("KITE_ACCESS_TOKEN")
        self._exchange = exchange.upper()
        self._kite = kite  # injected client wins over lazy construction

    @property
    def info(self) -> ProviderInfo:
        caps = (
            ProviderCapability.LAST_PRICE
            | ProviderCapability.QUOTE
            | ProviderCapability.REAL_TIME
        )
        if self._exchange == "NSE":
            caps |= ProviderCapability.SUPPORTS_NSE
        elif self._exchange == "BSE":
            caps |= ProviderCapability.SUPPORTS_BSE
        return ProviderInfo(
            name="kite",
            description="Zerodha Kite Connect live market data (bid/ask + depth).",
            capabilities=caps,
            requires_api_key=True,
            homepage="https://kite.trade/",
            notes=(
                "Set KITE_API_KEY and KITE_ACCESS_TOKEN (token rotates daily). "
                "Live data plan: Rs.500/month per API key."
            ),
        )

    # ── Client construction ───────────────────────────────────────────

    def _client(self) -> object:
        """Return the Kite client, building it lazily on first use."""
        if self._kite is not None:
            return self._kite
        if not self._api_key or not self._access_token:
            raise ProviderError(
                "Kite credentials missing. Set KITE_API_KEY and "
                "KITE_ACCESS_TOKEN (or pass api_key=/access_token=)."
            )
        try:
            from kiteconnect import KiteConnect  # noqa: PLC0415
        except ImportError as e:
            raise ProviderError(
                "kiteconnect is not installed. "
                "Install with: pip install papertrade-india[kite]"
            ) from e
        client = KiteConnect(api_key=self._api_key)
        client.set_access_token(self._access_token)
        self._kite = client
        return client

    # ── Quote ─────────────────────────────────────────────────────────

    def get_quote(self, symbol: str) -> MarketQuote | None:
        client = self._client()
        instrument = f"{self._exchange}:{symbol.upper()}"
        try:
            resp = client.quote(instrument)  # type: ignore[attr-defined]
        except Exception as e:  # noqa: BLE001 — SDK raises various exc types
            # TokenException, NetworkException, etc. all become ProviderError
            # so the fallback chain can try the next provider.
            raise ProviderError(f"kite quote failed for {symbol}: {e}") from e

        data = resp.get(instrument) if isinstance(resp, dict) else None
        if not data:
            return None  # genuinely unknown symbol

        last = _f(data.get("last_price"))
        if last is None:
            return None

        bid, bid_size = _best(data, "buy")
        ask, ask_size = _best(data, "sell")
        ohlc = data.get("ohlc") or {}

        ts = _parse_ts(data.get("timestamp") or data.get("last_trade_time"))

        return MarketQuote(
            last=last,
            timestamp=ts or datetime.now(),
            bid=bid,
            ask=ask,
            bid_size=bid_size,
            ask_size=ask_size,
            open=_f(ohlc.get("open")),
            high=_f(ohlc.get("high")),
            low=_f(ohlc.get("low")),
            prev_close=_f(ohlc.get("close")),
            volume=_i(data.get("volume")),
            source="kite",
            is_real_time=True,
        )


# ── Parse helpers ─────────────────────────────────────────────────────


def _best(data: dict, side: str) -> tuple[float | None, int | None]:
    """Extract best (price, quantity) from the Kite depth block.

    ``data["depth"]["buy"|"sell"]`` is a list of ``{price, quantity,
    orders}`` ordered best-first. Returns ``(None, None)`` when depth is
    absent (e.g. a non-depth quote endpoint).
    """
    depth = data.get("depth") or {}
    levels = depth.get(side) or []
    if not levels:
        return (None, None)
    top = levels[0] or {}
    return (_f(top.get("price")), _i(top.get("quantity")))


def _parse_ts(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(value, fmt)
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
        v = int(float(x))
        return v if v >= 0 else None
    except (TypeError, ValueError):
        return None


__all__ = ["KiteProvider"]
