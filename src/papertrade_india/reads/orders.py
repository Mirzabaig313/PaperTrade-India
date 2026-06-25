"""Read-only views over orders.

Like the rest of :mod:`reads`, helpers take the broker as their first
argument and never mutate state. Row-to-:class:`Order` mapping lives on
the broker for now (it'll move to :mod:`orders.state` in the Phase 4
extraction); we delegate to it here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..domain.models import Order, OrderStatus

if TYPE_CHECKING:  # pragma: no cover
    from ..broker import IndiaPaperBroker


def list_all(
    broker: "IndiaPaperBroker",
    status: OrderStatus | None = None,
    limit: int = 100,
) -> list[Order]:
    """Recent orders for this account, newest first.

    ``status`` narrows the query when provided.
    """
    with broker.persistence.read() as conn:
        if status is not None:
            rows = conn.execute(
                "SELECT * FROM orders WHERE account_id = ? AND status = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (broker.account_id, status.value, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM orders WHERE account_id = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (broker.account_id, limit),
            ).fetchall()

    return [broker._row_to_order(r) for r in rows]


def get(broker: "IndiaPaperBroker", order_id: str) -> Order | None:
    """Fetch one order by id, scoped to the broker's account."""
    with broker.persistence.read() as conn:
        row = conn.execute(
            "SELECT * FROM orders WHERE id = ? AND account_id = ?",
            (order_id, broker.account_id),
        ).fetchone()
    return broker._row_to_order(row) if row else None


__all__ = ["list_all", "get"]
