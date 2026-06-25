"""Dhan provider — free real broker feed with depth.

Dhan's market-data API is free for account holders and returns live
last price, OHLC, and 5-level market depth (bid/ask + sizes) — the same
fidelity the order-book simulator wants, at no monthly cost.

The security-id wrinkle
-----------------------
Unlike Kite (which keys quotes by ``"NSE:RELIANCE"``), Dhan keys
everything by a numeric ``security_id`` taken from its instrument master
CSV. So this provider needs a way to turn a trading symbol into a
security id. Supply one of:

  - ``security_id_map={"RELIANCE": "2885", ...}`` at construction, or
  - a ``resolve`` callable ``(symbol, segment) -> security_id | None``.

When a symbol can't be resolved we return ``None`` (treated as "unknown
symbol" by the fallback chain) rather than raising.

Auth
----
Reads ``DHAN_CLIENT_ID`` and ``DHAN_ACCESS_TOKEN`` from the environment
by default. The access token is long-lived (unlike Kite's daily token).

Install: ``pip install papertrade-india[dhan]`` (pulls ``dhanhq``).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import datetime

from .base import (
    MarketDataProvider,
    MarketQuote,
    ProviderCapability,
    ProviderError,
    ProviderInfo,
)

logger = logging.getLogger(__name__)

# Dhan exchange-segment identifiers.
_SEGMENT = {"NSE": "NSE_EQ", "BSE": "BSE_EQ"}


class DhanProvider(MarketDataProvider):
    """Dhan HQ market-data provider.

    Parameters
    ----------
    client_id, access_token:
        Dhan credentials. Fall back to ``DHAN_CLIENT_ID`` /
        ``DHAN_ACCESS_TOKEN``.
    exchange:
        ``"NSE"`` (default) or ``"BSE"``.
    security_id_map:
        Optional ``{symbol: security_id}`` lookup. Required (or
        ``resolve``) because Dhan keys quotes by numeric security id.
    resolve:
        Optional callable ``(symbol, segment) -> security_id | None``
        for dynamic resolution against your own instrument master.
    dhan:
        Pre-built ``dhanhq`` client (mostly for tests). When supplied,
        ``client_id`` / ``access_token`` are ignored.
    """

    def __init__(
        self,
        client_id: str | None = None,
        access_token: str | None = None,
        exchange: str = "NSE",
        security_id_map: dict[str, str] | None = None,
        resolve: Callable[[str, str], str | None] | None = None,
        dhan: object | None = None,
    ) -> None:
        self._client_id = client_id or os.environ.get("DHAN_CLIENT_ID")
        self._access_token = access_token or os.environ.get("DHAN_ACCESS_TOKEN")
        self._exchange = exchange.upper()
        self._segment = _SEGMENT.get(self._exchange, "NSE_EQ")
        self._security_id_map = {
            k.upper(): str(v) for k, v in (security_id_map or {}).items()
        }
        self._resolve = resolve
        self._dhan = dhan

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
            name="dhan",
            description="Dhan HQ live market data (bid/ask + 5-level depth).",
            capabilities=caps,
            requires_api_key=True,
            homepage="https://dhanhq.co/",
            notes=(
                "Set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN. Free for account "
                "holders. Needs a symbol->security_id map (Dhan keys by id)."
            ),
        )

    # ── Client + resolution ───────────────────────────────────────────

    def _client(self) -> object:
        if self._dhan is not None:
            return self._dhan
        if not self._client_id or not self._access_token:
            raise ProviderError(
                "Dhan credentials missing. Set DHAN_CLIENT_ID and "
                "DHAN_ACCESS_TOKEN (or pass client_id=/access_token=)."
            )
        try:
            from dhanhq import dhanhq  # noqa: PLC0415
        except ImportError as e:
            raise ProviderError(
                "dhanhq is not installed. "
                "Install with: pip install papertrade-india[dhan]"
            ) from e
        client = dhanhq(self._client_id, self._access_token)
        self._dhan = client
        return client

    def _security_id(self, symbol: str) -> str | None:
        sym = symbol.upper()
        if sym in self._security_id_map:
            return self._security_id_map[sym]
        if self._resolve is not None:
            try:
                return self._resolve(sym, self._segment)
            except Exception as e:  # noqa: BLE001
                logger.debug("dhan resolve failed for %s: %s", sym, e)
                return None
        return None

    # ── Quote ─────────────────────────────────────────────────────────

    def get_quote(self, symbol: str) -> MarketQuote | None:
        security_id = self._security_id(symbol)
        if security_id is None:
            logger.debug(
                "dhan: no security_id for %s; supply security_id_map or resolve",
                symbol,
            )
            return None

        client = self._client()
        try:
            resp = client.quote_data(  # type: ignore[attr-defined]
                securities={self._segment: [int(security_id)]},
            )
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"dhan quote failed for {symbol}: {e}") from e

        data = _extract(resp, self._segment, str(security_id))
        if not data:
            return None

        last = _f(data.get("last_price"))
        if last is None:
            return None

        bid, bid_size = _best(data, "buy")
        ask, ask_size = _best(data, "sell")
        ohlc = data.get("ohlc") or {}

        ts = _parse_ts(data.get("last_trade_time") or data.get("LTT"))

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
            source="dhan",
            is_real_time=True,
        )


# ── Parse helpers ─────────────────────────────────────────────────────


def _extract(resp: object, segment: str, security_id: str) -> dict | None:
    """Dig the per-instrument block out of Dhan's nested response.

    Dhan wraps the payload as ``{"status": "success", "data": {"data":
    {"<segment>": {"<security_id>": {...}}}}}``. We tolerate a couple of
    shape variations (with / without the outer ``data`` envelope).
    """
    if not isinstance(resp, dict):
        return None
    payload = resp.get("data", resp)
    if isinstance(payload, dict) and "data" in payload:
        payload = payload["data"]
    if not isinstance(payload, dict):
        return None
    seg = payload.get(segment)
    if not isinstance(seg, dict):
        return None
    block = seg.get(security_id) or seg.get(str(security_id))
    return block if isinstance(block, dict) else None


def _best(data: dict, side: str) -> tuple[float | None, int | None]:
    depth = data.get("depth") or {}
    levels = depth.get(side) or []
    if not levels:
        return (None, None)
    top = levels[0] or {}
    return (_f(top.get("price")), _i(top.get("quantity")))


def _parse_ts(value: object) -> datetime | None:
    """Parse Dhan's last-trade time (epoch seconds/ms or ISO string)."""
    if isinstance(value, (int, float)) and value > 0:
        # Dhan epochs are seconds; ms values are ~1e12.
        secs = value / 1000.0 if value > 1e11 else float(value)
        try:
            return datetime.fromtimestamp(secs)
        except (OverflowError, OSError, ValueError):
            return None
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


__all__ = ["DhanProvider"]
