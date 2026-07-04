"""Domain models for the India paper broker.

Strict typing, immutable dataclasses (``frozen=True``). The broker mutates
state via the persistence layer — domain objects themselves are values.

These shapes are intentionally close to Alpaca's dataclasses so a caller
that consumes either type sees a familiar interface. The differences
(``exchange``, ``fees_paid``, ``realized_pl``) are India-specific
additions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Literal


class OrderType(str, Enum):
    """Order type.

    - ``MARKET``: fills instantly at last known price (slippage-adjusted).
    - ``LIMIT``: queued, fills when the market crosses the limit price.
    - ``STOP_MARKET``: queued, fires a market order once the stop price
      is touched (BUY: when last >= stop; SELL: when last <= stop).
      Used for stop-losses on long positions and breakout entries.
    - ``STOP_LIMIT``: queued, fires a *limit* order once the stop price
      is touched. Avoids the "stop hits and slips through bad price"
      failure mode at the cost of possibly not filling at all.
    - ``BRACKET``: parent order (entry as MARKET or LIMIT) plus child
      stop-loss + child target. Children auto-cancel each other on
      fill (OCO — One-Cancels-Other) and auto-cancel if the parent is
      cancelled before fill.
    """

    MARKET = "market"
    LIMIT = "limit"
    STOP_MARKET = "stop_market"
    STOP_LIMIT = "stop_limit"
    BRACKET = "bracket"


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(str, Enum):
    """Order lifecycle states.

    States used today by the simulator:
      - PENDING: limit order queued, awaiting price condition
      - FILLED: market order or limit order completed
      - CANCELLED: explicitly cancelled by the user before fill
      - EXPIRED: DAY-tif limit order swept at session close

    Reserved for future use (defined for forward compatibility):
      - PARTIALLY_FILLED: when partial-fill modeling is added
      - REJECTED: surfaced when broker-side validation rejects pre-fill
    """

    PENDING = "pending"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


class Exchange(str, Enum):
    NSE = "NSE"
    BSE = "BSE"


class ProductType(str, Enum):
    """Indian retail product type, in the Zerodha/Upstox parlance.

    - ``DELIVERY`` (CNC at Zerodha, D at Upstox): T+1 settlement, no
      auto-square-off, attracts STT on delivery rates and DP charges.
      The default for most strategies.
    - ``INTRADAY`` (MIS): same-session round-trip, no DP charge, broker
      auto-squares-off any open MIS positions at 15:15 IST. Modeled
      via :class:`papertrade_india.SettlementEngine`.
    - ``MARGIN``, ``PLEDGE``: explicit "not modeled" sentinels. The
      simulator is cash-equity only; orders submitted with these
      product types raise :class:`papertrade_india.MarginNotSupported`
      so failures are loud rather than silent.

    Out of scope: NRML (overnight derivatives), BO/CO (cover/bracket as
    separate product flags — we model bracket as an order type instead),
    MTF (margin trading).
    """

    DELIVERY = "delivery"
    INTRADAY = "intraday"
    MARGIN = "margin"
    PLEDGE = "pledge"


class TimeInForce(str, Enum):
    """Order lifetime / queueing behavior.

    - ``DAY``: pending until the session ends, then expired by
      :meth:`IndiaPaperBroker.expire_stale_day_orders`. The default.
    - ``GTT`` (Good-Till-Triggered): NSE's persistent stop-style order
      that survives across sessions. Stays PENDING across day rollovers
      until the price condition triggers it or the user cancels.
      Most useful with STOP_MARKET / STOP_LIMIT order types.
    - ``IOC`` (Immediate-Or-Cancel): fill what's immediately available,
      cancel the rest. Reserved — not yet implemented.
    - ``AMO`` (After-Market Order): queues overnight. The watcher fires
      it at the next session open. Supported for MARKET and LIMIT.

    The string is held on :attr:`Order.time_in_force` for back-compat;
    this enum is the authoritative set of values the simulator supports.
    """

    DAY = "DAY"
    GTT = "GTT"
    IOC = "IOC"
    AMO = "AMO"


Currency = Literal["INR", "USD"]


@dataclass(frozen=True)
class Position:
    """Open position with mark-to-market valuation."""

    symbol: str
    exchange: Exchange
    qty: float
    avg_cost: float                  # Per-share economic basis (INR), incl. prorated buy fees
    current_price: float             # Last known market price (or avg_cost if stale)
    market_value: float              # qty * current_price
    cost_basis: float                # qty * avg_cost
    unrealized_pl: float             # market_value - cost_basis
    unrealized_pl_percent: float     # (unrealized_pl / cost_basis) * 100
    entry_date: datetime
    # True when the price feed could not produce a fresh quote and the
    # broker fell back to ``avg_cost``. Lets agents distinguish a real
    # break-even position from a stale-valuation one.
    current_price_stale: bool = False
    # Mark-to-market basis. ``"last"`` is the historical default;
    # ``"bid"`` (long) / ``"ask"`` (short) is what real brokers use to
    # compute exit P&L. Set to ``"mid"`` when only mid is available.
    mark_basis: str = "last"


@dataclass(frozen=True)
class Order:
    """Submitted order (filled, pending, or terminal)."""

    id: str
    symbol: str
    exchange: Exchange
    side: OrderSide
    qty: float
    order_type: OrderType
    status: OrderStatus
    filled_qty: float = 0.0
    filled_avg_price: float | None = None
    limit_price: float | None = None
    fees_paid: float = 0.0           # Total Indian fees on this order
    realized_pl: float = 0.0         # Non-zero on sells (after fees, full round-trip)
    time_in_force: str = "DAY"
    created_at: datetime = field(default_factory=datetime.now)
    filled_at: datetime | None = None
    cancelled_at: datetime | None = None
    expired_at: datetime | None = None
    rejection_reason: str | None = None
    # Stop / Bracket extensions (default ``None`` for plain MARKET/LIMIT).
    stop_price: float | None = None        # STOP_MARKET / STOP_LIMIT
    target_price: float | None = None      # BRACKET take-profit leg
    parent_order_id: str | None = None     # BRACKET child → parent linkage
    product_type: ProductType = ProductType.DELIVERY
    triggered_at: datetime | None = None   # When a STOP transitioned PENDING → working


@dataclass(frozen=True)
class Account:
    """Account summary: cash, holdings value, P&L."""

    account_id: str
    equity: float                    # cash + portfolio_value
    cash: float
    portfolio_value: float           # Sum of all positions' market_value
    buying_power: float              # cash minus pending buy-limit notional
    realized_pl_total: float         # Cumulative realized P&L (after fees)
    unrealized_pl_total: float       # Sum across positions
    currency: Currency = "INR"
    updated_at: datetime = field(default_factory=datetime.now)


@dataclass(frozen=True)
class Trade:
    """Atomic execution event. One order can produce multiple trades.

    Currently the simulator emits exactly one trade per filled order, but
    the schema is shaped for future partial-fill support.
    """

    id: str
    order_id: str
    symbol: str
    side: OrderSide
    qty: float
    price: float
    fees: float
    realized_pl: float = 0.0         # Only on sells
    executed_at: datetime = field(default_factory=datetime.now)
