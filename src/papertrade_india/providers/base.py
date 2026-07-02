"""Provider interface — the single contract every data source implements.

Why an ABC and not just a Protocol
-----------------------------------
The legacy ``PriceProvider`` Protocol (``get_price(symbol) -> float | None``)
covers one method. Real providers expose more: full quotes (bid/ask/last/
volume), historical bars, capability flags (does this source give
intraday? bid/ask? options chains?), and provenance (which provider was
hit, when, was the response cached).

An explicit ABC gives us:
- A single place to document what providers must do.
- Capability flags so the composite layer can route requests to providers
  that can answer them (``capabilities & QUOTE`` etc.).
- A typed ``ProviderInfo`` so the CLI and dashboards can introspect
  registered providers without instantiating them.
- A consistent error type (``ProviderError``) so callers can distinguish
  "provider failed" from "symbol is genuinely unknown".

Backwards-compat: every concrete provider here also defines a thin
``get_price`` method that satisfies the legacy Protocol, so the existing
``PriceFeed`` → providers wiring keeps working.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from datetime import date, datetime
from enum import Flag, auto


class ProviderCapability(Flag):
    """What a provider can deliver. Combine with ``|``.

    The composite/registry layer uses these to pick the right provider
    for each query — e.g. asking a delayed-only provider for a real-time
    quote should fail fast or fall through.
    """

    NONE = 0
    LAST_PRICE = auto()       # last-traded price (the bare minimum)
    QUOTE = auto()            # bid/ask + last
    OHLCV_INTRADAY = auto()   # intraday bars (1m/5m/...)
    OHLCV_DAILY = auto()      # daily/EOD bars
    REAL_TIME = auto()        # live (no delay)
    DELAYED = auto()          # 15-min or end-of-day delayed
    SUPPORTS_NSE = auto()
    SUPPORTS_BSE = auto()


@dataclass(frozen=True)
class MarketQuote:
    """A rich price snapshot.

    Required: ``last`` and ``timestamp``. Everything else is optional
    so partial-fidelity providers can fill in only what they have.
    Consumers should check for ``None`` before using bid/ask/volume.

    Fields
    ------
    last:
        Last-traded price (always populated).
    timestamp:
        When the underlying provider produced this quote.
    bid, ask:
        Best bid / best ask. Sometimes one without the other (e.g. an
        EOD source has neither).
    bid_size, ask_size:
        Depth at the top of book.
    open, high, low, prev_close:
        Day's OHL + previous close (for percent-change displays).
    volume:
        Traded shares so far today.
    adjusted_close:
        Split- and dividend-adjusted close. Set by providers that can
        deliver an adjusted history (yfinance ``auto_adjust=True``,
        Alpha Vantage ``TIME_SERIES_DAILY_ADJUSTED``). ``None`` when
        the source only delivers raw closes. Backtesters should prefer
        ``adjusted_close`` when present so historical splits don't
        cause spurious P&L jumps.
    currency:
        ISO code, defaults to INR for the Indian market.
    source:
        Provider identifier (filled in by the provider itself).
    is_real_time:
        False when the source is delayed/cached. The broker's
        ``enforce_fresh_prices`` mode rejects fills on stale snapshots.
    """

    last: float
    timestamp: datetime
    bid: float | None = None
    ask: float | None = None
    bid_size: int | None = None
    ask_size: int | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    prev_close: float | None = None
    volume: int | None = None
    adjusted_close: float | None = None
    currency: str = "INR"
    source: str = "unknown"
    is_real_time: bool = False
    # Full L2 depth ladders when the provider supplies them (best-first,
    # each entry ``(price, size)``). ``None`` means depth-unknown — the
    # book simulator then synthesizes from bid/ask. Upstox returns 5
    # levels; ``bid``/``ask`` above mirror ``bids[0]``/``asks[0]``.
    bids: tuple[tuple[float, int], ...] | None = None
    asks: tuple[tuple[float, int], ...] | None = None

    @property
    def has_depth(self) -> bool:
        """True when real multi-level depth is available on both sides."""
        return bool(self.bids and self.asks)

    @property
    def mid(self) -> float | None:
        """Mid-price between bid and ask, or ``None`` when either is missing."""
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2.0

    @property
    def spread_bps(self) -> float | None:
        """Bid/ask spread expressed in basis points off mid, or ``None``."""
        m = self.mid
        if m is None or m <= 0 or self.bid is None or self.ask is None:
            return None
        return (self.ask - self.bid) / m * 10000.0


@dataclass(frozen=True)
class OHLCV:
    """A single OHLCV bar."""

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass(frozen=True)
class ProviderInfo:
    """Static metadata about a registered provider.

    Used by the registry, CLI introspection, and dashboards. Does not
    require instantiating the provider.
    """

    name: str
    description: str
    capabilities: ProviderCapability
    requires_api_key: bool = False
    requires_network: bool = True
    homepage: str | None = None
    notes: str = ""


class ProviderError(Exception):
    """Raised by providers when a fetch genuinely failed.

    Distinct from "no data for this symbol" — providers should return
    ``None`` for unknown symbols and raise this only when the call
    itself broke (network down, rate limit, parse error).
    """


class MarketDataProvider(abc.ABC):
    """Abstract base for every market-data provider.

    Implementations must override ``info`` and ``get_quote``. Other
    methods have safe defaults built on top of those two so a minimal
    provider can ship with just the basics.

    Implementations should:
      - Return ``None`` for unknown symbols (it's not an error).
      - Raise :class:`ProviderError` when the upstream call fails.
      - Set ``MarketQuote.source`` to a stable, lowercase identifier.
    """

    # ── Introspection ─────────────────────────────────────────────────

    @property
    @abc.abstractmethod
    def info(self) -> ProviderInfo:
        """Static metadata describing this provider."""

    @property
    def name(self) -> str:
        """Stable identifier (lowercased provider name)."""
        return self.info.name

    @property
    def capabilities(self) -> ProviderCapability:
        """Capability flags for routing decisions."""
        return self.info.capabilities

    def supports(self, capability: ProviderCapability) -> bool:
        """True when this provider can deliver ``capability``."""
        return bool(self.capabilities & capability)

    # ── Required hot path ─────────────────────────────────────────────

    @abc.abstractmethod
    def get_quote(self, symbol: str) -> MarketQuote | None:
        """Return a quote for ``symbol`` or ``None`` if unknown.

        Raises :class:`ProviderError` on infrastructure failures
        (network, parse, rate-limit). Returning ``None`` means the
        symbol is genuinely not tracked by this provider — callers
        should try the next one.
        """

    # ── Convenience defaults ──────────────────────────────────────────

    def get_price(self, symbol: str) -> float | None:
        """Legacy ``PriceProvider`` shim.

        Returns last-traded price or ``None``. Errors are swallowed
        because the legacy fallback chain expects ``None``-on-failure;
        :meth:`get_quote` is the correct entry point for new code.
        """
        try:
            quote = self.get_quote(symbol)
        except ProviderError:
            return None
        return quote.last if quote is not None else None

    def get_history(
        self,
        symbol: str,
        start: date,
        end: date,
        interval: str = "1d",
    ) -> list[OHLCV]:
        """Return historical bars between ``start`` and ``end`` inclusive.

        Default implementation returns an empty list — providers that
        only deliver spot quotes can leave this alone. Override when
        the source supports historical data.
        """
        return []

    # ── Fluent ergonomics ─────────────────────────────────────────────

    def __repr__(self) -> str:  # pragma: no cover — trivial
        return f"<{type(self).__name__} name={self.name}>"


# Sentinel used by registry/discovery to stand in for an optional
# provider whose deps aren't installed. Imported by registry.py.
_UNAVAILABLE: object = object()
