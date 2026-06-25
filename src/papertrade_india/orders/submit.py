"""Order submission pipeline.

:func:`submit_order` is the single entry point for ``buy()`` and
``sell()``. It validates, checks risk and microstructure, enforces
session-phase rules, and dispatches to the right execution module.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..domain.exceptions import (
    AMOWindowClosedError,
    IdempotencyConflict,
    InvalidOrderError,
    MarginNotSupported,
    MarketClosedError,
    RandomBrokerRejection,
)
from ..domain.models import Order, OrderSide, OrderStatus, OrderType, ProductType
from ..domain.rules.risk import RiskContext
from ..domain.rules.tick_lot_band import validate_band, validate_lot, validate_tick
from ..infrastructure import idempotency as _idempotency
from ..infrastructure.market_hours import SessionPhase
from . import amo as _amo
from . import bracket as _bracket
from . import limit as _limit
from . import market as _market
from . import state as _state
from . import stop as _stop
from .book_helpers import symbol_microstructure

if TYPE_CHECKING:  # pragma: no cover
    from .._context import BrokerContext

logger = logging.getLogger(__name__)

_QTY_EPSILON = 1e-9


def submit_order(
    ctx: "BrokerContext",
    symbol: str,
    qty: float,
    side: OrderSide,
    order_type: OrderType,
    limit_price: float | None,
    time_in_force: str,
    idempotency_key: str | None = None,
    stop_price: float | None = None,
    target_price: float | None = None,
    product_type: ProductType = ProductType.DELIVERY,
) -> Order:
    """Validate, check, and dispatch an order.

    Validation chain (in order):
    1. Basic parameter guards (qty, product_type, price presence)
    2. Latency + rejection simulation
    3. Idempotency replay
    4. Symbol master validation
    5. Microstructure (tick / lot / band)
    6. Risk engine
    7. TIF + session-phase rules
    8. T+1 deliverable-qty check (DELIVERY sells only)
    9. Dispatch to execution module
    10. Idempotency store
    """
    # ── 1. Basic guards ───────────────────────────────────────────────
    if qty <= 0:
        raise InvalidOrderError("qty must be positive")
    if product_type in (ProductType.MARGIN, ProductType.PLEDGE):
        raise MarginNotSupported(
            f"product_type={product_type.value} requires margin / pledge "
            "accounting which the cash-equity simulator does not model. "
            "Use ProductType.DELIVERY (T+1 cash) or "
            "ProductType.INTRADAY (same-day, auto-square-off).",
        )
    if order_type == OrderType.LIMIT and limit_price is None:
        raise InvalidOrderError("limit_price required for LIMIT orders")
    if order_type == OrderType.LIMIT and limit_price is not None and limit_price <= 0:
        raise InvalidOrderError("limit_price must be positive")
    if order_type in (OrderType.STOP_MARKET, OrderType.STOP_LIMIT):
        if stop_price is None or stop_price <= 0:
            raise InvalidOrderError("stop_price (positive) required for STOP orders")
        if order_type == OrderType.STOP_LIMIT and (
            limit_price is None or limit_price <= 0
        ):
            raise InvalidOrderError("limit_price required for STOP_LIMIT orders")
    if order_type == OrderType.BRACKET:
        if stop_price is None or target_price is None:
            raise InvalidOrderError(
                "BRACKET requires both stop_price and target_price"
            )

    # ── 2. Latency + rejection simulation ────────────────────────────
    if ctx.latency_sim.enabled:
        ctx.latency_sim.sleep()
    if ctx.reject_sim.enabled:
        scenario = ctx.reject_sim.maybe_reject()
        if scenario is not None:
            raise RandomBrokerRejection(
                f"Simulated broker rejection: {scenario.value}"
            )

    # ── 3. Idempotency replay ─────────────────────────────────────────
    if idempotency_key is not None:
        replay = _idempotency_replay(
            ctx, key=idempotency_key, side=side, symbol=symbol, qty=qty,
            order_type=order_type, limit_price=limit_price,
            time_in_force=time_in_force,
        )
        if replay is not None:
            return replay

    # ── 4. Symbol master ──────────────────────────────────────────────
    with ctx.persistence.read() as conn:
        ctx.symbol_master.validate(conn, symbol, ctx.default_exchange)

    # ── 5. Microstructure ─────────────────────────────────────────────
    _microstructure_check(ctx, symbol, qty, limit_price, stop_price, target_price)

    # ── 6. Risk ───────────────────────────────────────────────────────
    risk_price = (
        limit_price if order_type == OrderType.LIMIT and limit_price is not None
        else _safe_last_price_for_risk(ctx, symbol)
    )
    _risk_check(ctx, side, symbol, qty, risk_price)

    # ── 7. TIF + session-phase rules ──────────────────────────────────
    valid_tif = {"DAY", "GTT", "GTC", "AMO", "IOC"}
    if time_in_force not in valid_tif:
        raise InvalidOrderError(
            f"Unsupported time_in_force {time_in_force!r}. "
            f"Valid: {sorted(valid_tif)}"
        )
    if time_in_force == "IOC":
        raise InvalidOrderError(
            "time_in_force='IOC' is reserved and not yet implemented. "
            "Use 'DAY' for normal session orders."
        )

    phase = ctx.calendar.current_phase(ctx.clock.now())
    is_amo = time_in_force == "AMO"

    if is_amo and phase == SessionPhase.REGULAR:
        raise AMOWindowClosedError(
            "AMO orders cannot be submitted during the REGULAR session. "
            "Submit AMOs after market close (POST_CLOSE / CLOSED) so "
            "they queue for the next open. For an in-session order, "
            "use time_in_force='DAY'.",
        )

    market_open = phase == SessionPhase.REGULAR
    if (
        ctx.enforce_market_hours
        and not market_open
        and order_type == OrderType.MARKET
        and not is_amo
    ):
        raise MarketClosedError(
            f"Cannot fill MARKET order — current phase: {phase.value}. "
            f"Next REGULAR open: {ctx.calendar.next_open(ctx.clock.now())}. "
            f"Use time_in_force='AMO' to queue for the next session, "
            f"or a LIMIT order during PRE_OPEN.",
        )

    # ── 8. T+1 deliverable-qty check ─────────────────────────────────
    from ..execution.settlement import SettlementMode  # noqa: PLC0415

    if (
        side == OrderSide.SELL
        and product_type == ProductType.DELIVERY
        and ctx.settlement.mode == SettlementMode.T_PLUS_1
    ):
        _t_plus_1_sellable_check(ctx, symbol, qty)

    # ── 9. Dispatch ───────────────────────────────────────────────────
    if order_type == OrderType.MARKET:
        if is_amo:
            order = _amo.queue(ctx, symbol, qty, side, time_in_force, product_type)
        else:
            order = _market.execute(ctx, symbol, qty, side, time_in_force,
                                    product_type=product_type)
    elif order_type == OrderType.LIMIT:
        assert limit_price is not None
        order = _limit.queue(ctx, symbol, qty, side, limit_price, time_in_force,
                             product_type=product_type)
    elif order_type in (OrderType.STOP_MARKET, OrderType.STOP_LIMIT):
        assert stop_price is not None
        order = _stop.queue(ctx, symbol, qty, side, order_type, stop_price,
                            limit_price, time_in_force, product_type=product_type)
    else:  # BRACKET
        assert stop_price is not None and target_price is not None
        order = _bracket.queue(ctx, symbol, qty, side, limit_price, stop_price,
                               target_price, time_in_force,
                               product_type=product_type)

    # ── 10. Idempotency store ─────────────────────────────────────────
    if idempotency_key is not None:
        _idempotency_store(ctx, key=idempotency_key, order_id=order.id,
                           side=side, symbol=symbol, qty=qty,
                           order_type=order_type, limit_price=limit_price,
                           time_in_force=time_in_force)

    return order


# ── Private helpers ───────────────────────────────────────────────────


def _idempotency_replay(
    ctx: "BrokerContext",
    key: str,
    side: OrderSide,
    symbol: str,
    qty: float,
    order_type: OrderType,
    limit_price: float | None,
    time_in_force: str,
) -> Order | None:
    with ctx.persistence.read() as conn:
        entry = _idempotency.lookup(conn, ctx.account_id, key)
    if entry is None:
        return None

    rh = _idempotency.hash_request(
        side=side.value, symbol=symbol, qty=qty,
        order_type=order_type.value, limit_price=limit_price,
        time_in_force=time_in_force,
    )
    if entry.request_hash != rh:
        raise IdempotencyConflict(
            f"Idempotency key {key!r} was previously used with "
            f"different parameters. Use a fresh key for new requests."
        )

    with ctx.persistence.read() as conn:
        row = conn.execute(
            "SELECT * FROM orders WHERE id = ? AND account_id = ?",
            (entry.order_id, ctx.account_id),
        ).fetchone()
    if row is None:
        logger.warning(
            "Idempotency key %s pointed at missing order %s; replaying as new",
            key, entry.order_id,
        )
        return None
    order = _state.row_to_order(row)
    logger.debug("Idempotency replay: key=%s -> order=%s", key, order.id)
    return order


def _idempotency_store(
    ctx: "BrokerContext",
    key: str,
    order_id: str,
    side: OrderSide,
    symbol: str,
    qty: float,
    order_type: OrderType,
    limit_price: float | None,
    time_in_force: str,
) -> None:
    rh = _idempotency.hash_request(
        side=side.value, symbol=symbol, qty=qty,
        order_type=order_type.value, limit_price=limit_price,
        time_in_force=time_in_force,
    )
    with ctx.persistence.transaction() as conn:
        _idempotency.store(conn, ctx.account_id, key, rh, order_id,
                           ctx.now_iso())


def _safe_last_price_for_risk(ctx: "BrokerContext", symbol: str) -> float:
    """Best-effort price for risk-cap math. Falls back to 0.0."""
    try:
        return ctx.price_feed.get_price(symbol)
    except Exception as e:  # noqa: BLE001
        logger.debug("Risk pre-check: price unavailable for %s: %s", symbol, e)
        return 0.0


def _risk_check(
    ctx: "BrokerContext",
    side: OrderSide,
    symbol: str,
    qty: float,
    price_for_check: float,
) -> None:
    existing_qty = 0.0
    existing_avg = 0.0
    with ctx.persistence.read() as conn:
        row = conn.execute(
            "SELECT qty, avg_cost FROM positions "
            "WHERE account_id = ? AND symbol = ?",
            (ctx.account_id, symbol),
        ).fetchone()
        if row is not None:
            existing_qty = row["qty"]
            existing_avg = row["avg_cost"]
        equity_row = conn.execute(
            "SELECT cash FROM account WHERE account_id = ?",
            (ctx.account_id,),
        ).fetchone()
        cash = equity_row["cash"] if equity_row else 0.0
    equity = cash + existing_qty * existing_avg

    risk_ctx = RiskContext(
        side=side, symbol=symbol, qty=qty, price=price_for_check,
        existing_qty=existing_qty, existing_avg_cost=existing_avg, equity=equity,
    )
    try:
        ctx.risk_engine.check(risk_ctx)
    except Exception as e:
        with ctx.persistence.transaction() as conn:
            ctx.emit(conn, event_type="order_rejected",
                     payload={"symbol": symbol, "side": side.value, "qty": qty,
                              "reason": type(e).__name__, "detail": str(e)})
        ctx.drain_pending_events()
        raise


def _microstructure_check(
    ctx: "BrokerContext",
    symbol: str,
    qty: float,
    limit_price: float | None,
    stop_price: float | None,
    target_price: float | None,
) -> None:
    cfg = ctx.microstructure_config
    tick, lot, band_pct = symbol_microstructure(ctx, symbol)

    if cfg.enforce_tick_size:
        for label, p in (
            ("limit_price", limit_price),
            ("stop_price", stop_price),
            ("target_price", target_price),
        ):
            validate_tick(p, tick, label)

    if cfg.enforce_lot_size:
        validate_lot(qty, lot)

    if cfg.enforce_price_band and band_pct and band_pct > 0:
        prev_close = _safe_prev_close(ctx, symbol)
        if prev_close is not None:
            for p in (limit_price, stop_price, target_price):
                if p is not None:
                    validate_band(p, prev_close, band_pct)


def _safe_prev_close(ctx: "BrokerContext", symbol: str) -> float | None:
    try:
        mq = ctx.price_feed.get_market_quote(symbol)
    except Exception as e:  # noqa: BLE001
        logger.debug("prev_close unavailable for %s: %s", symbol, e)
        return None
    return mq.prev_close


def _t_plus_1_sellable_check(ctx: "BrokerContext", symbol: str, qty: float) -> None:
    from ..domain.exceptions import InsufficientSharesError  # noqa: PLC0415

    with ctx.persistence.read() as conn:
        row = conn.execute(
            "SELECT qty FROM positions WHERE account_id = ? AND symbol = ?",
            (ctx.account_id, symbol),
        ).fetchone()
        held = float(row["qty"]) if row else 0.0
        sellable = ctx.settlement.deliverable_qty(
            conn, account_id=ctx.account_id, symbol=symbol,
            position_qty=held, as_of=ctx.clock.now().date(),
        )
    if qty > sellable + _QTY_EPSILON:
        raise InsufficientSharesError(
            f"Only {sellable:g} share(s) of {symbol} are deliverable "
            f"today (held={held:g}, in-flight T+1 buys reduce it). "
            f"Use product_type=ProductType.INTRADAY for same-day "
            f"round-trips, or wait for settlement.",
        )


__all__ = ["submit_order"]
