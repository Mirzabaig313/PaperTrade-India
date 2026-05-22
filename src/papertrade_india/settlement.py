"""T+1 settlement and intraday auto-square-off.

India moved to T+1 settlement in 2023 (full rollout by Jan 2024). The
practical effect on a retail trader:

- A SELL credits proceeds to ``cash`` on T+1, not T+0. Until then the
  cash exists but isn't withdrawable to the bank — though for most
  paper-trading purposes you can still re-buy with it.
- A BUY debits cash immediately but the shares aren't deliverable until
  T+1. You cannot sell what you don't have, even if your limit order
  filled this morning.
- INTRADAY (MIS) trades sidestep both: the broker squares off any open
  MIS position at 15:15 IST, no demat involvement, no DP charge, no
  delivery.

Real-world deviations we don't model:
- BTST (Buy Today Sell Tomorrow) using auctioned shares — too edge-case.
- T+0 settlement pilot for a small subset of scrips — opt-in to the
  pilot is per-broker.
- T+2 leftovers in BSE for a few weeks during transitions.

This module provides:

- :class:`SettlementMode` — picks the broker's settlement style.
- :class:`SettlementConfig` — one knob: ``auto_square_off_intraday_at``
  (minutes since midnight IST, 15:15 by default).
- :class:`PendingSettlement` — a single row in the queue.
- :class:`SettlementEngine` — drives the queue against the broker.

The broker hosts an instance and calls :meth:`settle_due` on each tick
and at session close. Persistence lives in a new ``pending_settlements``
table added in migration v2.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum

from .market_hours import IST

logger = logging.getLogger(__name__)


class SettlementMode(str, Enum):
    """How the simulator settles cash and shares.

    - ``T_PLUS_1``: realistic Indian settlement (default since v0.2).
      Sells produce a pending-cash row that converts to spendable cash
      on the next trading day. Buys reduce ``deliverable_qty`` until
      T+1 — you can't sell what you bought today on a delivery account.
    - ``T_PLUS_0``: legacy mode. Cash settles instantly. Use this for
      backtests that pre-date T+1 (NSE's T+1 rollout was Jan 2024) or
      when you want the simpler "money is fungible immediately" model.
    """

    T_PLUS_0 = "T+0"
    T_PLUS_1 = "T+1"


@dataclass(frozen=True)
class SettlementConfig:
    """Runtime knobs for the settlement engine.

    ``mode`` controls the cash-settlement timing. Default is
    :attr:`SettlementMode.T_PLUS_1` to match real Indian retail
    behavior — a fresh delivery buy isn't sellable the same day.
    Pass ``SettlementMode.T_PLUS_0`` for the legacy "instant
    settlement" simplification that was the v0.1.x default.

    ``auto_square_off_at`` is the wall-clock time-of-day in IST when
    intraday positions get forcibly closed (15:15 IST is the
    conventional broker default — NSE itself trades until 15:30, but
    most brokers force-close at 15:15 so any market-impact slack is
    absorbed before close).
    """

    mode: SettlementMode = SettlementMode.T_PLUS_1
    auto_square_off_at: time = time(15, 15)
    auto_square_off_enabled: bool = True


@dataclass(frozen=True)
class PendingSettlement:
    """A pending T+1 cash settlement.

    Fields
    ------
    id:
        UUID hex.
    account_id, symbol:
        Account scope and the underlying symbol.
    side:
        ``"buy"`` or ``"sell"`` — determines whether shares or cash is
        the pending side.
    qty:
        Number of shares involved (informational; cash deltas are
        already in ``account.cash``).
    cash_delta:
        Cash to credit (sells, positive) or debit (buys, usually 0
        because buys debit immediately) when the row settles.
    settle_on:
        ISO date the row becomes ``"settled"``.
    status:
        ``"pending"`` until the engine settles it.
    """

    id: str
    account_id: str
    symbol: str
    side: str
    qty: float
    cash_delta: float
    trade_date: date
    settle_on: date
    status: str
    created_at: datetime


# ── DDL ──────────────────────────────────────────────────────────────


SETTLEMENT_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS pending_settlements (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('buy','sell')),
    qty REAL NOT NULL,
    cash_delta REAL NOT NULL DEFAULT 0,
    trade_date TEXT NOT NULL,
    settle_on TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','settled','cancelled')),
    created_at TEXT NOT NULL,
    FOREIGN KEY (account_id) REFERENCES account(account_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_pending_settlements_due
    ON pending_settlements(account_id, status, settle_on);
"""


# ── Engine ───────────────────────────────────────────────────────────


class SettlementEngine:
    """Drives the T+1 queue and intraday auto-square-off.

    Stateless — all state lives in ``pending_settlements``. Inject one
    into the broker; the broker calls :meth:`enqueue_sell`,
    :meth:`enqueue_buy`, and :meth:`settle_due` at the right moments.

    Square-off is *initiated* here (we identify positions to close);
    the actual broker.sell call happens in the broker so fees, ledger,
    and events flow through the canonical execution path. Keeping the
    engine free of the broker's internals avoids a circular import and
    makes the engine unit-testable.
    """

    def __init__(self, config: SettlementConfig | None = None) -> None:
        self.config = config or SettlementConfig()

    @property
    def mode(self) -> SettlementMode:
        return self.config.mode

    # ── Enqueue ──────────────────────────────────────────────────────

    def enqueue_sell(
        self,
        conn: sqlite3.Connection,
        account_id: str,
        symbol: str,
        qty: float,
        cash_credit: float,
        trade_date: date,
        now: datetime,
    ) -> str | None:
        """Record a pending T+1 sell. Returns the row id, or ``None``
        when the engine is in T+0 mode (no row needed).

        ``cash_credit`` is the *additional* cash that will land on T+1
        beyond what's already been credited. In our default model,
        sells already credit cash on T+0 (so the user can buy back),
        and ``cash_credit`` records the bookkeeping rather than another
        movement. Callers can pass 0 when they want shares-only tracking.
        """
        if self.config.mode != SettlementMode.T_PLUS_1:
            return None
        sid = uuid.uuid4().hex[:12]
        settle_on = _next_business_day(trade_date)
        conn.execute(
            "INSERT INTO pending_settlements "
            "(id, account_id, symbol, side, qty, cash_delta, "
            "trade_date, settle_on, status, created_at) "
            "VALUES (?, ?, ?, 'sell', ?, ?, ?, ?, 'pending', ?)",
            (
                sid, account_id, symbol, qty, cash_credit,
                trade_date.isoformat(), settle_on.isoformat(),
                now.isoformat(),
            ),
        )
        return sid

    def enqueue_buy(
        self,
        conn: sqlite3.Connection,
        account_id: str,
        symbol: str,
        qty: float,
        trade_date: date,
        now: datetime,
    ) -> str | None:
        """Record a pending T+1 buy.

        Tracks ``qty`` shares as undeliverable until T+1. The broker
        uses the ``deliverable_qty(...)`` query to enforce "you can't
        sell shares you bought earlier today on a delivery account."
        Returns the row id (or ``None`` in T+0 mode).
        """
        if self.config.mode != SettlementMode.T_PLUS_1:
            return None
        sid = uuid.uuid4().hex[:12]
        settle_on = _next_business_day(trade_date)
        conn.execute(
            "INSERT INTO pending_settlements "
            "(id, account_id, symbol, side, qty, cash_delta, "
            "trade_date, settle_on, status, created_at) "
            "VALUES (?, ?, ?, 'buy', ?, 0, ?, ?, 'pending', ?)",
            (
                sid, account_id, symbol, qty,
                trade_date.isoformat(), settle_on.isoformat(),
                now.isoformat(),
            ),
        )
        return sid

    # ── Daily roll ────────────────────────────────────────────────────

    def settle_due(
        self,
        conn: sqlite3.Connection,
        account_id: str,
        as_of: date,
    ) -> int:
        """Mark all rows with ``settle_on <= as_of`` as ``settled``.

        Returns the count rolled. Cash already moved at trade time, so
        no further account.cash mutation is needed; this just retires
        the bookkeeping. For deliverable-qty tracking it's the moment
        a buy turns "free for sale."
        """
        cur = conn.execute(
            "UPDATE pending_settlements "
            "SET status = 'settled' "
            "WHERE account_id = ? AND status = 'pending' AND settle_on <= ?",
            (account_id, as_of.isoformat()),
        )
        if cur.rowcount:
            logger.info(
                "Settled %d row(s) for account=%s as_of=%s",
                cur.rowcount, account_id, as_of.isoformat(),
            )
        return cur.rowcount

    # ── Deliverable-qty query ────────────────────────────────────────

    def deliverable_qty(
        self,
        conn: sqlite3.Connection,
        account_id: str,
        symbol: str,
        position_qty: float,
        as_of: date,
    ) -> float:
        """Shares of ``symbol`` that are sellable today on a delivery
        account.

        Sellable = position - (sum of unsettled buys still in flight).
        In T+0 mode, unsettled buys is always 0, so this returns
        ``position_qty`` unchanged.
        """
        if self.config.mode != SettlementMode.T_PLUS_1:
            return position_qty
        row = conn.execute(
            "SELECT COALESCE(SUM(qty), 0) AS in_flight "
            "FROM pending_settlements "
            "WHERE account_id = ? AND symbol = ? AND side = 'buy' "
            "AND status = 'pending' AND settle_on > ?",
            (account_id, symbol, as_of.isoformat()),
        ).fetchone()
        in_flight = float(row["in_flight"]) if row else 0.0
        return max(0.0, position_qty - in_flight)

    # ── Auto square-off helper ───────────────────────────────────────

    def is_square_off_time(self, now: datetime) -> bool:
        """True when ``now`` (IST) has passed the configured square-off time."""
        if not self.config.auto_square_off_enabled:
            return False
        # Compare time-of-day in IST. ``now`` may be naive (broker uses
        # IST internally) or aware; we treat naive as IST.
        local = now.astimezone(IST) if now.tzinfo else now.replace(tzinfo=IST)
        return local.time() >= self.config.auto_square_off_at

    # ── Reads ────────────────────────────────────────────────────────

    def list_pending(
        self,
        conn: sqlite3.Connection,
        account_id: str,
        limit: int = 100,
    ) -> list[PendingSettlement]:
        """All pending rows for an account, oldest first."""
        rows = conn.execute(
            "SELECT * FROM pending_settlements "
            "WHERE account_id = ? AND status = 'pending' "
            "ORDER BY settle_on ASC, created_at ASC LIMIT ?",
            (account_id, limit),
        ).fetchall()
        return [_row_to_pending(r) for r in rows]


# ── Helpers ──────────────────────────────────────────────────────────


def _next_business_day(d: date) -> date:
    """Naive next-business-day: skip weekends.

    We deliberately don't import the NSE holiday calendar here — the
    settlement engine is a low-level component and the calendar lives
    one layer up in the broker. In practice "settle on Monday after
    a Friday trade" is the common case and weekend skipping is enough
    for paper-trading; users who care about Diwali settlement quirks
    can subclass and override.
    """
    nxt = d + timedelta(days=1)
    while nxt.weekday() >= 5:  # Sat=5, Sun=6
        nxt += timedelta(days=1)
    return nxt


def _row_to_pending(row: sqlite3.Row) -> PendingSettlement:
    return PendingSettlement(
        id=row["id"],
        account_id=row["account_id"],
        symbol=row["symbol"],
        side=row["side"],
        qty=float(row["qty"]),
        cash_delta=float(row["cash_delta"]),
        trade_date=date.fromisoformat(row["trade_date"]),
        settle_on=date.fromisoformat(row["settle_on"]),
        status=row["status"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )
