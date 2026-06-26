"""Unit tests for the settlement engine + auto-square-off logic."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, time

import pytest

from papertrade_india import (
    SettlementConfig,
    SettlementEngine,
    SettlementMode,
)
from papertrade_india.execution.settlement import (
    SETTLEMENT_TABLE_SQL,
    _next_business_day,
)


@pytest.fixture()
def conn() -> sqlite3.Connection:
    raw = sqlite3.connect(":memory:")
    raw.row_factory = sqlite3.Row
    raw.execute("""
        CREATE TABLE account (
            account_id TEXT PRIMARY KEY, cash REAL, realized_pl_total REAL,
            created_at TEXT
        )
    """)
    raw.execute(
        "INSERT INTO account VALUES (?, ?, ?, ?)",
        ("A", 100_000.0, 0.0, datetime.now().isoformat()),
    )
    for stmt in SETTLEMENT_TABLE_SQL.split(";"):
        s = stmt.strip()
        if s:
            raw.execute(s)
    return raw


def test_next_business_day_skips_weekend() -> None:
    friday = date(2026, 5, 22)
    assert _next_business_day(friday) == date(2026, 5, 25)  # Mon
    saturday = date(2026, 5, 23)
    assert _next_business_day(saturday) == date(2026, 5, 25)


def test_t_plus_0_mode_does_nothing(conn: sqlite3.Connection) -> None:
    eng = SettlementEngine(SettlementConfig(mode=SettlementMode.T_PLUS_0))
    sid = eng.enqueue_sell(
        conn, account_id="A", symbol="X", qty=10, cash_credit=1000,
        trade_date=date(2026, 5, 22), now=datetime.now(),
    )
    assert sid is None
    rows = conn.execute("SELECT COUNT(*) AS n FROM pending_settlements").fetchone()
    assert rows["n"] == 0


def test_enqueue_and_settle_t_plus_1(conn: sqlite3.Connection) -> None:
    eng = SettlementEngine(SettlementConfig(mode=SettlementMode.T_PLUS_1))
    eng.enqueue_sell(
        conn, account_id="A", symbol="X", qty=10, cash_credit=1000,
        trade_date=date(2026, 5, 22), now=datetime(2026, 5, 22, 15, 30),
    )

    pending = eng.list_pending(conn, "A")
    assert len(pending) == 1
    assert pending[0].settle_on == date(2026, 5, 25)
    assert pending[0].status == "pending"

    # Settling on T (trade date) doesn't trip the row.
    n = eng.settle_due(conn, "A", as_of=date(2026, 5, 22))
    assert n == 0

    # Settling on T+1 rolls it.
    n = eng.settle_due(conn, "A", as_of=date(2026, 5, 25))
    assert n == 1
    pending_after = eng.list_pending(conn, "A")
    assert len(pending_after) == 0


def test_deliverable_qty_subtracts_in_flight_buys(
    conn: sqlite3.Connection,
) -> None:
    eng = SettlementEngine(SettlementConfig(mode=SettlementMode.T_PLUS_1))
    # User bought 30 shares today (T+1 in flight).
    eng.enqueue_buy(
        conn, account_id="A", symbol="X", qty=30,
        trade_date=date(2026, 5, 22), now=datetime(2026, 5, 22, 10, 0),
    )
    # Also sold 10 (these are not in_flight buys, so don't subtract).
    eng.enqueue_sell(
        conn, account_id="A", symbol="X", qty=10, cash_credit=500,
        trade_date=date(2026, 5, 22), now=datetime(2026, 5, 22, 11, 0),
    )

    # Position qty (caller-supplied) = 100.
    sellable = eng.deliverable_qty(
        conn, account_id="A", symbol="X",
        position_qty=100, as_of=date(2026, 5, 22),
    )
    assert sellable == 70  # 100 - 30 in_flight


def test_deliverable_qty_t_plus_0_returns_position(
    conn: sqlite3.Connection,
) -> None:
    eng = SettlementEngine(SettlementConfig(mode=SettlementMode.T_PLUS_0))
    sellable = eng.deliverable_qty(
        conn, account_id="A", symbol="X",
        position_qty=50, as_of=date(2026, 5, 22),
    )
    assert sellable == 50


def test_is_square_off_time() -> None:
    eng = SettlementEngine(SettlementConfig(
        auto_square_off_at=time(15, 15),
        auto_square_off_enabled=True,
    ))
    assert not eng.is_square_off_time(datetime(2026, 5, 22, 15, 14))
    assert eng.is_square_off_time(datetime(2026, 5, 22, 15, 15))
    assert eng.is_square_off_time(datetime(2026, 5, 22, 15, 30))


def test_is_square_off_time_disabled() -> None:
    eng = SettlementEngine(SettlementConfig(auto_square_off_enabled=False))
    assert not eng.is_square_off_time(datetime(2026, 5, 22, 15, 30))
