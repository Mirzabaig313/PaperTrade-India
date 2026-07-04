"""Pre-open auction matching.

NSE runs an order-collection auction from 09:00 to 09:08 IST. During
this window:

- Buyers and sellers submit limit orders. No continuous matching.
- At 09:08 the exchange computes a single equilibrium price ("call
  auction" rules).
- Orders that cross at the equilibrium price fill there. Orders that
  don't cross transition to the regular session and queue normally.

Our equilibrium algorithm
-------------------------
We use the same rules NSE publishes:

1. **Maximum executable volume**: pick the price that maximizes the
   total tradeable quantity (min(buy_qty_at_price, sell_qty_at_price)).
2. **Minimum imbalance**: among prices with equal max volume, pick the
   one with the smallest |buy_qty - sell_qty|.
3. **Tie-break by price proximity to last close**: among prices still
   tied, pick the one closest to the previous day's close (which we
   approximate from :attr:`MarketQuote.prev_close` or, failing that,
   the current ``last`` price).
4. **Lexicographic by price** if all else ties — pick the higher price
   so buyers get filled at "their" price.

Why all-or-nothing pro-rata
---------------------------
Real NSE pro-rates allocations among orders at each price level. We
simplify: orders at the equilibrium price (or better) fill *fully* in
order of submission until the matched volume is exhausted. The leftover
orders pass through to the regular session unchanged — they just keep
their queued state.

What this does not model
------------------------
- Iceberg / disclosed-qty orders.
- Indicative price computation that real NSE publishes during the window.
- AON (all-or-none) and FOK (fill-or-kill) flag handling — every order
  is treated as "ordinary limit, partial OK on rollover".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .._context import BrokerContext

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _BookRow:
    """One side of one price level in the auction book."""

    price: float
    qty: float
    order_id: str
    submission_seq: int


@dataclass(frozen=True)
class AuctionMatch:
    """Result of a pre-open auction run.

    Fields
    ------
    equilibrium_price:
        The clearing price (``None`` when no overlap → no fills).
    matched_volume:
        Total quantity matched at the equilibrium price.
    fills:
        ``[(order_id, fill_qty, fill_price)]`` for every order that
        partially or fully filled. Caller is responsible for applying
        the fills via :meth:`IndiaPaperBroker._execute_limit_fill` or
        equivalent.
    """

    equilibrium_price: float | None
    matched_volume: float
    fills: list[tuple[str, float, float]]


def compute_equilibrium(
    buys: list[_BookRow],
    sells: list[_BookRow],
    reference_price: float | None = None,
) -> AuctionMatch:
    """Run the auction over the given two-sided book.

    Parameters
    ----------
    buys, sells:
        The collected limit orders for each side. Caller orders them by
        submission time so we can assign fills first-in-first-out.
    reference_price:
        Used as the price-proximity tie-break (typically previous close
        or current last). ``None`` falls back to the midpoint of best
        bid/ask.

    Returns
    -------
    :class:`AuctionMatch`. ``equilibrium_price`` is ``None`` when no
    crossover exists (no fills happen).
    """
    if not buys or not sells:
        return AuctionMatch(equilibrium_price=None, matched_volume=0.0, fills=[])

    # Quick non-overlap check: highest buy < lowest sell ⇒ no auction match.
    max_buy = max(b.price for b in buys)
    min_sell = min(s.price for s in sells)
    if max_buy < min_sell:
        return AuctionMatch(equilibrium_price=None, matched_volume=0.0, fills=[])

    # Candidate prices: every distinct limit price on either side.
    candidates = sorted({b.price for b in buys} | {s.price for s in sells})

    best: tuple[float, float, float, float] | None = None  # (vol, -imbalance, -|p-ref|, p)
    if reference_price is None:
        # Midpoint of crossing range as a sane reference.
        reference_price = (max_buy + min_sell) / 2.0

    for p in candidates:
        # Buyers willing at price p: every buy with limit >= p.
        buy_vol = sum(b.qty for b in buys if b.price >= p)
        # Sellers willing at price p: every sell with limit <= p.
        sell_vol = sum(s.qty for s in sells if s.price <= p)
        vol = min(buy_vol, sell_vol)
        if vol <= 0:
            continue
        imbalance = abs(buy_vol - sell_vol)
        proximity = abs(p - reference_price)
        # Larger vol wins; smaller imbalance wins on tie; closer to ref
        # wins next; higher price wins last (favors buyers).
        score = (vol, -imbalance, -proximity, p)
        if best is None or score > best:
            best = score

    if best is None:
        return AuctionMatch(equilibrium_price=None, matched_volume=0.0, fills=[])
    matched_volume = best[0]
    equilibrium = best[3]

    # Allocate fills: FIFO across buys whose limit >= equilibrium, and
    # sells whose limit <= equilibrium, bounded by ``matched_volume``.
    fills: list[tuple[str, float, float]] = []
    eligible_buys = sorted(
        (b for b in buys if b.price >= equilibrium),
        key=lambda x: x.submission_seq,
    )
    eligible_sells = sorted(
        (s for s in sells if s.price <= equilibrium),
        key=lambda x: x.submission_seq,
    )

    remaining = matched_volume
    for order in eligible_buys:
        if remaining <= 0:
            break
        take = min(order.qty, remaining)
        fills.append((order.order_id, take, equilibrium))
        remaining -= take

    remaining = matched_volume
    for order in eligible_sells:
        if remaining <= 0:
            break
        take = min(order.qty, remaining)
        fills.append((order.order_id, take, equilibrium))
        remaining -= take

    return AuctionMatch(
        equilibrium_price=equilibrium,
        matched_volume=matched_volume,
        fills=fills,
    )


def run(ctx: BrokerContext) -> AuctionMatch:  # type: ignore[name-defined]
    """Match all PENDING limit orders for this account at the equilibrium price.

    Called by the watcher at the PRE_OPEN → REGULAR transition (or
    manually for tests). Implements NSE's call-auction rules: max
    executable volume → min imbalance → closest to reference price →
    higher price.

    Returns the :class:`AuctionMatch`. ``equilibrium_price=None`` when
    no overlap exists (no fills happen).

    Symbol scoping: the auction runs **per symbol**. We loop over every
    symbol with at least one PENDING limit on this account.
    """
    from ..domain.exceptions import OrderNoLongerPending  # noqa: PLC0415
    from ..domain.models import OrderSide, OrderStatus, OrderType  # noqa: PLC0415
    from . import state as _state  # noqa: PLC0415
    from .limit import fill as _limit_fill  # noqa: PLC0415

    with ctx.persistence.read() as conn:
        rows = conn.execute(
            "SELECT * FROM orders WHERE account_id = ? AND status = ? "
            "ORDER BY created_at ASC",
            (ctx.account_id, OrderStatus.PENDING.value),
        ).fetchall()

    all_pending = [
        _state.row_to_order(r) for r in rows
        if OrderType(r["order_type"]) == OrderType.LIMIT
        and r["parent_order_id"] is None
        and r["time_in_force"] in ("DAY", "GTT")
    ]
    if not all_pending:
        return AuctionMatch(equilibrium_price=None, matched_volume=0.0, fills=[])

    by_symbol: dict[str, list] = {}
    for o in all_pending:
        by_symbol.setdefault(o.symbol, []).append(o)

    all_fills: list[tuple[str, float, float]] = []
    total_matched = 0.0
    last_price: float | None = None

    for symbol, orders in by_symbol.items():
        buys = [
            _BookRow(
                price=o.limit_price,  # type: ignore[arg-type]
                qty=o.qty - o.filled_qty,
                order_id=o.id,
                submission_seq=int(o.created_at.timestamp() * 1000),
            )
            for o in orders if o.side == OrderSide.BUY
        ]
        sells = [
            _BookRow(
                price=o.limit_price,  # type: ignore[arg-type]
                qty=o.qty - o.filled_qty,
                order_id=o.id,
                submission_seq=int(o.created_at.timestamp() * 1000),
            )
            for o in orders if o.side == OrderSide.SELL
        ]
        try:
            ref_price = ctx.price_feed.get_price(symbol)
        except Exception:  # noqa: BLE001
            ref_price = None

        match = compute_equilibrium(buys=buys, sells=sells,
                                    reference_price=ref_price)
        if match.equilibrium_price is None:
            continue
        for fill_id, fill_qty, fill_price in match.fills:
            with ctx.persistence.read() as conn:
                row = conn.execute(
                    "SELECT * FROM orders WHERE id = ? AND account_id = ?",
                    (fill_id, ctx.account_id),
                ).fetchone()
            if row is None:
                continue
            order = _state.row_to_order(row)
            try:
                _limit_fill(ctx, order, fill_price, fill_qty=fill_qty)
                all_fills.append((fill_id, fill_qty, fill_price))
                total_matched += fill_qty
                last_price = fill_price
            except OrderNoLongerPending:
                continue

    return AuctionMatch(
        equilibrium_price=last_price,
        matched_volume=total_matched,
        fills=all_fills,
    )
