"""Market order execution.

A market order fills immediately at the slippage-adjusted (and
optionally book-impact-adjusted) price. This module owns the full
execution path: quote fetch → price adjustment → fee calculation →
position update → ledger → settlement → events.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from ..domain.models import Order, OrderSide, OrderStatus, OrderType, ProductType
from ..execution.slippage import apply_slippage
from . import state as _state

if TYPE_CHECKING:  # pragma: no cover
    from .._context import BrokerContext

logger = logging.getLogger(__name__)


def execute(
    ctx: "BrokerContext",
    symbol: str,
    qty: float,
    side: OrderSide,
    time_in_force: str,
    product_type: ProductType = ProductType.DELIVERY,
) -> Order:
    """Fill immediately at slippage-adjusted market price."""
    from .book_helpers import maybe_apply_book_impact  # noqa: PLC0415

    quote = _get_fill_quote(ctx, symbol)
    last_price = quote.price
    price = apply_slippage(
        ctx.slippage_config,
        side=side,
        order_type=OrderType.MARKET,
        last_price=last_price,
        symbol=symbol,
    )
    if ctx.book_sim.config.enabled:
        price = maybe_apply_book_impact(ctx, symbol=symbol, qty=qty,
                                        side=side, last_price=last_price)

    order_id = uuid.uuid4().hex[:12]
    now = ctx.now_iso()
    fee_engine = ctx.fee_engine_for(now)
    fees = fee_engine.calculate(side, qty, price, ctx.default_exchange)

    with ctx.persistence.transaction() as conn:
        position_existed_before = _state.symbol_position_qty(ctx, conn, symbol) > 0
        if side == OrderSide.BUY:
            _state.apply_buy(ctx, conn, symbol, qty, price, fees.total, now,
                             order_id=order_id)
            realized_pl = 0.0
        else:
            realized_pl = _state.apply_sell(ctx, conn, symbol, qty, price,
                                            fees.total, now, order_id=order_id)

        _state.record(
            ctx, conn,
            order_id=order_id, symbol=symbol, side=side, qty=qty,
            order_type=OrderType.MARKET, status=OrderStatus.FILLED,
            filled_qty=qty, filled_avg_price=price, limit_price=None,
            fees_paid=fees.total, realized_pl=realized_pl,
            time_in_force=time_in_force, created_at=now, filled_at=now,
            product_type=product_type,
        )
        _state.record_trade(
            ctx, conn, order_id=order_id, symbol=symbol, side=side,
            qty=qty, price=price, fees=fees.total,
            realized_pl=realized_pl, executed_at=now,
        )
        _maybe_settle_trade(ctx, conn, side=side, symbol=symbol, qty=qty,
                            cash_credit=qty * price - fees.total,
                            product_type=product_type, now=now)

        ctx.emit(conn, event_type="order_submitted", order_id=order_id,
                 payload={"symbol": symbol, "side": side.value, "qty": qty,
                          "order_type": OrderType.MARKET.value})
        ctx.emit(conn, event_type="order_filled", order_id=order_id,
                 payload={"symbol": symbol, "side": side.value, "qty": qty,
                          "fill_price": price, "fees_paid": fees.total})

        qty_after = _state.symbol_position_qty(ctx, conn, symbol)
        order_for_events = Order(
            id=order_id, symbol=symbol, exchange=ctx.default_exchange,
            side=side, qty=qty, order_type=OrderType.MARKET,
            status=OrderStatus.FILLED, filled_qty=qty,
            filled_avg_price=price, limit_price=None,
            fees_paid=fees.total, realized_pl=realized_pl,
        )
        _state.emit_position_events(ctx, conn, order_for_events,
                                    position_existed_before, qty_after)

    ctx.drain_pending_events()

    if side == OrderSide.SELL:
        logger.info("FILL %s %s %s @ ₹%.2f (fees ₹%.2f, realized P&L ₹%.2f)",
                    side.value.upper(), qty, symbol, price, fees.total, realized_pl)
    else:
        logger.info("FILL %s %s %s @ ₹%.2f (fees ₹%.2f)",
                    side.value.upper(), qty, symbol, price, fees.total)

    with ctx.persistence.read() as conn:
        row = conn.execute(
            "SELECT * FROM orders WHERE id = ? AND account_id = ?",
            (order_id, ctx.account_id),
        ).fetchone()
    assert row is not None, "order disappeared after commit"
    return _state.row_to_order(row)


def fill_pending_market(ctx: "BrokerContext", order_id: str) -> None:
    """Fill a PENDING market order (bracket parent or AMO) at current price.

    Reuses the standard execution path so fees/ledger/events flow
    through canonical code.
    """
    from .book_helpers import maybe_apply_book_impact  # noqa: PLC0415

    with ctx.persistence.read() as conn:
        row = conn.execute(
            "SELECT * FROM orders WHERE id = ? AND account_id = ?",
            (order_id, ctx.account_id),
        ).fetchone()
    if row is None:
        return
    order = _state.row_to_order(row)
    if order.status != OrderStatus.PENDING:
        return

    quote = _get_fill_quote(ctx, order.symbol)
    last_price = quote.price
    price = apply_slippage(
        ctx.slippage_config,
        side=order.side,
        order_type=OrderType.MARKET,
        last_price=last_price,
        symbol=order.symbol,
    )
    if ctx.book_sim.config.enabled:
        price = maybe_apply_book_impact(ctx, symbol=order.symbol,
                                        qty=order.qty, side=order.side,
                                        last_price=last_price)

    now = ctx.now_iso()
    fee_engine = ctx.fee_engine_for(now)
    fees = fee_engine.calculate(order.side, order.qty, price, ctx.default_exchange)

    with ctx.persistence.transaction() as conn:
        cur = conn.execute(
            "UPDATE orders SET status = ?, filled_qty = ?, "
            "filled_avg_price = ?, fees_paid = ?, filled_at = ? "
            "WHERE id = ? AND account_id = ? AND status = ?",
            (
                OrderStatus.FILLED.value, order.qty, price,
                fees.total, now, order_id, ctx.account_id,
                OrderStatus.PENDING.value,
            ),
        )
        if cur.rowcount == 0:
            return

        position_existed = _state.symbol_position_qty(ctx, conn, order.symbol) > 0
        if order.side == OrderSide.BUY:
            _state.apply_buy(ctx, conn, order.symbol, order.qty, price,
                             fees.total, now, order_id=order_id)
            realized_pl = 0.0
        else:
            realized_pl = _state.apply_sell(ctx, conn, order.symbol, order.qty,
                                            price, fees.total, now,
                                            order_id=order_id)
        if realized_pl != 0.0:
            conn.execute(
                "UPDATE orders SET realized_pl = ? WHERE id = ?",
                (realized_pl, order_id),
            )
        _state.record_trade(
            ctx, conn, order_id=order_id, symbol=order.symbol,
            side=order.side, qty=order.qty, price=price,
            fees=fees.total, realized_pl=realized_pl, executed_at=now,
        )
        _maybe_settle_trade(ctx, conn, side=order.side, symbol=order.symbol,
                            qty=order.qty,
                            cash_credit=order.qty * price - fees.total,
                            product_type=order.product_type, now=now)

        qty_after = _state.symbol_position_qty(ctx, conn, order.symbol)
        ctx.emit(conn, event_type="order_filled", order_id=order_id,
                 payload={"symbol": order.symbol, "side": order.side.value,
                          "qty": order.qty, "fill_price": price,
                          "fees_paid": fees.total,
                          "is_bracket_parent": True})
        _state.emit_position_events(ctx, conn, order, position_existed, qty_after)

        if order.parent_order_id is not None:
            _state.cancel_bracket_siblings(ctx, conn, order)

    ctx.drain_pending_events()


# ── Helpers ───────────────────────────────────────────────────────────


def _get_fill_quote(ctx: "BrokerContext", symbol: str):  # type: ignore[return]
    """Fetch a quote and apply the stale-price guard."""
    from ..domain.exceptions import StalePriceRejected  # noqa: PLC0415

    quote = ctx.price_feed.get_quote(symbol)
    if ctx.enforce_fresh_prices and quote.is_stale:
        raise StalePriceRejected(
            f"Refusing to fill {symbol} at stale cached price "
            f"₹{quote.price:.2f} (fetched {quote.fetched_at.isoformat()}). "
            f"Disable enforce_fresh_prices=False to allow stale fills."
        )
    return quote


def _maybe_settle_trade(
    ctx: "BrokerContext",
    conn,
    side: OrderSide,
    symbol: str,
    qty: float,
    cash_credit: float,
    product_type: ProductType,
    now: str,
) -> None:
    """Enqueue a T+1 row for delivery trades; no-op for intraday/T+0."""
    from ..execution.settlement import SettlementMode  # noqa: PLC0415
    from datetime import datetime as _dt  # noqa: PLC0415

    if product_type != ProductType.DELIVERY:
        return
    if ctx.settlement.mode != SettlementMode.T_PLUS_1:
        return
    trade_date = _dt.fromisoformat(now).date()
    now_dt = _dt.fromisoformat(now)
    if side == OrderSide.SELL:
        ctx.settlement.enqueue_sell(
            conn, account_id=ctx.account_id, symbol=symbol, qty=qty,
            cash_credit=cash_credit, trade_date=trade_date, now=now_dt,
        )
    else:
        ctx.settlement.enqueue_buy(
            conn, account_id=ctx.account_id, symbol=symbol, qty=qty,
            trade_date=trade_date, now=now_dt,
        )


__all__ = ["execute", "fill_pending_market"]
