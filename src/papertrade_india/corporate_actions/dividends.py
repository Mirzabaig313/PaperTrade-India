"""Cash dividend corporate action.

Credits ``amount_per_share * qty_held`` to the account's cash on the
ex-date and records a ``dividend`` row in the ledger.

Tax-aware behavior is *not* modeled: in reality, Indian dividend income
is taxable to the recipient (and TDS may apply for some holders), but
that's beyond the scope of a paper simulator.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..infrastructure import ledger as _ledger
from . import store as _store

if TYPE_CHECKING:  # pragma: no cover
    from .._context import BrokerContext

logger = logging.getLogger(__name__)


def apply(
    ctx: "BrokerContext",
    symbol: str,
    amount_per_share: float,
    ex_date: str | None = None,
    notes: str | None = None,
) -> str:
    """Apply a cash dividend to the broker's holding.

    Returns the action id. Calling twice on the same dividend
    double-credits — wrap in your own dedup if needed.
    """
    if amount_per_share <= 0:
        raise ValueError("amount_per_share must be positive")
    ex_date = ex_date or ctx.clock.now().date().isoformat()
    now = ctx.now_iso()

    with ctx.persistence.transaction() as conn:
        action_id = _store.record_dividend(
            conn,
            symbol=symbol,
            exchange=ctx.default_exchange.value,
            amount_per_share=amount_per_share,
            ex_date=ex_date,
            applied_at_iso=now,
            notes=notes,
        )
        row = conn.execute(
            "SELECT qty FROM positions "
            "WHERE account_id = ? AND symbol = ?",
            (ctx.account_id, symbol),
        ).fetchone()
        if row is None or row["qty"] <= 0:
            logger.info(
                "DIVIDEND %s ₹%.4f/sh recorded; no holding to credit",
                symbol, amount_per_share,
            )
            return action_id

        credit = row["qty"] * amount_per_share
        conn.execute(
            "UPDATE account SET cash = cash + ? WHERE account_id = ?",
            (credit, ctx.account_id),
        )
        _ledger.record(
            conn,
            account_id=ctx.account_id,
            amount=credit,
            reason="dividend",
            recorded_at_iso=now,
            symbol=symbol,
            notes=f"Dividend ₹{amount_per_share}/sh × {row['qty']:g}",
        )
        ctx.emit(
            conn,
            event_type="corporate_action",
            payload={
                "type": "dividend",
                "symbol": symbol,
                "per_share": amount_per_share,
                "qty": row["qty"],
                "credit": credit,
                "ex_date": ex_date,
            },
        )

    ctx.drain_pending_events()
    logger.info(
        "DIVIDEND %s ₹%.4f/sh credited ₹%.2f to %s",
        symbol, amount_per_share, credit, ctx.account_id,
    )
    return action_id


__all__ = ["apply"]
