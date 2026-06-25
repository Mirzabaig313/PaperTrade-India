"""Bracket order: parent entry + stop-loss child + target child.

- :func:`queue` — create the three-order bracket atomically.
- The OCO and sibling-rebalancing logic lives in :mod:`orders.state`
  (``cancel_bracket_siblings``, ``rebalance_bracket_sibling_qty``) so
  it can be called from both the limit and market fill paths.
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
    limit_price: float | None,
    stop_price: float,
    target_price: float,
    time_in_force: str,
    product_type: ProductType = ProductType.DELIVERY,
) -> Order:
    """Queue a bracket: parent entry + child SL + child target.

    Semantics
    ---------
    - Parent: MARKET (when ``limit_price is None``) or LIMIT.
    - Children are queued in PENDING but won't trigger until the parent
      fills. The watcher gates them on parent status.
    - On either child filling, the other is auto-cancelled (OCO).
    - On the parent being cancelled while still PENDING, both children
      are auto-cancelled too.

    Children are the *opposite side* of the parent: a BUY parent spawns
    SELL-stop and SELL-limit children at ``stop_price`` and
    ``target_price`` respectively.
    """
    from .market import fill_pending_market  # noqa: PLC0415

    opposite = OrderSide.SELL if side == OrderSide.BUY else OrderSide.BUY
    parent_id = uuid.uuid4().hex[:12]
    now = ctx.now_iso()

    with ctx.persistence.transaction() as conn:
        parent_type = OrderType.LIMIT if limit_price is not None else OrderType.MARKET
        _state.record(
            ctx, conn,
            order_id=parent_id, symbol=symbol, side=side, qty=qty,
            order_type=parent_type, status=OrderStatus.PENDING,
            filled_qty=0.0, filled_avg_price=None, limit_price=limit_price,
            fees_paid=0.0, realized_pl=0.0, time_in_force=time_in_force,
            created_at=now, filled_at=None, stop_price=stop_price,
            target_price=target_price, product_type=product_type,
        )
        sl_id = uuid.uuid4().hex[:12]
        _state.record(
            ctx, conn,
            order_id=sl_id, symbol=symbol, side=opposite, qty=qty,
            order_type=OrderType.STOP_MARKET, status=OrderStatus.PENDING,
            filled_qty=0.0, filled_avg_price=None, limit_price=None,
            fees_paid=0.0, realized_pl=0.0, time_in_force=time_in_force,
            created_at=now, filled_at=None, stop_price=stop_price,
            parent_order_id=parent_id, product_type=product_type,
        )
        tgt_id = uuid.uuid4().hex[:12]
        _state.record(
            ctx, conn,
            order_id=tgt_id, symbol=symbol, side=opposite, qty=qty,
            order_type=OrderType.LIMIT, status=OrderStatus.PENDING,
            filled_qty=0.0, filled_avg_price=None, limit_price=target_price,
            fees_paid=0.0, realized_pl=0.0, time_in_force=time_in_force,
            created_at=now, filled_at=None, parent_order_id=parent_id,
            product_type=product_type,
        )
        ctx.emit(conn, event_type="order_submitted", order_id=parent_id,
                 payload={"symbol": symbol, "side": side.value, "qty": qty,
                          "order_type": "bracket", "limit_price": limit_price,
                          "stop_price": stop_price, "target_price": target_price,
                          "child_sl_id": sl_id, "child_target_id": tgt_id,
                          "product_type": product_type.value})
    ctx.drain_pending_events()
    logger.info("QUEUE BRACKET %s %s %s entry=%s SL=₹%.2f TGT=₹%.2f",
                side.value.upper(), qty, symbol,
                f"₹{limit_price:.2f}" if limit_price else "MARKET",
                stop_price, target_price)

    if parent_type == OrderType.MARKET:
        try:
            fill_pending_market(ctx, parent_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("Parent bracket %s could not fill immediately: %s",
                           parent_id, e)

    with ctx.persistence.read() as conn:
        row = conn.execute(
            "SELECT * FROM orders WHERE id = ? AND account_id = ?",
            (parent_id, ctx.account_id),
        ).fetchone()
    assert row is not None
    return _state.row_to_order(row)


__all__ = ["queue"]
