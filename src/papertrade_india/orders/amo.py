"""After-Market Order (AMO) queueing and firing.

AMO market orders are persisted as PENDING with ``time_in_force='AMO'``
and fired at the next session open by :func:`fire_pending`.
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
    ctx: BrokerContext,
    symbol: str,
    qty: float,
    side: OrderSide,
    time_in_force: str,
    product_type: ProductType,
) -> Order:
    """Queue an AMO market order. Fires at next session open via
    :func:`fire_pending` (the watcher calls this automatically).

    Persisted as PENDING with ``order_type=MARKET``,
    ``time_in_force='AMO'``. Distinguishing from a regular pending
    market order is via the TIF — regular markets never persist as
    PENDING, only AMOs do.
    """
    order_id = uuid.uuid4().hex[:12]
    now = ctx.now_iso()
    with ctx.persistence.transaction() as conn:
        _state.record(
            ctx, conn,
            order_id=order_id, symbol=symbol, side=side, qty=qty,
            order_type=OrderType.MARKET, status=OrderStatus.PENDING,
            filled_qty=0.0, filled_avg_price=None, limit_price=None,
            fees_paid=0.0, realized_pl=0.0, time_in_force=time_in_force,
            created_at=now, filled_at=None, product_type=product_type,
        )
        ctx.emit(conn, event_type="order_submitted", order_id=order_id,
                 payload={"symbol": symbol, "side": side.value, "qty": qty,
                          "order_type": OrderType.MARKET.value,
                          "time_in_force": "AMO",
                          "product_type": product_type.value})
    ctx.drain_pending_events()
    logger.info("QUEUE AMO MARKET %s %s %s (fires at next open)",
                side.value.upper(), qty, symbol)

    with ctx.persistence.read() as conn:
        row = conn.execute(
            "SELECT * FROM orders WHERE id = ? AND account_id = ?",
            (order_id, ctx.account_id),
        ).fetchone()
    assert row is not None
    return _state.row_to_order(row)


def fire_pending(ctx: BrokerContext) -> int:
    """Fill every pending AMO market order at the current price.

    Idempotent within a session: orders flip from PENDING to FILLED
    atomically, so a second call after the first fires nothing.
    Intended to be called once at REGULAR-phase open by the watcher.

    Returns the count of AMO orders fired.
    """
    from .market import fill_pending_market  # noqa: PLC0415

    with ctx.persistence.read() as conn:
        rows = conn.execute(
            "SELECT id FROM orders "
            "WHERE account_id = ? AND status = ? "
            "AND order_type = ? AND time_in_force = 'AMO'",
            (ctx.account_id, OrderStatus.PENDING.value, OrderType.MARKET.value),
        ).fetchall()
    ids = [r["id"] for r in rows]
    n = 0
    for oid in ids:
        try:
            fill_pending_market(ctx, oid)
            n += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("AMO fire failed for %s: %s", oid, e)
    if n:
        logger.info("Fired %d AMO market order(s)", n)
    return n


__all__ = ["queue", "fire_pending"]
