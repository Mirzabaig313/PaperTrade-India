"""Bonus issue corporate action.

A bonus issue gives ``num`` extra shares per ``den`` held — economically
equivalent to a ``(num + den) : den`` split but recorded with
``action_type='bonus'`` so the audit trail stays distinct.
"""

from __future__ import annotations

import logging
from fractions import Fraction
from typing import TYPE_CHECKING

from . import store as _store

if TYPE_CHECKING:  # pragma: no cover
    from .._context import BrokerContext

logger = logging.getLogger(__name__)


def apply(
    ctx: "BrokerContext",
    symbol: str,
    ratio_num: int,
    ratio_den: int = 1,
    ex_date: str | None = None,
    notes: str | None = None,
) -> str:
    """Apply a bonus issue to the broker's holding.

    Bonus issue economics: holders receive ``ratio_num`` extra shares per
    ``ratio_den`` held. A 1:1 bonus doubles holdings (one new share per
    one held). A 1:2 bonus gives one new share for every two — total
    holding becomes 1.5x.

    Mathematically equivalent to a ``(num + den) : den`` split:
    ``new_qty = old_qty * (num + den) / den``, with avg_cost adjusted to
    preserve total cost basis.

    Returns the action id. Idempotency is *not* enforced.
    """
    if ratio_num <= 0 or ratio_den <= 0:
        raise ValueError("ratio components must be positive integers")
    bonus_ratio = Fraction(ratio_num, ratio_den)
    # Split-equivalent: shares grow by (num + den) / den.
    split_equiv = Fraction(ratio_num + ratio_den, ratio_den)
    ex_date = ex_date or ctx.clock.now().date().isoformat()
    now = ctx.now_iso()

    with ctx.persistence.transaction() as conn:
        action_id = _store.record_bonus(
            conn,
            symbol=symbol,
            exchange=ctx.default_exchange.value,
            ratio=bonus_ratio,
            ex_date=ex_date,
            applied_at_iso=now,
            notes=notes,
        )
        existing = conn.execute(
            "SELECT qty, avg_cost FROM positions "
            "WHERE account_id = ? AND symbol = ?",
            (ctx.account_id, symbol),
        ).fetchone()
        if existing is None:
            logger.info(
                "BONUS %s %d:%d recorded; no holding to adjust",
                symbol, ratio_num, ratio_den,
            )
            return action_id

        old_qty = existing["qty"]
        old_avg = existing["avg_cost"]
        new_qty = old_qty * float(split_equiv)
        new_avg = old_avg / float(split_equiv)
        conn.execute(
            "UPDATE positions SET qty = ?, avg_cost = ? "
            "WHERE account_id = ? AND symbol = ?",
            (new_qty, new_avg, ctx.account_id, symbol),
        )
        ctx.emit(
            conn,
            event_type="corporate_action",
            payload={
                "type": "bonus",
                "symbol": symbol,
                "ratio_num": ratio_num,
                "ratio_den": ratio_den,
                "ex_date": ex_date,
                "old_qty": old_qty,
                "new_qty": new_qty,
            },
        )

    ctx.drain_pending_events()
    logger.info(
        "BONUS %s %d:%d applied to %s: %g → %g shares (avg ₹%.4f → ₹%.4f)",
        symbol, ratio_num, ratio_den, ctx.account_id,
        old_qty, new_qty, old_avg, new_avg,
    )
    return action_id


__all__ = ["apply"]
