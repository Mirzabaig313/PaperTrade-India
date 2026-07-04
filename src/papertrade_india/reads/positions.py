"""Read-only views over positions.

Functions here never mutate state. Each takes the broker as its first
argument so they can reach the persistence handle, the price feed, and
the ``mark_to_bid`` toggle. The broker's public ``get_positions`` /
``get_position`` methods are thin delegators over these helpers.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from ..domain.models import Exchange, Position

if TYPE_CHECKING:  # pragma: no cover
    from ..broker import IndiaPaperBroker

logger = logging.getLogger(__name__)


def mark_price(broker: IndiaPaperBroker, symbol: str) -> tuple[float, str]:
    """Resolve the mark-to-market price for a long position.

    Returns ``(price, basis)``:

    - With ``broker.mark_to_bid=True`` and the rich quote exposing a
      bid, returns ``(bid, "bid")``. This matches what a real broker
      uses for unrealized P&L on a long.
    - When bid is missing but we have both bid+ask, mid is used.
    - Otherwise falls back to the legacy last-price behavior.

    Raises whatever the price feed raises so callers can decide between
    fall-back and propagation.
    """
    if broker.mark_to_bid:
        try:
            mq = broker.price_feed.get_market_quote(symbol)
        except Exception:  # noqa: BLE001
            mq = None
        if mq is not None:
            if mq.bid is not None and mq.bid > 0:
                return float(mq.bid), "bid"
            if mq.bid is not None and mq.ask is not None:
                return float((mq.bid + mq.ask) / 2.0), "mid"
    return broker.price_feed.get_price(symbol), "last"


def list_all(broker: IndiaPaperBroker) -> list[Position]:
    """Return every open position for the broker's account."""
    with broker.persistence.read() as conn:
        rows = conn.execute(
            "SELECT symbol, exchange, qty, avg_cost, entry_date "
            "FROM positions WHERE account_id = ?",
            (broker.account_id,),
        ).fetchall()

    positions: list[Position] = []
    for row in rows:
        stale = False
        basis = "last"
        try:
            price, basis = mark_price(broker, row["symbol"])
        except Exception as e:  # noqa: BLE001 — network-volatile
            logger.warning(
                "Position %s: price unavailable, using avg_cost. %s",
                row["symbol"], e,
            )
            price = row["avg_cost"]
            stale = True

        mv = price * row["qty"]
        cb = row["avg_cost"] * row["qty"]
        positions.append(
            Position(
                symbol=row["symbol"],
                exchange=Exchange(row["exchange"]),
                qty=row["qty"],
                avg_cost=row["avg_cost"],
                current_price=price,
                market_value=mv,
                cost_basis=cb,
                unrealized_pl=mv - cb,
                unrealized_pl_percent=(
                    ((mv - cb) / cb * 100) if cb > 0 else 0.0
                ),
                entry_date=datetime.fromisoformat(row["entry_date"]),
                current_price_stale=stale,
                mark_basis=basis,
            )
        )
    return positions


def get(broker: IndiaPaperBroker, symbol: str) -> Position | None:
    """Direct O(1) lookup against the (account_id, symbol) primary key."""
    with broker.persistence.read() as conn:
        row = conn.execute(
            "SELECT symbol, exchange, qty, avg_cost, entry_date "
            "FROM positions WHERE account_id = ? AND symbol = ?",
            (broker.account_id, symbol),
        ).fetchone()
    if row is None:
        return None

    stale = False
    basis = "last"
    try:
        price, basis = mark_price(broker, row["symbol"])
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "Position %s: price unavailable, using avg_cost. %s",
            row["symbol"], e,
        )
        price = row["avg_cost"]
        stale = True

    mv = price * row["qty"]
    cb = row["avg_cost"] * row["qty"]
    return Position(
        symbol=row["symbol"],
        exchange=Exchange(row["exchange"]),
        qty=row["qty"],
        avg_cost=row["avg_cost"],
        current_price=price,
        market_value=mv,
        cost_basis=cb,
        unrealized_pl=mv - cb,
        unrealized_pl_percent=(
            ((mv - cb) / cb * 100) if cb > 0 else 0.0
        ),
        entry_date=datetime.fromisoformat(row["entry_date"]),
        current_price_stale=stale,
        mark_basis=basis,
    )


def basis_breakdown(broker: IndiaPaperBroker, symbol: str) -> dict | None:
    """Return the open position's cost basis broken into principal vs fees.

    Useful for reconciling against a broker contract note: ``avg_cost``
    bakes in prorated buy-side fees, but the ledger has the raw
    components separately. We only count buy-side movements that
    haven't been reversed by a subsequent sell.

    Returns ``None`` if no open position exists for ``symbol``.
    """
    pos = get(broker, symbol)
    if pos is None:
        return None

    with broker.persistence.read() as conn:
        rows = conn.execute(
            "SELECT reason, COALESCE(SUM(amount), 0) AS total "
            "FROM cash_movements "
            "WHERE account_id = ? AND symbol = ? "
            "GROUP BY reason",
            (broker.account_id, symbol),
        ).fetchall()

    by_reason = {r["reason"]: float(r["total"]) for r in rows}
    buy_principal = abs(by_reason.get("buy_principal", 0.0))
    buy_fees = abs(by_reason.get("buy_fees", 0.0))
    sell_principal = abs(by_reason.get("sell_principal", 0.0))
    sell_fees = abs(by_reason.get("sell_fees", 0.0))

    total_basis = pos.qty * pos.avg_cost
    if buy_principal > 0:
        open_share = total_basis / max(buy_principal + buy_fees, 1e-9)
        fees_in_basis = min(buy_fees * open_share, total_basis)
    else:
        fees_in_basis = 0.0
    principal_in_basis = total_basis - fees_in_basis

    return {
        "qty": pos.qty,
        "principal": principal_in_basis,
        "fees_in_basis": fees_in_basis,
        "total_basis": total_basis,
        "ledger_buy_principal": buy_principal,
        "ledger_buy_fees": buy_fees,
        "ledger_sell_principal": sell_principal,
        "ledger_sell_fees": sell_fees,
    }


__all__ = ["list_all", "get", "mark_price", "basis_breakdown"]
