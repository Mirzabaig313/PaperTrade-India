"""Order-state persistence and position helpers.

Owns every operation that touches the ``orders``, ``trades``, and
``positions`` tables directly:

- Inserting order and trade rows
- Hydrating rows back into :class:`Order` dataclasses
- Applying buy/sell fills to positions and the cash ledger
- Cancelling, expiring, and squaring-off orders
- Emitting position-boundary events (opened / closed)
- Resolving the current position qty for a symbol

All functions take ``ctx`` (:class:`BrokerContext`) as their first
argument so they can be tested without standing up the full broker.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from ..domain.exceptions import InsufficientFundsError, InsufficientSharesError
from ..domain.models import (
    Exchange,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    ProductType,
)
from ..infrastructure import ledger as _ledger

if TYPE_CHECKING:  # pragma: no cover
    from .._context import BrokerContext

logger = logging.getLogger(__name__)

_QTY_EPSILON = 1e-9


# ── Row insert helpers ────────────────────────────────────────────────


def record(
    ctx: BrokerContext,
    conn: sqlite3.Connection,
    order_id: str,
    symbol: str,
    side: OrderSide,
    qty: float,
    order_type: OrderType,
    status: OrderStatus,
    filled_qty: float,
    filled_avg_price: float | None,
    limit_price: float | None,
    fees_paid: float,
    realized_pl: float,
    time_in_force: str,
    created_at: str,
    filled_at: str | None = None,
    stop_price: float | None = None,
    target_price: float | None = None,
    parent_order_id: str | None = None,
    product_type: ProductType = ProductType.DELIVERY,
    triggered_at: str | None = None,
) -> None:
    """Insert one row into the ``orders`` table."""
    conn.execute(
        "INSERT INTO orders "
        "(id, account_id, symbol, exchange, side, qty, order_type, "
        "status, filled_qty, filled_avg_price, limit_price, fees_paid, "
        "realized_pl, time_in_force, created_at, filled_at, "
        "stop_price, target_price, parent_order_id, product_type, "
        "triggered_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "?, ?, ?, ?, ?)",
        (
            order_id,
            ctx.account_id,
            symbol,
            ctx.default_exchange.value,
            side.value,
            qty,
            order_type.value,
            status.value,
            filled_qty,
            filled_avg_price,
            limit_price,
            fees_paid,
            realized_pl,
            time_in_force,
            created_at,
            filled_at,
            stop_price,
            target_price,
            parent_order_id,
            product_type.value,
            triggered_at,
        ),
    )


def record_trade(
    ctx: BrokerContext,
    conn: sqlite3.Connection,
    order_id: str,
    symbol: str,
    side: OrderSide,
    qty: float,
    price: float,
    fees: float,
    realized_pl: float,
    executed_at: str,
) -> None:
    """Insert one row into the ``trades`` table."""
    conn.execute(
        "INSERT INTO trades "
        "(id, order_id, account_id, symbol, side, qty, price, fees, "
        "realized_pl, executed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            uuid.uuid4().hex[:12],
            order_id,
            ctx.account_id,
            symbol,
            side.value,
            qty,
            price,
            fees,
            realized_pl,
            executed_at,
        ),
    )


def row_to_order(row: sqlite3.Row) -> Order:
    """Hydrate an :class:`Order` from a SQLite row.

    Handles legacy rows that lack the newer realism columns gracefully.
    """
    cols = set(row.keys())
    return Order(
        id=row["id"],
        symbol=row["symbol"],
        exchange=Exchange(row["exchange"]),
        side=OrderSide(row["side"]),
        qty=row["qty"],
        order_type=OrderType(row["order_type"]),
        status=OrderStatus(row["status"]),
        filled_qty=row["filled_qty"] or 0.0,
        filled_avg_price=row["filled_avg_price"],
        limit_price=row["limit_price"],
        fees_paid=row["fees_paid"] or 0.0,
        realized_pl=row["realized_pl"] or 0.0,
        time_in_force=row["time_in_force"] or "DAY",
        created_at=datetime.fromisoformat(row["created_at"]),
        filled_at=(
            datetime.fromisoformat(row["filled_at"])
            if row["filled_at"] else None
        ),
        cancelled_at=(
            datetime.fromisoformat(row["cancelled_at"])
            if row["cancelled_at"] else None
        ),
        expired_at=(
            datetime.fromisoformat(row["expired_at"])
            if row["expired_at"] else None
        ),
        rejection_reason=row["rejection_reason"],
        stop_price=row["stop_price"] if "stop_price" in cols else None,
        target_price=row["target_price"] if "target_price" in cols else None,
        parent_order_id=(
            row["parent_order_id"] if "parent_order_id" in cols else None
        ),
        product_type=ProductType(
            row["product_type"]
            if "product_type" in cols and row["product_type"]
            else ProductType.DELIVERY.value,
        ),
        triggered_at=(
            datetime.fromisoformat(row["triggered_at"])
            if "triggered_at" in cols and row["triggered_at"] else None
        ),
    )


# ── Position helpers ──────────────────────────────────────────────────


def symbol_position_qty(
    ctx: BrokerContext,
    conn: sqlite3.Connection,
    symbol: str,
) -> float:
    """Return the current qty for (account_id, symbol), or 0.0."""
    row = conn.execute(
        "SELECT qty FROM positions WHERE account_id = ? AND symbol = ?",
        (ctx.account_id, symbol),
    ).fetchone()
    return row["qty"] if row else 0.0


def emit_position_events(
    ctx: BrokerContext,
    conn: sqlite3.Connection,
    order: Order,
    qty_before: bool,
    qty_after: float,
) -> None:
    """Emit position_opened / position_closed at zero-crossing boundaries.

    ``qty_before`` is a bool (held / not held); we only care about the
    open/close edges.
    """
    if not qty_before and qty_after > 0:
        ctx.emit(
            conn,
            event_type="position_opened",
            order_id=order.id,
            payload={"symbol": order.symbol, "qty": qty_after},
        )
    elif qty_before and qty_after <= _QTY_EPSILON:
        ctx.emit(
            conn,
            event_type="position_closed",
            order_id=order.id,
            payload={"symbol": order.symbol},
        )


def apply_buy(
    ctx: BrokerContext,
    conn: sqlite3.Connection,
    symbol: str,
    qty: float,
    price: float,
    fees: float,
    now: str,
    order_id: str | None = None,
) -> None:
    """Deduct cash, update or create the position.

    ``avg_cost`` includes fees: the new per-share cost basis is
    ``(old_qty*old_avg + qty*price + fees) / new_qty``. This means
    ``qty * avg_cost`` always equals total cash spent on the position,
    and a later sell's realized P&L — computed as
    ``(price - avg_cost) * qty - sell_fees`` — naturally accounts for
    *both* sides of fees over the round-trip.

    Ledger: writes two cash-movement rows (buy_principal, buy_fees)
    so ``sum(movements) == account.cash`` stays an exact invariant.
    """
    principal = qty * price
    cost = principal + fees
    cash = conn.execute(
        "SELECT cash FROM account WHERE account_id = ?",
        (ctx.account_id,),
    ).fetchone()["cash"]

    if cost > cash:
        raise InsufficientFundsError(
            f"Need ₹{cost:,.2f} (incl fees ₹{fees:.2f}), have ₹{cash:,.2f}"
        )

    conn.execute(
        "UPDATE account SET cash = cash - ? WHERE account_id = ?",
        (cost, ctx.account_id),
    )

    _ledger.record(
        conn,
        account_id=ctx.account_id,
        amount=-principal,
        reason="buy_principal",
        recorded_at_iso=now,
        order_id=order_id,
        symbol=symbol,
    )
    if fees != 0:
        _ledger.record(
            conn,
            account_id=ctx.account_id,
            amount=-fees,
            reason="buy_fees",
            recorded_at_iso=now,
            order_id=order_id,
            symbol=symbol,
        )

    existing = conn.execute(
        "SELECT qty, avg_cost FROM positions "
        "WHERE account_id = ? AND symbol = ?",
        (ctx.account_id, symbol),
    ).fetchone()

    if existing:
        old_qty = existing["qty"]
        old_avg = existing["avg_cost"]
        new_qty = old_qty + qty
        new_avg = ((old_avg * old_qty) + (price * qty) + fees) / new_qty
        conn.execute(
            "UPDATE positions SET qty = ?, avg_cost = ? "
            "WHERE account_id = ? AND symbol = ?",
            (new_qty, new_avg, ctx.account_id, symbol),
        )
    else:
        avg_cost = (price * qty + fees) / qty
        conn.execute(
            "INSERT INTO positions "
            "(account_id, symbol, exchange, qty, avg_cost, entry_date) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                ctx.account_id, symbol, ctx.default_exchange.value,
                qty, avg_cost, now,
            ),
        )


def apply_sell(
    ctx: BrokerContext,
    conn: sqlite3.Connection,
    symbol: str,
    qty: float,
    price: float,
    fees: float,
    now: str,
    order_id: str | None = None,
) -> float:
    """Credit cash, update or close position. Returns realized P&L.

    Realized P&L = ``(price - avg_cost) * qty - sell_fees``. Because
    ``avg_cost`` already includes prorated buy-side fees, this is the
    true round-trip P&L net of all fees.

    Ledger: writes two cash-movement rows (sell_principal positive,
    sell_fees negative) so ``sum(movements) == account.cash`` stays
    exact across round-trips.
    """
    existing = conn.execute(
        "SELECT qty, avg_cost FROM positions "
        "WHERE account_id = ? AND symbol = ?",
        (ctx.account_id, symbol),
    ).fetchone()

    if not existing or existing["qty"] < qty:
        held = existing["qty"] if existing else 0
        raise InsufficientSharesError(
            f"Want to sell {qty} of {symbol}, hold {held}"
        )

    old_qty = existing["qty"]
    avg_cost = existing["avg_cost"]
    principal = qty * price
    proceeds = principal - fees
    realized_pl = (price - avg_cost) * qty - fees

    conn.execute(
        "UPDATE account SET cash = cash + ?, "
        "realized_pl_total = realized_pl_total + ? "
        "WHERE account_id = ?",
        (proceeds, realized_pl, ctx.account_id),
    )

    _ledger.record(
        conn,
        account_id=ctx.account_id,
        amount=principal,
        reason="sell_principal",
        recorded_at_iso=now,
        order_id=order_id,
        symbol=symbol,
    )
    if fees != 0:
        _ledger.record(
            conn,
            account_id=ctx.account_id,
            amount=-fees,
            reason="sell_fees",
            recorded_at_iso=now,
            order_id=order_id,
            symbol=symbol,
        )

    new_qty = old_qty - qty
    if new_qty <= _QTY_EPSILON:
        conn.execute(
            "DELETE FROM positions "
            "WHERE account_id = ? AND symbol = ?",
            (ctx.account_id, symbol),
        )
    else:
        conn.execute(
            "UPDATE positions SET qty = ? "
            "WHERE account_id = ? AND symbol = ?",
            (new_qty, ctx.account_id, symbol),
        )

    return realized_pl


# ── Order lifecycle transitions ───────────────────────────────────────


def cancel(ctx: BrokerContext, order_id: str) -> bool:
    """Cancel a pending or partially-filled order. Race-safe.

    Bracket-aware: if the cancelled order has children (bracket parent),
    the children are cancelled in the same transaction. If the cancelled
    order *is* a child, we leave the sibling alone — only OCO-on-fill
    cancels siblings.
    """
    with ctx.persistence.transaction() as conn:
        cur = conn.execute(
            "UPDATE orders SET status = ?, cancelled_at = ? "
            "WHERE id = ? AND account_id = ? AND status IN (?, ?)",
            (
                OrderStatus.CANCELLED.value,
                ctx.now_iso(),
                order_id,
                ctx.account_id,
                OrderStatus.PENDING.value,
                OrderStatus.PARTIALLY_FILLED.value,
            ),
        )
        cancelled = cur.rowcount == 1
        if cancelled:
            child_cur = conn.execute(
                "UPDATE orders SET status = ?, cancelled_at = ?, "
                "rejection_reason = 'parent cancelled' "
                "WHERE account_id = ? AND parent_order_id = ? "
                "AND status IN (?, ?)",
                (
                    OrderStatus.CANCELLED.value, ctx.now_iso(),
                    ctx.account_id, order_id,
                    OrderStatus.PENDING.value,
                    OrderStatus.PARTIALLY_FILLED.value,
                ),
            )
            ctx.emit(
                conn,
                event_type="order_cancelled",
                order_id=order_id,
                payload=(
                    {"children_cancelled": child_cur.rowcount}
                    if child_cur.rowcount else {}
                ),
            )
    if cancelled:
        ctx.drain_pending_events()
        logger.info("CANCEL order %s", order_id)
    return cancelled


def cancel_all(ctx: BrokerContext) -> int:
    """Cancel every pending order on the account. Returns count."""
    with ctx.persistence.read() as conn:
        rows = conn.execute(
            "SELECT id FROM orders WHERE account_id = ? AND status = ?",
            (ctx.account_id, OrderStatus.PENDING.value),
        ).fetchall()
    return sum(1 for r in rows if cancel(ctx, r["id"]))


def expire_day_orders(ctx: BrokerContext) -> int:
    """Mark all PENDING DAY-tif orders as EXPIRED in one transaction.

    Call from a session-close hook. GTT and AMO orders are spared.
    Returns the count expired.
    """
    with ctx.persistence.transaction() as conn:
        rows = conn.execute(
            "SELECT id FROM orders "
            "WHERE account_id = ? AND status = ? AND time_in_force = 'DAY'",
            (ctx.account_id, OrderStatus.PENDING.value),
        ).fetchall()
        expired_ids = [r["id"] for r in rows]
        cur = conn.execute(
            "UPDATE orders SET status = ?, expired_at = ? "
            "WHERE account_id = ? AND status = ? AND time_in_force = 'DAY'",
            (
                OrderStatus.EXPIRED.value,
                ctx.now_iso(),
                ctx.account_id,
                OrderStatus.PENDING.value,
            ),
        )
        n = cur.rowcount
        for oid in expired_ids:
            ctx.emit(conn, event_type="order_expired", order_id=oid)
    ctx.drain_pending_events()
    if n:
        logger.info("EXPIRE %d DAY order(s) on account %s", n, ctx.account_id)
    return n


def square_off_intraday(ctx: BrokerContext) -> int:
    """Close all open INTRADAY positions at market.

    Called from the watcher at auto-square-off time. Each round-trip
    flows through the canonical sell path so fees, ledger, and events
    fire normally. Returns the count of positions squared off.
    """
    with ctx.persistence.read() as conn:
        today = ctx.clock.now().date().isoformat()
        rows = conn.execute(
            "SELECT symbol, SUM(CASE WHEN side='buy' THEN filled_qty "
            "ELSE -filled_qty END) AS net "
            "FROM orders WHERE account_id = ? AND product_type = 'intraday' "
            "AND status IN ('filled','partially_filled') "
            "AND substr(created_at, 1, 10) = ? "
            "GROUP BY symbol HAVING net > 0",
            (ctx.account_id, today),
        ).fetchall()
        squared_targets = [(r["symbol"], float(r["net"])) for r in rows]

    # Import here to avoid a circular import at module load time.

    # We need the broker's sell() to route through the full pipeline.
    # square_off_intraday is called from the broker itself, so we
    # reconstruct the call via the context's back-reference.
    n = 0
    for symbol, qty in squared_targets:
        try:
            # Reach the broker via the context's persistence handle —
            # the broker is the only object that owns a Persistence
            # instance with this db_path, so we can't reconstruct it
            # here. Instead, we call the module-level helper that
            # accepts a ctx directly.
            _square_off_one(ctx, symbol, qty)
            n += 1
            logger.info("AUTO-SQUARE-OFF %s qty=%g (intraday)", symbol, qty)
        except Exception as e:  # noqa: BLE001
            logger.exception("Auto-square-off failed for %s: %s", symbol, e)
    return n


def _square_off_one(ctx: BrokerContext, symbol: str, qty: float) -> None:
    """Execute a single intraday square-off via the market execution path."""
    from .market import execute as _execute_market  # noqa: PLC0415

    _execute_market(
        ctx,
        symbol=symbol,
        qty=qty,
        side=OrderSide.SELL,
        time_in_force="DAY",
        product_type=ProductType.INTRADAY,
    )


# ── Bracket OCO helpers ───────────────────────────────────────────────


def cancel_bracket_siblings(
    ctx: BrokerContext,
    conn: sqlite3.Connection,
    filled_child: Order,
) -> None:
    """One-Cancels-Other: when one bracket child fills, cancel its sibling.

    Both are written in one UPDATE so the OCO is atomic.
    """
    parent = filled_child.parent_order_id
    if parent is None:
        return
    cur = conn.execute(
        "UPDATE orders SET status = ?, cancelled_at = ?, "
        "rejection_reason = 'OCO sibling filled' "
        "WHERE account_id = ? AND parent_order_id = ? AND id != ? "
        "AND status IN (?, ?)",
        (
            OrderStatus.CANCELLED.value, ctx.now_iso(),
            ctx.account_id, parent, filled_child.id,
            OrderStatus.PENDING.value, OrderStatus.PARTIALLY_FILLED.value,
        ),
    )
    if cur.rowcount:
        ctx.emit(
            conn, event_type="bracket_oco_cancelled",
            order_id=filled_child.id,
            payload={"parent": parent, "siblings_cancelled": cur.rowcount},
        )


def rebalance_bracket_sibling_qty(
    ctx: BrokerContext,
    conn: sqlite3.Connection,
    filling_child: Order,
    filled_qty_so_far: float,
) -> None:
    """Shrink the OCO sibling's outstanding qty to match this child's progress.

    When a bracket child partial-fills, the sibling no longer needs to
    cover the just-closed shares. Total exposure across siblings =
    remaining parent position.
    """
    parent_id = filling_child.parent_order_id
    if parent_id is None:
        return
    parent_row = conn.execute(
        "SELECT filled_qty FROM orders WHERE id = ? AND account_id = ?",
        (parent_id, ctx.account_id),
    ).fetchone()
    if parent_row is None:
        return
    parent_filled = float(parent_row["filled_qty"])
    new_sibling_qty = parent_filled - filled_qty_so_far
    if new_sibling_qty <= 0:
        return
    conn.execute(
        "UPDATE orders SET qty = ? "
        "WHERE account_id = ? AND parent_order_id = ? AND id != ? "
        "AND status IN (?, ?) AND qty > ?",
        (
            new_sibling_qty,
            ctx.account_id, parent_id, filling_child.id,
            OrderStatus.PENDING.value, OrderStatus.PARTIALLY_FILLED.value,
            new_sibling_qty,
        ),
    )


__all__ = [
    "record",
    "record_trade",
    "row_to_order",
    "symbol_position_qty",
    "emit_position_events",
    "apply_buy",
    "apply_sell",
    "cancel",
    "cancel_all",
    "expire_day_orders",
    "square_off_intraday",
    "cancel_bracket_siblings",
    "rebalance_bracket_sibling_qty",
]
