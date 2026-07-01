"""Upstox provider — free real broker feed with depth (REST).

Upstox's market-quote API is free for account holders and returns live
last price, OHLC, volume, and full market depth (bid/ask + sizes). This
provider talks to the v2 REST endpoint directly via ``urllib`` (no SDK
dependency), so it's stubbable in tests the same way as the other REST
providers.

The instrument-key wrinkle
--------------------------
Upstox keys quotes by an ``instrument_key`` (e.g.
``"NSE_EQ|INE002A01018"`` for RELIANCE), not by trading symbol. Supply
one of:

  - ``instrument_key_map={"RELIANCE": "NSE_EQ|INE002A01018", ...}``, or
  - a ``resolve`` callable ``(symbol, segment) -> instrument_key | None``.

A symbol that can't be resolved yields ``None`` (treated as "unknown").

Auth
----
Reads ``UPSTOX_ACCESS_TOKEN`` from the environment by default. Tokens
are issued by the Upstox login flow and rotate daily.

No extra install needed — uses the standard library.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request
from collections.abc import Callable
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

_BASE = "https://api.upstox.com/v2"
_SEGMENT = {"NSE": "NSE_EQ", "BSE": "BSE_EQ"}


class UpstoxProvider(MarketDataProvider):
    """Upstox v2 market-quote provider (REST).

    Parameters
    ----------
    access_token:
        Upstox bearer token. Falls back to ``UPSTOX_ACCESS_TOKEN``.
    exchange:
        ``"NSE"`` (default) or ``"BSE"``.
    instrument_key_map:
        Optional ``{symbol: instrument_key}`` lookup. Required (or
        ``resolve``) because Upstox keys quotes by instrument key.
    resolve:
        Optional callable ``(symbol, segment) -> instrument_key | None``
        for dynamic resolution against your own instrument master.
    timeout:
        Per-request timeout in seconds.
    """

    def __init__(
        self,
        access_token: str | None = None,
        exchange: str = "NSE",
        instrument_key_map: dict[str, str] | None = None,
        resolve: Callable[[str, str], str | None] | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._access_token = access_token or os.environ.get("UPSTOX_ACCESS_TOKEN")
        self._exchange = exchange.upper()
        self._segment = _SEGMENT.get(self._exchange, "NSE_EQ")
        self._instrument_key_map = {
            k.upper(): str(v) for k, v in (instrument_key_map or {}).items()
        }
        self._resolve = resolve
        self._timeout = float(timeout)

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
            name="upstox",
            description="Upstox v2 live market quotes (bid/ask + depth).",
            capabilities=caps,
            requires_api_key=True,
            homepage="https://upstox.com/developer/api-documentation/",
            notes=(
                "Set UPSTOX_ACCESS_TOKEN (rotates daily). Free for account "
                "holders. Needs a symbol->instrument_key map."
            ),
        )

    # ── Resolution ────────────────────────────────────────────────────

    def _instrument_key(self, symbol: str) -> str | None:
        sym = symbol.upper()
        if sym in self._instrument_key_map:
            return self._instrument_key_map[sym]
        if self._resolve is not None:
            try:
                return self._resolve(sym, self._segment)
            except ProviderError:
                # A resolver outage (e.g. instrument-master download
                # failed) is a provider failure, not "unknown symbol" —
                # let it propagate so the feed logs it and falls back.
                raise
            except Exception as e:  # noqa: BLE001
                logger.debug("upstox resolve failed for %s: %s", sym, e)
                return None
        return None

    # ── Quote ─────────────────────────────────────────────────────────

    def get_quote(self, symbol: str) -> MarketQuote | None:
        if not self._access_token:
            raise ProviderError(
                "Upstox access token missing. Set UPSTOX_ACCESS_TOKEN "
                "or pass access_token=..."
            )
        instrument_key = self._instrument_key(symbol)
        if instrument_key is None:
            logger.debug(
                "upstox: no instrument_key for %s; supply instrument_key_map",
                symbol,
            )
            return None

        params = urllib.parse.urlencode({"instrument_key": instrument_key})
        url = f"{_BASE}/market-quote/quotes?{params}"
        req = urllib.request.Request(  # noqa: S310
            url,
            headers={
                "Authorization": f"Bearer {self._access_token}",
                "Accept": "application/json",
                # Upstox sits behind Cloudflare, which bans the default
                # ``Python-urllib/x.y`` UA (Error 1010) before auth is
                # even evaluated. A descriptive client UA passes.
                "User-Agent": "papertrade-india/0.1 (+https://github.com/your-org/papertrade-india)",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # noqa: S310
                # Cap the read — a single quote response is a few KB.
                body = resp.read(4 * 1024 * 1024).decode("utf-8", errors="replace")
        except HTTPError as e:
            if e.code in (401, 403):
                raise ProviderError(f"upstox auth failed (HTTP {e.code})") from e
            raise ProviderError(f"upstox HTTP {e.code}") from e
        except (URLError, TimeoutError) as e:
            raise ProviderError(f"upstox fetch failed: {e}") from e

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as e:
            raise ProviderError("upstox returned non-JSON response") from e

        data = _extract(payload, instrument_key, symbol)
        if not data:
            return None

        last = _f(data.get("last_price"))
        if last is None:
            return None

        bid, bid_size = _best(data, "buy")
        ask, ask_size = _best(data, "sell")
        ohlc = data.get("ohlc") or {}

        ts = _parse_ts(data.get("last_trade_time") or data.get("ltt"))

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
            source="upstox",
            is_real_time=True,
        )


# ── Parse helpers ─────────────────────────────────────────────────────


def _extract(payload: object, instrument_key: str, symbol: str) -> dict | None:
    """Pull the per-instrument block out of the Upstox response.

    Upstox responds with ``{"status": "success", "data": {"<KEY>":
    {...}}}`` where ``<KEY>`` is usually ``"<SEGMENT>:<SYMBOL>"`` (e.g.
    ``"NSE_EQ:RELIANCE"``) rather than the raw instrument key. We accept
    any single-entry data block, then fall back to matching by the
    instrument key or symbol suffix.
    """
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, dict) or not data:
        return None
    # Exact instrument-key match first.
    if instrument_key in data and isinstance(data[instrument_key], dict):
        return data[instrument_key]
    # Match by trailing symbol (``NSE_EQ:RELIANCE``).
    sym = symbol.upper()
    for key, block in data.items():
        if isinstance(block, dict) and key.upper().endswith(f":{sym}"):
            return block
    # Single-entry response: accept the only block, but only when it
    # doesn't positively contradict the requested symbol (guards against
    # returning a mismatched instrument).
    if len(data) == 1:
        only_key, only = next(iter(data.items()))
        if not isinstance(only, dict):
            return None
        key_up = only_key.upper()
        if ":" in key_up and not key_up.endswith(f":{sym}"):
            return None  # names a different symbol
        return only
    return None


def _best(data: dict, side: str) -> tuple[float | None, int | None]:
    depth = data.get("depth") or {}
    levels = depth.get(side) or []
    if not levels:
        return (None, None)
    top = levels[0] or {}
    return (_f(top.get("price")), _i(top.get("quantity")))


def _parse_ts(value: object) -> datetime | None:
    """Parse Upstox last-trade time (epoch ms/seconds or ISO string)."""
    if isinstance(value, (int, float)) and value > 0:
        secs = value / 1000.0 if value > 1e11 else float(value)
        try:
            return datetime.fromtimestamp(secs)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        # Numeric epoch delivered as a string.
        if value.isdigit():
            return _parse_ts(int(value))
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


__all__ = ["UpstoxProvider"]
