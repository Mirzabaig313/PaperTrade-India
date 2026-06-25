"""Read-only views over the account: cash, equity, ledger, events.

Like :mod:`reads.positions`, every helper takes the broker as its first
argument and never mutates state.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..domain.models import Account, OrderSide, OrderStatus, OrderType
from ..infrastructure import events as _events
from ..infrastructure import ledger as _ledger
from . import positions as _positions

if TYPE_CHECKING:  # pragma: no cover
    from ..broker import IndiaPaperBroker
    from ..infrastructure.market_hours import SessionPhase

logger = logging.getLogger(__name__)


def summary(broker: "IndiaPaperBroker") -> Account:
    """Build an :class:`Account` snapshot.

    Note on consistency: cash/realized_pl_total and pending-buy notional
    are read from the same connection inside one read context to keep
    the snapshot tight. Mark-to-market on positions still calls out to
    the price feed (network-volatile), so the ``equity`` and
    ``unrealized_pl_total`` fields can drift slightly if a fill lands
    mid-call. Acceptable for paper-trading reads.
    """
    with broker.persistence.read() as conn:
        acct_row = conn.execute(
            "SELECT cash, realized_pl_total FROM account "
            "WHERE account_id = ?",
            (broker.account_id,),
        ).fetchone()
        pending_buys = conn.execute(
            "SELECT COALESCE(SUM(qty * limit_price), 0) AS notional "
            "FROM orders WHERE account_id = ? AND status = ? "
            "AND side = ? AND order_type = ?",
            (
                broker.account_id, OrderStatus.PENDING.value,
                OrderSide.BUY.value, OrderType.LIMIT.value,
            ),
        ).fetchone()["notional"]

    cash = acct_row["cash"]
    realized = acct_row["realized_pl_total"]
    open_positions = _positions.list_all(broker)
    portfolio_value = sum(p.market_value for p in open_positions)
    unrealized = sum(p.unrealized_pl for p in open_positions)

    return Account(
        account_id=broker.account_id,
        equity=cash + portfolio_value,
        cash=cash,
        portfolio_value=portfolio_value,
        buying_power=max(0.0, cash - pending_buys),
        realized_pl_total=realized,
        unrealized_pl_total=unrealized,
        currency="INR",
    )


def list_cash_movements(
    broker: "IndiaPaperBroker", limit: int = 200,
) -> list[_ledger.CashMovement]:
    """Recent cash-ledger rows for this account, newest first."""
    with broker.persistence.read() as conn:
        return _ledger.list_for_account(conn, broker.account_id, limit=limit)


def verify_cash_invariant(
    broker: "IndiaPaperBroker", tolerance: float = 0.01,
) -> bool:
    """Assert ``account.cash == sum(cash_movements.amount)``.

    Returns ``True`` if the invariant holds within ``tolerance``
    (₹0.01 absolute by default — paise rounding only).

    Run this from tests, audits, or a periodic health check. A False
    result means there's a code path mutating ``account.cash`` without
    writing a matching ledger row, which is a bug.

    On drift (returning False) we also log a structured WARN with the
    magnitude and the most recent ledger rows so the failure is
    debuggable in the wild without needing to re-run with debug flags.
    """
    with broker.persistence.read() as conn:
        cash = conn.execute(
            "SELECT cash FROM account WHERE account_id = ?",
            (broker.account_id,),
        ).fetchone()["cash"]
        ledger_total = _ledger.sum_for_account(conn, broker.account_id)

    drift = cash - ledger_total
    if abs(drift) <= tolerance:
        return True

    recent = list_cash_movements(broker, limit=5)
    logger.warning(
        "Cash invariant broken on account=%s: cash=%.4f, "
        "sum(movements)=%.4f, drift=%.4f. Recent movements: %s",
        broker.account_id, cash, ledger_total, drift,
        [(m.recorded_at.isoformat(), m.reason, m.amount) for m in recent],
    )
    return False


def list_events(
    broker: "IndiaPaperBroker",
    limit: int = 200,
    event_types: tuple[str, ...] | None = None,
) -> list[_events.Event]:
    """Recent events for this account, newest first.

    ``event_types`` filters by types when provided, e.g.
    ``event_types=("order_filled", "order_partially_filled")``.
    """
    with broker.persistence.read() as conn:
        return _events.list_for_account(
            conn, broker.account_id, limit=limit, event_types=event_types,
        )


def current_session_phase(broker: "IndiaPaperBroker") -> "SessionPhase":
    """The active NSE session phase at the broker's clock."""
    return broker.calendar.current_phase(broker.clock.now())


__all__ = [
    "summary",
    "list_cash_movements",
    "verify_cash_invariant",
    "list_events",
    "current_session_phase",
]
