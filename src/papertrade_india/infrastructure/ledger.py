"""Immutable cash-movement ledger.

Every mutation of ``account.cash`` is also recorded as an append-only row
in ``cash_movements``. ``account.cash`` itself stays as a maintained
running balance — keeping reads O(1) — but the ledger gives us full
auditability and a strict invariant we can verify any time:

    sum(amount across all movements for an account) == account.cash

Movements are immutable (no UPDATE / DELETE in normal flow). The only
non-strict path is ``broker.reset()``, which drops the entire account's
ledger together with positions and orders.

Reasons captured on each row
----------------------------
- ``buy_principal``    — cash spent on share principal (qty * price)
- ``buy_fees``         — Indian fees paid on the buy leg
- ``sell_principal``   — cash received from a sell (qty * price)
- ``sell_fees``        — Indian fees paid on the sell leg (negative)
- ``dividend``         — cash dividend received per share held
- ``adjustment``       — manual one-off adjustments (rare)
- ``initial_capital``  — opening deposit when the account was created

Splitting buy/sell into principal vs fees lets analytics distinguish
pure-economic flows from broker-imposed costs without re-deriving them
from the orders table.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS cash_movements (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    -- Positive = credit, negative = debit. cash + sum(amount) is the
    -- post-movement balance, so a buy_principal is negative.
    amount REAL NOT NULL,
    reason TEXT NOT NULL CHECK(reason IN (
        'buy_principal', 'buy_fees',
        'sell_principal', 'sell_fees',
        'dividend', 'adjustment', 'initial_capital'
    )),
    -- Optional cross-references for audit. NOT a foreign key by design:
    -- ledger writes happen alongside the cash UPDATE in _apply_buy /
    -- _apply_sell, BEFORE the orders row is inserted in the same
    -- transaction, so a hard FK would fail. Keeping this as a soft
    -- reference is fine — orders also have ON DELETE CASCADE on
    -- account_id, so they're swept together with ledger rows on reset.
    order_id TEXT,
    symbol TEXT,
    notes TEXT,
    -- Wall-clock recorded time. Multiple movements in one transaction
    -- can share a timestamp; their PK ``id`` keeps them ordered.
    recorded_at TEXT NOT NULL,
    FOREIGN KEY (account_id) REFERENCES account(account_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_cash_movements_account_recorded
    ON cash_movements(account_id, recorded_at DESC);

CREATE INDEX IF NOT EXISTS idx_cash_movements_order
    ON cash_movements(order_id);
"""


@dataclass(frozen=True)
class CashMovement:
    id: str
    account_id: str
    amount: float
    reason: str
    order_id: str | None
    symbol: str | None
    notes: str | None
    recorded_at: datetime


def record(
    conn: sqlite3.Connection,
    account_id: str,
    amount: float,
    reason: str,
    recorded_at_iso: str,
    order_id: str | None = None,
    symbol: str | None = None,
    notes: str | None = None,
) -> str:
    """Insert one immutable ledger row. Returns the generated id."""
    movement_id = uuid.uuid4().hex[:12]
    conn.execute(
        "INSERT INTO cash_movements "
        "(id, account_id, amount, reason, order_id, symbol, notes, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            movement_id, account_id, amount, reason,
            order_id, symbol, notes, recorded_at_iso,
        ),
    )
    return movement_id


def list_for_account(
    conn: sqlite3.Connection,
    account_id: str,
    limit: int = 200,
) -> list[CashMovement]:
    rows = conn.execute(
        "SELECT id, account_id, amount, reason, order_id, symbol, "
        "notes, recorded_at "
        "FROM cash_movements WHERE account_id = ? "
        "ORDER BY recorded_at DESC, id DESC LIMIT ?",
        (account_id, limit),
    ).fetchall()
    return [
        CashMovement(
            id=r["id"],
            account_id=r["account_id"],
            amount=r["amount"],
            reason=r["reason"],
            order_id=r["order_id"],
            symbol=r["symbol"],
            notes=r["notes"],
            recorded_at=datetime.fromisoformat(r["recorded_at"]),
        )
        for r in rows
    ]


def sum_for_account(conn: sqlite3.Connection, account_id: str) -> float:
    """Aggregate of all amounts for an account. Used by verification."""
    row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total "
        "FROM cash_movements WHERE account_id = ?",
        (account_id,),
    ).fetchone()
    return float(row["total"])
