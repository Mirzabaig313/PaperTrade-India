"""Stock split corporate action.

A split adjusts the holding so the total cost basis is preserved:
``new_qty = old_qty * num/den`` and ``new_avg = old_avg * den/num``.
The exchange mechanics are recorded for audit by
:func:`store.record_split`; this module owns the position adjustment.
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
    ctx: BrokerContext,
    symbol: str,
    ratio_num: int,
    ratio_den: int = 1,
    ex_date: str | None = None,
    notes: str | None = None,
) -> str:
    """Apply a stock split / bonus issue to the broker's holding.

    Parameters
    ----------
    ctx:
        Shared broker context.
    symbol:
        The split symbol.
    ratio_num, ratio_den:
        New shares per old. A 2:1 split is ``ratio_num=2``, ``ratio_den=1``
        (qty doubles, avg_cost halves). A 1:5 reverse split is
        ``ratio_num=1, ratio_den=5``.
    ex_date:
        ISO date string. Defaults to today (per the broker's clock).
    notes:
        Optional free-text annotation stored on the action row.

    Returns the action id. Idempotency is *not* enforced — calling twice
    applies the split twice. Wrap in your own dedup if needed.
    """
    if ratio_num <= 0 or ratio_den <= 0:
        raise ValueError("ratio components must be positive integers")
    ratio = Fraction(ratio_num, ratio_den)
    ex_date = ex_date or ctx.clock.now().date().isoformat()
    now = ctx.now_iso()

    with ctx.persistence.transaction() as conn:
        action_id = _store.record_split(
            conn,
            symbol=symbol,
            exchange=ctx.default_exchange.value,
            ratio=ratio,
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
                "SPLIT %s %d:%d recorded; no holding to adjust",
                symbol, ratio_num, ratio_den,
            )
            return action_id

        old_qty = existing["qty"]
        old_avg = existing["avg_cost"]
        new_qty = old_qty * (ratio_num / ratio_den)
        new_avg = old_avg * (ratio_den / ratio_num)
        conn.execute(
            "UPDATE positions SET qty = ?, avg_cost = ? "
            "WHERE account_id = ? AND symbol = ?",
            (new_qty, new_avg, ctx.account_id, symbol),
        )
        ctx.emit(
            conn,
            event_type="corporate_action",
            payload={
                "type": "split",
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
        "SPLIT %s %d:%d applied to %s: %g → %g shares (avg ₹%.4f → ₹%.4f)",
        symbol, ratio_num, ratio_den, ctx.account_id,
        old_qty, new_qty, old_avg, new_avg,
    )
    return action_id


__all__ = ["apply"]
