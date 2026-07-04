"""Rights issue corporate action.

The user opts in via ``subscribe=True``. When subscribing, cash is
debited at ``subscription_price`` per new share, the position grows,
and the average cost is adjusted to preserve the round-trip P&L
invariant.
"""

from __future__ import annotations

import logging
from fractions import Fraction
from typing import TYPE_CHECKING

from ..domain.exceptions import InsufficientFundsError
from ..infrastructure import ledger as _ledger
from . import store as _store

if TYPE_CHECKING:  # pragma: no cover
    from .._context import BrokerContext

logger = logging.getLogger(__name__)


def apply(
    ctx: BrokerContext,
    symbol: str,
    ratio_num: int,
    ratio_den: int,
    subscription_price: float,
    subscribe: bool = False,
    ex_date: str | None = None,
    notes: str | None = None,
) -> str:
    """Record a rights issue and optionally subscribe.

    Rights issue mechanics: holders receive a *right* (not an
    obligation) to buy ``ratio_num`` new shares per ``ratio_den`` held,
    at ``subscription_price`` per share — typically below market. The
    user must opt in by passing ``subscribe=True``; otherwise the rights
    lapse (no position change, no cash debit).

    When ``subscribe=True``:

    - ``new_shares = floor(qty_held * ratio_num / ratio_den)``
    - Cash is debited by ``new_shares * subscription_price``.
    - Position grows: new ``qty = old_qty + new_shares``,
      ``avg_cost`` recomputed so total basis = old basis +
      subscription cost (preserves the round-trip P&L invariant).
    - Insufficient cash raises :class:`InsufficientFundsError`.

    Returns the action id whether or not the user subscribes.
    """
    if ratio_num <= 0 or ratio_den <= 0:
        raise ValueError("ratio components must be positive integers")
    if subscription_price <= 0:
        raise ValueError("subscription_price must be positive")
    rights_ratio = Fraction(ratio_num, ratio_den)
    ex_date = ex_date or ctx.clock.now().date().isoformat()
    now = ctx.now_iso()

    with ctx.persistence.transaction() as conn:
        action_id = _store.record_rights(
            conn,
            symbol=symbol,
            exchange=ctx.default_exchange.value,
            ratio=rights_ratio,
            subscription_price=subscription_price,
            ex_date=ex_date,
            applied_at_iso=now,
            notes=notes,
        )
        existing = conn.execute(
            "SELECT qty, avg_cost FROM positions "
            "WHERE account_id = ? AND symbol = ?",
            (ctx.account_id, symbol),
        ).fetchone()

        if not subscribe or existing is None:
            logger.info(
                "RIGHTS %s %d:%d recorded; subscribe=%s, holding=%s",
                symbol, ratio_num, ratio_den, subscribe,
                "none" if existing is None else f"{existing['qty']:g}",
            )
            ctx.emit(
                conn,
                event_type="corporate_action",
                payload={
                    "type": "rights",
                    "symbol": symbol,
                    "ratio_num": ratio_num,
                    "ratio_den": ratio_den,
                    "subscription_price": subscription_price,
                    "subscribed": False,
                    "ex_date": ex_date,
                },
            )
            ctx.drain_pending_events()
            return action_id

        # Subscribe path: integer-floor the entitlement, charge cash,
        # add shares.
        old_qty = existing["qty"]
        old_avg = existing["avg_cost"]
        new_shares = int(old_qty * ratio_num // ratio_den)
        if new_shares <= 0:
            logger.info(
                "RIGHTS %s %d:%d subscribe=True but entitlement rounded to zero",
                symbol, ratio_num, ratio_den,
            )
            ctx.emit(
                conn,
                event_type="corporate_action",
                payload={
                    "type": "rights",
                    "symbol": symbol,
                    "ratio_num": ratio_num,
                    "ratio_den": ratio_den,
                    "subscription_price": subscription_price,
                    "subscribed": True,
                    "new_shares": 0,
                    "ex_date": ex_date,
                },
            )
            ctx.drain_pending_events()
            return action_id

        cost = new_shares * subscription_price
        cash = conn.execute(
            "SELECT cash FROM account WHERE account_id = ?",
            (ctx.account_id,),
        ).fetchone()["cash"]
        if cost > cash:
            raise InsufficientFundsError(
                f"Rights subscription needs ₹{cost:,.2f}, have ₹{cash:,.2f}",
            )

        conn.execute(
            "UPDATE account SET cash = cash - ? WHERE account_id = ?",
            (cost, ctx.account_id),
        )
        _ledger.record(
            conn,
            account_id=ctx.account_id,
            amount=-cost,
            reason="adjustment",
            recorded_at_iso=now,
            symbol=symbol,
            notes=(
                f"Rights subscription: {new_shares} sh × "
                f"₹{subscription_price:.2f}"
            ),
        )

        new_qty = old_qty + new_shares
        new_basis = old_qty * old_avg + cost
        new_avg = new_basis / new_qty
        conn.execute(
            "UPDATE positions SET qty = ?, avg_cost = ? "
            "WHERE account_id = ? AND symbol = ?",
            (new_qty, new_avg, ctx.account_id, symbol),
        )
        ctx.emit(
            conn,
            event_type="corporate_action",
            payload={
                "type": "rights",
                "symbol": symbol,
                "ratio_num": ratio_num,
                "ratio_den": ratio_den,
                "subscription_price": subscription_price,
                "subscribed": True,
                "new_shares": new_shares,
                "cost": cost,
                "ex_date": ex_date,
            },
        )

    ctx.drain_pending_events()
    logger.info(
        "RIGHTS %s %d:%d subscribed: +%d sh @ ₹%.2f (₹%.2f total) "
        "-> %g sh avg ₹%.4f",
        symbol, ratio_num, ratio_den, new_shares, subscription_price,
        cost, new_qty, new_avg,
    )
    return action_id


__all__ = ["apply"]
