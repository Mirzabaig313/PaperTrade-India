"""Domain models for the India paper broker.

Strict typing, immutable dataclasses (``frozen=True``). The broker mutates
state via the persistence layer — domain objects themselves are values.

These shapes are intentionally close to the Alpaca-side dataclasses so an
agent that consumes either type sees a familiar interface. The differences
(``exchange``, ``fees_paid``, ``realized_pl``) are India-specific additions
that wouldn't fit on the Alpaca side.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Literal


class OrderType(str, Enum):
    """Order type. ``MARKET`` fills instantly at last known price;
    ``LIMIT`` is queued and filled by ``LimitOrderWatcher`` when the
    market crosses the limit price."""

    MARKET = "market"
    LIMIT = "limit"


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
