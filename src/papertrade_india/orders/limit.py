"""Limit order queueing and fill execution.

Two entry points:

- :func:`queue` — persist a new PENDING limit order and optionally
  prime its book-queue position.
- :func:`fill` — called by :class:`LimitOrderWatcher` when the market
  crosses the limit price; handles partial fills, bracket OCO, and
  settlement.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from ..domain.exceptions import OrderNoLongerPending
from ..domain.models import Order, OrderSide, OrderStatus, OrderType, ProductType
from ..execution.slippage import apply_slippage
from . import state as _state
from .book_helpers import maybe_join_book_queue
from .market import _maybe_settle_trade

if TYPE_CHECKING:  # pragma: no cover
    from .._context import BrokerContext

logger = logging.getLogger(__name__)


def queue(
    ctx: BrokerContext,
    symbol: str,
    qty: float,
    side: OrderSide,
    limit_price: float,
    time_in_force: str,
    product_type: ProductType = ProductType.DELIVERY,
) -> Order:
    """Persist a new PENDING limit order."""
    order_id = uuid.uuid4().hex[:12]
    now = ctx.now_iso()

    with ctx.persistence.transaction() as conn:
        _state.record(
            ctx, conn,
            order_id=order_id, symbol=symbol, side=side, qty=qty,
            order_type=OrderType.LIMIT, status=OrderStatus.PENDING,
            filled_qty=0.0, filled_avg_price=None, limit_price=limit_price,
            fees_paid=0.0, realized_pl=0.0, time_in_force=time_in_force,
            created_at=now, filled_at=None, product_type=product_type,
        )
        ctx.emit(conn, event_type="order_submitted", order_id=order_id,
                 payload={"symbol": symbol, "side": side.value, "qty": qty,
                          "order_type": OrderType.LIMIT.value,
                          "limit_price": limit_price,
                          "product_type": product_type.value})

    if ctx.book_sim.config.enabled:
        maybe_join_book_queue(ctx, symbol, side, limit_price)

    ctx.drain_pending_events()
    logger.info("QUEUE LIMIT %s %s %s @ ₹%.2f",
                side.value.upper(), qty, symbol, limit_price)

    with ctx.persistence.read() as conn:
        row = conn.execute(
            "SELECT * FROM orders WHERE id = ? AND account_id = ?",
            (order_id, ctx.account_id),
        ).fetchone()
    assert row is not None
    return _state.row_to_order(row)


def fill(
    ctx: BrokerContext,
    order: Order,
    fill_price: float,
    fill_qty: float | None = None,
) -> None:
    """Called by :class:`LimitOrderWatcher` when market crosses limit price.

    Race-safety: claims the order first (UPDATE WHERE status IN
    pending/partially_filled); raises :class:`OrderNoLongerPending` on
    a lost race so the watcher can skip and move on.

    Slippage on limit fills is opt-in (``SlippageConfig.apply_to_limits``).
    Default behavior fills at the supplied ``fill_price``.

    Partial fills: ``fill_qty`` (if provided and < remaining qty) marks
    the order as ``PARTIALLY_FILLED`` and updates ``filled_qty`` rather
    than transitioning to ``FILLED``.
    """
    adjusted_price = apply_slippage(
        ctx.slippage_config,
        side=order.side,
        order_type=OrderType.LIMIT,
        last_price=fill_price,
        limit_price=order.limit_price,
        symbol=order.symbol,
    )
    now = ctx.now_iso()
    fee_engine = ctx.fee_engine_for(now)

    remaining = order.qty - order.filled_qty
    if fill_qty is None or fill_qty >= remaining:
        slice_qty = remaining
        terminal = True
    else:
        slice_qty = fill_qty
        terminal = False

    if slice_qty <= 0:
        return

    fees = fee_engine.calculate(order.side, slice_qty, adjusted_price,
                                order.exchange)

    new_filled_qty = order.filled_qty + slice_qty
    prior_total = order.filled_qty * (order.filled_avg_price or 0.0)
    new_avg_fill = (prior_total + slice_qty * adjusted_price) / new_filled_qty
    new_status = OrderStatus.FILLED if terminal else OrderStatus.PARTIALLY_FILLED
    cumulative_fees = order.fees_paid + fees.total

    with ctx.persistence.transaction() as conn:
        cur = conn.execute(
            "UPDATE orders SET status = ?, filled_qty = ?, "
            "filled_avg_price = ?, fees_paid = ?, realized_pl = ?, "
            "filled_at = ? "
            "WHERE id = ? AND account_id = ? AND status IN (?, ?)",
            (
                new_status.value, new_filled_qty, new_avg_fill,
                cumulative_fees, 0.0,
                now if terminal else None,
                order.id, ctx.account_id,
                OrderStatus.PENDING.value, OrderStatus.PARTIALLY_FILLED.value,
            ),
        )
        if cur.rowcount == 0:
            raise OrderNoLongerPending(
                f"Order {order.id} is no longer pending; skip"
            )

        position_existed_before = (
            _state.symbol_position_qty(ctx, conn, order.symbol) > 0
        )

        if order.side == OrderSide.BUY:
            _state.apply_buy(ctx, conn, order.symbol, slice_qty, adjusted_price,
                             fees.total, now, order_id=order.id)
            slice_realized_pl = 0.0
        else:
            slice_realized_pl = _state.apply_sell(
                ctx, conn, order.symbol, slice_qty, adjusted_price,
                fees.total, now, order_id=order.id,
            )

        cumulative_realized_pl = order.realized_pl + slice_realized_pl
        if cumulative_realized_pl != 0.0:
            conn.execute(
                "UPDATE orders SET realized_pl = ? WHERE id = ? AND account_id = ?",
                (cumulative_realized_pl, order.id, ctx.account_id),
            )

        _state.record_trade(
            ctx, conn, order_id=order.id, symbol=order.symbol,
            side=order.side, qty=slice_qty, price=adjusted_price,
            fees=fees.total, realized_pl=slice_realized_pl, executed_at=now,
        )
        _maybe_settle_trade(
            ctx, conn, side=order.side, symbol=order.symbol, qty=slice_qty,
            cash_credit=slice_qty * adjusted_price - fees.total,
            product_type=order.product_type, now=now,
        )

        position_qty_after = _state.symbol_position_qty(ctx, conn, order.symbol)
        _state.emit_position_events(ctx, conn, order,
                                    position_existed_before, position_qty_after)

        if order.parent_order_id is not None:
            _state.rebalance_bracket_sibling_qty(ctx, conn, order, new_filled_qty)
            if terminal:
                _state.cancel_bracket_siblings(ctx, conn, order)

        if terminal:
            ctx.emit(conn, event_type="order_filled", order_id=order.id,
                     payload={"symbol": order.symbol, "side": order.side.value,
                              "qty": new_filled_qty, "fill_price": adjusted_price,
                              "fees_paid": cumulative_fees})
        else:
            ctx.emit(conn, event_type="order_partially_filled", order_id=order.id,
                     payload={"symbol": order.symbol, "side": order.side.value,
                              "slice_qty": slice_qty, "filled_qty": new_filled_qty,
                              "remaining_qty": order.qty - new_filled_qty,
                              "slice_price": adjusted_price})

    ctx.drain_pending_events()

    if terminal:
        logger.info("LIMIT FILL %s %s %s @ ₹%.2f (limit was ₹%.2f)",
                    order.side.value.upper(), order.qty, order.symbol,
                    adjusted_price, order.limit_price)
    else:
        logger.info("LIMIT PARTIAL %s %s/%s %s @ ₹%.2f (limit was ₹%.2f)",
                    order.side.value.upper(),
                    slice_qty, order.qty - new_filled_qty + slice_qty,
                    order.symbol, adjusted_price, order.limit_price)


__all__ = ["queue", "fill"]
