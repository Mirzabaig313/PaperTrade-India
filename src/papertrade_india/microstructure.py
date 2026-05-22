"""Market microstructure: tick size, lot size, price bands, and the
synthetic order book.

This module is the realism backbone the simulator was missing. It owns
four ideas, each independently configurable:

1. **Tick size** — limit / stop prices must be a multiple of the scrip's
   tick (₹0.05 is the NSE default for cash equity). Misaligned prices
   are rejected, just like the real exchange.
2. **Lot size** — orders must be a whole multiple of the lot. Cash
   equities are mostly lot=1 but some scrips trade in lots; F&O
   contracts use real lots (out of scope here, but the rule is enforced
   uniformly).
3. **Daily price band** — orders that would fill outside ``prev_close ×
   (1 ± band_pct)`` are rejected. NSE bands are 2/5/10/20% per scrip,
   set by the exchange; we default to 20% so users have to opt into the
   tighter bands.
4. **Synthetic order book** — a parametric L2 book derived from the
   provider's ``MarketQuote.bid``/``ask``/``volume``. Market orders walk
   the book (price impact = volume-weighted average), and limit orders
   get a queue position so they don't fill against ghost liquidity.

Why parametric (vs. a real LOB simulator)?
------------------------------------------
A real LOB needs every market order, cancel, and replace from every
participant in the day's tape — terabytes of data per day. The
parametric book here gets you 80% of the realism with 0% of the data
infrastructure: you tell the simulator "depth at the touch is roughly
0.5% of ADV" and it builds a realistic-shape book on the fly. Tune
``OrderBookConfig.depth_pct_of_adv`` and ``shape_decay`` to match the
liquidity profile of the names you trade.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from .exceptions import (
    LotSizeViolation,
    PriceBandViolation,
    TickSizeViolation,
)
from .models import OrderSide

logger = logging.getLogger(__name__)


# ── Tick / lot / band rules ──────────────────────────────────────────


@dataclass(frozen=True)
class MicrostructureConfig:
    """Toggles for tick / lot / band enforcement.

    All three default to ``True`` because rejecting them is what makes
    fills land where a real broker would land them. Set the field to
    ``False`` if you have a backtest that pre-dates a band change and
    you want to suppress the check.
    """

    enforce_tick_size: bool = True
    enforce_lot_size: bool = True
    enforce_price_band: bool = True

    # Default tick size / band when the symbol master has no override.
    # NSE cash equity is ₹0.05 across nearly all scrips; band defaults
    # to a permissive 20% so the legacy "anything fills" tests don't
    # break.
    default_tick_size: float = 0.05
    default_lot_size: int = 1
    default_band_pct: float = 0.20  # ±20%


def round_to_tick(price: float, tick: float) -> float:
    """Round ``price`` to the nearest multiple of ``tick``.

    Uses ``Decimal`` to avoid the binary-float traps that put 2940.05 at
    2940.0499999999999. Returns ``0.0`` when ``tick`` is ``0`` or the
    input is non-positive (callers should validate before calling).
    """
    if tick <= 0 or price <= 0:
        return 0.0
    p = Decimal(str(price))
    t = Decimal(str(tick))
    n = (p / t).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return float(n * t)


def is_aligned_to_tick(price: float, tick: float, atol: float = 1e-6) -> bool:
    """True when ``price`` is within ``atol`` paise of a tick boundary."""
    if tick <= 0:
        return True  # tick disabled
    rounded = round_to_tick(price, tick)
    return abs(price - rounded) <= atol


def validate_tick(price: float | None, tick: float, label: str) -> None:
    """Raise :class:`TickSizeViolation` if a price isn't tick-aligned."""
    if price is None or tick <= 0:
        return
    if not is_aligned_to_tick(price, tick):
        raise TickSizeViolation(
            f"{label} ₹{price:.6f} is not aligned to tick size ₹{tick:.4f}. "
            f"Nearest valid: ₹{round_to_tick(price, tick):.4f}",
        )


