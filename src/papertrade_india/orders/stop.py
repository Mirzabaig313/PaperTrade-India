"""STOP_MARKET and STOP_LIMIT order lifecycle.

- :func:`queue` — persist a new PENDING stop order.
- :func:`trigger` — called by :class:`LimitOrderWatcher` when the stop
  price is crossed; converts to an immediate market fill or a pending
  limit order.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from ..domain.models import Order, OrderSide, OrderStatus, OrderType, ProductType
from . import state as _state

if TYPE_CHECKING:  # pragma: no cover
    from .._context import BrokerContext

logger = logging.getLogger(__name__)


def queue(
    ctx: "BrokerContext",
    symbol: str,
    qty: float,
    side: OrderSide,
    order_type: OrderType,
    stop_price: float,
    limit_price: float | None,
    time_in_force: str,
    product_type: ProductType = ProductType.DELIVERY,
) -> Order:
    """Queue a STOP_MARKET or STOP_LIMIT order in PENDING state.

    The watcher monitors the price feed each tick. When the stop is
    triggered (BUY: last >= stop, SELL: last <= stop), the watcher
    calls :func:`trigger`.
    """
    order_id = uuid.uuid4().hex[:12]
    now = ctx.now_iso()
    with ctx.persistence.transaction() as conn:
        _state.record(
            ctx, conn,
            order_id=order_id, symbol=symbol, side=side, qty=qty,
            order_type=order_type, status=OrderStatus.PENDING,
            filled_qty=0.0, filled_avg_price=None, limit_price=limit_price,
            fees_paid=0.0, realized_pl=0.0, time_in_force=time_in_force,
            created_at=now, filled_at=None, stop_price=stop_price,
            product_type=product_type,
        )
        ctx.emit(conn, event_type="order_submitted", order_id=order_id,
                 payload={"symbol": symbol, "side": side.value, "qty": qty,
                          "order_type": order_type.value,
                          "stop_price": stop_price, "limit_price": limit_price,
                          "product_type": product_type.value})
    ctx.drain_pending_events()
    logger.info("QUEUE STOP %s %s %s stop=₹%.2f%s",
                side.value.upper(), qty, symbol, stop_price,
                f" limit=₹{limit_price:.2f}" if limit_price else "")

    with ctx.persistence.read() as conn:
        row = conn.execute(
            "SELECT * FROM orders WHERE id = ? AND account_id = ?",
            (order_id, ctx.account_id),
        ).fetchone()
    assert row is not None
    return _state.row_to_order(row)


def trigger(ctx: "BrokerContext", order: Order, last_price: float) -> None:
    """Convert a triggered STOP into a market or limit fill.

    Called by :class:`LimitOrderWatcher`. STOP_MARKET fires immediately
    at last; STOP_LIMIT becomes a regular pending LIMIT and waits for
    the watcher's next pass to fill.

    Race-safe: the row's status flips PENDING → working under an
    UPDATE-with-WHERE-status guard. If we lose the race (concurrent
    cancel), we silently skip.
    """
    from .market import fill_pending_market  # noqa: PLC0415

    now = ctx.now_iso()
    if order.order_type == OrderType.STOP_MARKET:
        with ctx.persistence.transaction() as conn:
            cur = conn.execute(
                "UPDATE orders SET triggered_at = ? "
                "WHERE id = ? AND account_id = ? AND status = ?",
                (now, order.id, ctx.account_id, OrderStatus.PENDING.value),
            )
            if cur.rowcount == 0:
                return
            ctx.emit(conn, event_type="stop_triggered", order_id=order.id,
                     payload={"stop_price": order.stop_price,
                              "trigger_price": last_price})
        ctx.drain_pending_events()
        fill_pending_market(ctx, order.id)
    else:  # STOP_LIMIT
        with ctx.persistence.transaction() as conn:
            cur = conn.execute(
                "UPDATE orders SET order_type = ?, triggered_at = ? "
                "WHERE id = ? AND account_id = ? AND status = ?",
                (
                    OrderType.LIMIT.value, now, order.id,
                    ctx.account_id, OrderStatus.PENDING.value,
                ),
            )
            if cur.rowcount == 0:
                return
            ctx.emit(conn, event_type="stop_triggered", order_id=order.id,
                     payload={"stop_price": order.stop_price,
                              "trigger_price": last_price,
                              "now_pending_limit": order.limit_price})
        ctx.drain_pending_events()
        logger.info("STOP→LIMIT %s %s @ stop=₹%.2f (limit ₹%.2f) for %s",
                    order.side.value.upper(), order.qty,
                    order.stop_price, order.limit_price, order.symbol)


__all__ = ["queue", "trigger"]