def validate_lot(qty: float, lot: int) -> None:
    """Raise :class:`LotSizeViolation` if ``qty`` isn't a multiple of ``lot``."""
    if lot <= 1:
        return
    # Reject fractional qty up front (we don't model MF units here).
    if abs(qty - round(qty)) > 1e-9:
        raise LotSizeViolation(
            f"qty {qty} is fractional; lot size {lot} requires whole shares.",
        )
    rounded_qty = int(round(qty))
    if rounded_qty % lot != 0:
        nearest_down = (rounded_qty // lot) * lot
        nearest_up = nearest_down + lot
        raise LotSizeViolation(
            f"qty {rounded_qty} is not a multiple of lot size {lot}. "
            f"Use {nearest_down} or {nearest_up}.",
        )


def validate_band(
    price: float,
    prev_close: float | None,
    band_pct: float,
) -> None:
    """Raise :class:`PriceBandViolation` if ``price`` is outside the band.

    ``prev_close=None`` skips the check (e.g. first-day listing or the
    simulator hasn't seen a close yet).
    """
    if prev_close is None or prev_close <= 0 or band_pct <= 0:
        return
    upper = prev_close * (1.0 + band_pct)
    lower = prev_close * (1.0 - band_pct)
    if price > upper or price < lower:
        raise PriceBandViolation(
            f"Price ₹{price:.4f} is outside the daily band "
            f"[₹{lower:.4f}, ₹{upper:.4f}] (prev close ₹{prev_close:.4f}, "
            f"band ±{band_pct * 100:.1f}%).",
        )


# ── Synthetic order book ─────────────────────────────────────────────


@dataclass(frozen=True)
class OrderBookConfig:
    """Knobs for the parametric L2 book.

    The book has ``levels`` price levels on each side. The size at the
    top of book is ``adv * depth_pct_of_adv``; sizes at deeper levels
    decay geometrically by ``shape_decay`` (0.6 = each next level holds
    60% of the previous). Spreads come from the provider's bid/ask when
    available; with no real bid/ask the broker skips book impact and
    defers to the slippage model so we don't double-charge.

    Defaults are tuned for liquid Indian large-caps (RELIANCE, TCS,
    HDFCBANK). For a small-cap, drop ``depth_pct_of_adv`` to ~0.0005 and
    raise ``default_spread_bps`` to ~25.
    """

    enabled: bool = True               # default ON for active development
    levels: int = 10
    depth_pct_of_adv: float = 0.005    # 0.5% of ADV at the touch
    shape_decay: float = 0.6           # geometric decay per level
    default_spread_bps: float = 5.0    # fallback when bid/ask unknown
    default_adv: float = 100_000.0     # fallback ADV for unknown symbols
    # Almgren impact coefficient: extra slippage on a market order is
    # ``coeff * (qty / adv) ** exponent`` (in bps). 50 bps for 100% of
    # ADV is a reasonable starting point for Indian large-caps.
    almgren_coeff_bps: float = 50.0
    almgren_exponent: float = 0.5      # square-root impact


@dataclass(frozen=True)
class BookLevel:
    """One side of one level of the synthetic L2 book."""

    price: float
    size: int


@dataclass(frozen=True)
class OrderBook:
    """A point-in-time snapshot of the synthetic book."""

    symbol: str
    bids: list[BookLevel]   # descending price (best bid first)
    asks: list[BookLevel]   # ascending price (best ask first)
    last: float
    tick_size: float

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2.0


@dataclass(frozen=True)
class FillSlice:
    """A piece of a market-order fill against one book level."""

    price: float
    qty: int


@dataclass(frozen=True)
class MarketFill:
    """The aggregated fill produced by walking the book."""

    avg_price: float
    filled_qty: int
    slices: list[FillSlice]
    impact_bps: float       # extra cost vs. mid, in bps
    fully_filled: bool      # False when the book ran out of size


class OrderBookSimulator:
    """Parametric L2 book + queue position bookkeeping.

    Stateful only as far as queue position is concerned: each pending
    limit order reserves a "shares-ahead" slot at its price level. As
    the simulator observes trades at that level, ahead-of-me size
    decreases until the order fills.

    For market orders, the simulator walks the synthesized book to
    produce a realistic VWAP fill plus an impact cost in bps.
    """

    def __init__(self, config: OrderBookConfig | None = None) -> None:
        self.config = config or OrderBookConfig()
        # Per-symbol queue tracking: {(symbol, side, price): shares_ahead}.
        self._queue: dict[tuple[str, str, float], int] = {}

    # ── Book synthesis ────────────────────────────────────────────────

    def synthesize(
        self,
        symbol: str,
        last: float,
        bid: float | None,
        ask: float | None,
        adv: float | None,
        tick_size: float,
    ) -> OrderBook:
        """Build a synthetic L2 book from the provider's spot data.

        Parameters
        ----------
        symbol:
            The scrip.
        last:
            Last traded price (the anchor when bid/ask unknown).
        bid, ask:
            Real top-of-book quotes when the provider supplies them.
            ``None`` falls back to ``last × (1 ± spread/2)``.
        adv:
            Average daily volume in shares. Drives sizing across levels.
            ``None`` uses :attr:`OrderBookConfig.default_adv`.
        tick_size:
            Per-symbol tick. Levels are spaced one tick apart, like NSE.
        """
        cfg = self.config
        adv_val = adv if adv and adv > 0 else cfg.default_adv

        if bid is None or ask is None or bid >= ask or bid <= 0:
            half_spread = last * (cfg.default_spread_bps / 10000.0) / 2.0
            bid = last - max(half_spread, tick_size / 2.0)
            ask = last + max(half_spread, tick_size / 2.0)
        # Snap each side to the tick grid so levels are realistic.
        bid = round_to_tick(bid, tick_size) or bid
        ask = round_to_tick(ask, tick_size) or ask

        top_size = max(1, int(round(adv_val * cfg.depth_pct_of_adv)))
        bids: list[BookLevel] = []
        asks: list[BookLevel] = []
        for i in range(cfg.levels):
            size = max(1, int(round(top_size * (cfg.shape_decay ** i))))
            bids.append(BookLevel(price=bid - i * tick_size, size=size))
            asks.append(BookLevel(price=ask + i * tick_size, size=size))

        return OrderBook(
            symbol=symbol,
            bids=bids,
            asks=asks,
            last=last,
            tick_size=tick_size,
        )

    # ── Market-order walk ─────────────────────────────────────────────

    def walk_book(
        self,
        book: OrderBook,
        side: OrderSide,
        qty: int,
    ) -> MarketFill:
        """Consume size from the appropriate side until ``qty`` is filled.

        Returns a :class:`MarketFill`. If the book runs dry,
        ``fully_filled`` is False and ``filled_qty < qty``; the caller
        decides whether to retry or partial-fill the order.

        Impact cost is reported in bps off the mid (positive cost on
        both sides — buys pay above mid, sells receive below).
        """
        levels = book.asks if side == OrderSide.BUY else book.bids
        slices: list[FillSlice] = []
        remaining = int(qty)
        spent = 0.0
        filled = 0

        for level in levels:
            if remaining <= 0:
                break
            take = min(remaining, level.size)
            slices.append(FillSlice(price=level.price, qty=take))
            spent += level.price * take
            filled += take
            remaining -= take

        # Almgren add-on for orders that try to clear more than ADV.
        # Caller-supplied ADV would let us scale, but the book already
        # reflects ADV; we surface the impact rather than charging again.
        if filled == 0:
            return MarketFill(
                avg_price=0.0,
                filled_qty=0,
                slices=[],
                impact_bps=0.0,
                fully_filled=False,
            )

        avg = spent / filled
        mid = book.mid or book.last
        if side == OrderSide.BUY:
            impact_bps = (avg - mid) / mid * 10000.0
        else:
            impact_bps = (mid - avg) / mid * 10000.0
        return MarketFill(
            avg_price=avg,
            filled_qty=filled,
            slices=slices,
            impact_bps=max(0.0, impact_bps),
            fully_filled=remaining <= 0,
        )

    def almgren_impact_bps(
        self,
        qty: int,
        adv: float | None,
    ) -> float:
        """Stand-alone Almgren impact estimate in bps, for sizing tools.

        ``cost = coeff * (qty / adv) ** exponent`` (bps). Returns 0 when
        ``adv`` is ``None`` or non-positive.
        """
        if not adv or adv <= 0 or qty <= 0:
            return 0.0
        ratio = qty / adv
        return self.config.almgren_coeff_bps * (ratio ** self.config.almgren_exponent)

    # ── Queue position bookkeeping ────────────────────────────────────

    def join_queue(
        self,
        symbol: str,
        side: OrderSide,
        price: float,
        book: OrderBook,
    ) -> int:
        """Record that a new limit order is joining the back of the queue
        at ``(symbol, side, price)``. Returns its initial shares-ahead.

        We use the synthesized depth at that level as the ahead-of-me
        seed. Subsequent orders at the same price level stack behind us
        (their ahead = ours + our_qty).
        """
        levels = book.bids if side == OrderSide.BUY else book.asks
        ahead = 0
        for level in levels:
            if math.isclose(level.price, price, rel_tol=0.0, abs_tol=1e-6):
                ahead = level.size
                break
        key = (symbol, side.value, round_to_tick(price, book.tick_size) or price)
        # Stack behind any existing orders we've already queued at this level.
        existing = self._queue.get(key, 0)
        total = ahead + existing
        self._queue[key] = total
        return total

    def observe_trade(
        self,
        symbol: str,
        side: OrderSide,
        price: float,
        qty: int,
    ) -> None:
        """Tell the simulator that ``qty`` shares traded at ``price``.

        Decreases the "ahead of me" counter for any of our queued
        limits sitting at that price level. Trade is on the *opposite*
        side from the resting limit, so a buyer hitting the ask reduces
        the ask queue.
        """
        # A buy print eats ask-side queue; a sell print eats bid-side.
        resting_side = "sell" if side == OrderSide.BUY else "buy"
        # Walk all keys at this price for the resting side.
        for key in list(self._queue.keys()):
            sym, ks, p = key
            if sym != symbol or ks != resting_side:
                continue
            if not math.isclose(p, price, rel_tol=0.0, abs_tol=1e-4):
                continue
            self._queue[key] = max(0, self._queue[key] - qty)

    def queue_position(
        self,
        symbol: str,
        side: OrderSide,
        price: float,
        tick_size: float,
    ) -> int | None:
        """Current shares-ahead count for a limit at ``(symbol, side, price)``.

        Returns ``None`` if we never recorded a join at that level.
        """
        key = (symbol, side.value, round_to_tick(price, tick_size) or price)
        return self._queue.get(key)

    def clear_queue(self, symbol: str | None = None) -> None:
        """Drop queue state. Optionally narrow to one symbol."""
        if symbol is None:
            self._queue.clear()
            return
        for key in list(self._queue):
            if key[0] == symbol:
                del self._queue[key]
