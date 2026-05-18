"""Symbol master.

Tracks tradeable NSE/BSE symbols with optional metadata: long name,
exchange, lot size, ISIN, and a delisting timestamp. Two modes:

1. **Lenient (default)**: an unknown symbol is accepted; orders for
   symbols marked delisted are rejected with ``SymbolDelisted``. This is
   the right default for a paper broker — a user typing
   ``broker.buy("RELIANCE", 5)`` shouldn't have to seed a CSV first.
2. **Strict** (``strict=True`` on ``SymbolMaster``): unknown symbols
   are rejected with ``SymbolNotFound``. Use this for production-style
   deployments where you want every symbol audited.

The master is account-scope-free — symbols are global to a database
file. A sample seed CSV ships in ``data/nse_universe_sample.csv`` with
~30 of the largest Indian companies.
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .exceptions import SymbolDelisted, SymbolNotFound
from .market_hours import IST
from .models import Exchange

SCHEMA = """
CREATE TABLE IF NOT EXISTS symbols (
    symbol TEXT NOT NULL,
    exchange TEXT NOT NULL CHECK(exchange IN ('NSE','BSE')),
    name TEXT,
    isin TEXT,
    lot_size INTEGER NOT NULL DEFAULT 1 CHECK(lot_size >= 1),
    delisted_at TEXT,
    added_at TEXT NOT NULL,
    PRIMARY KEY (symbol, exchange)
);

CREATE INDEX IF NOT EXISTS idx_symbols_delisted
    ON symbols(delisted_at);
"""


@dataclass(frozen=True)
class SymbolEntry:
    symbol: str
    exchange: Exchange
    name: str | None
    isin: str | None
    lot_size: int
    delisted_at: datetime | None


class SymbolMaster:
    """Symbol master backed by the broker's SQLite file.

    Methods are designed to be called inside an open transaction (for
    writes) or a read context (for queries). The broker holds a single
    instance and threads its own connection in.
    """

    def __init__(self, strict: bool = False) -> None:
        self.strict = strict

    # ── Reads ──────────────────────────────────────────────────────────

    def get(
        self,
        conn: sqlite3.Connection,
        symbol: str,
        exchange: Exchange,
    ) -> SymbolEntry | None:
        row = conn.execute(
            "SELECT symbol, exchange, name, isin, lot_size, delisted_at "
            "FROM symbols WHERE symbol = ? AND exchange = ?",
            (symbol, exchange.value),
        ).fetchone()
        if row is None:
            return None
        return SymbolEntry(
            symbol=row["symbol"],
            exchange=Exchange(row["exchange"]),
            name=row["name"],
            isin=row["isin"],
            lot_size=row["lot_size"],
            delisted_at=(
                datetime.fromisoformat(row["delisted_at"])
                if row["delisted_at"] else None
            ),
        )

    def list_all(
        self,
        conn: sqlite3.Connection,
        include_delisted: bool = False,
    ) -> list[SymbolEntry]:
        if include_delisted:
            rows = conn.execute(
                "SELECT symbol, exchange, name, isin, lot_size, delisted_at "
                "FROM symbols ORDER BY exchange, symbol"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT symbol, exchange, name, isin, lot_size, delisted_at "
                "FROM symbols WHERE delisted_at IS NULL "
                "ORDER BY exchange, symbol"
            ).fetchall()
        return [
            SymbolEntry(
                symbol=r["symbol"],
                exchange=Exchange(r["exchange"]),
                name=r["name"],
                isin=r["isin"],
                lot_size=r["lot_size"],
                delisted_at=(
                    datetime.fromisoformat(r["delisted_at"])
                    if r["delisted_at"] else None
                ),
            )
            for r in rows
        ]

    # ── Writes ─────────────────────────────────────────────────────────

    def upsert(
        self,
        conn: sqlite3.Connection,
        symbol: str,
        exchange: Exchange,
        name: str | None = None,
        isin: str | None = None,
        lot_size: int = 1,
    ) -> None:
        """Insert or update a symbol. Clears any delisted_at flag."""
        now = datetime.now(IST).isoformat()
        conn.execute(
            "INSERT INTO symbols (symbol, exchange, name, isin, "
            "lot_size, delisted_at, added_at) "
            "VALUES (?, ?, ?, ?, ?, NULL, ?) "
            "ON CONFLICT(symbol, exchange) DO UPDATE SET "
            "name = excluded.name, isin = excluded.isin, "
            "lot_size = excluded.lot_size, delisted_at = NULL",
            (symbol, exchange.value, name, isin, lot_size, now),
        )

    def delist(
        self,
        conn: sqlite3.Connection,
        symbol: str,
        exchange: Exchange,
        when: datetime | None = None,
    ) -> bool:
        """Mark a symbol delisted. Returns True if a row was updated."""
        ts = (when or datetime.now(IST)).isoformat()
        cur = conn.execute(
            "UPDATE symbols SET delisted_at = ? "
            "WHERE symbol = ? AND exchange = ? AND delisted_at IS NULL",
            (ts, symbol, exchange.value),
        )
        return cur.rowcount > 0

    def relist(
        self,
        conn: sqlite3.Connection,
        symbol: str,
        exchange: Exchange,
    ) -> bool:
        """Clear the delisted flag. Returns True if a row was updated."""
        cur = conn.execute(
            "UPDATE symbols SET delisted_at = NULL "
            "WHERE symbol = ? AND exchange = ? AND delisted_at IS NOT NULL",
            (symbol, exchange.value),
        )
        return cur.rowcount > 0

    def load_csv(
        self,
        conn: sqlite3.Connection,
        csv_path: Path,
        exchange: Exchange,
    ) -> int:
        """Bulk-upsert from a CSV with columns: symbol[, name, isin, lot_size].

        Returns the count of rows upserted.
        """
        n = 0
        with open(csv_path, newline="") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames is None or "symbol" not in reader.fieldnames:
                raise ValueError(
                    f"CSV must have a 'symbol' column. Got: {reader.fieldnames}"
                )
            for row in reader:
                sym = (row.get("symbol") or "").strip().upper()
                if not sym:
                    continue
                self.upsert(
                    conn,
                    symbol=sym,
                    exchange=exchange,
                    name=(row.get("name") or "").strip() or None,
                    isin=(row.get("isin") or "").strip() or None,
                    lot_size=int(row.get("lot_size") or 1),
                )
                n += 1
        return n

    # ── Validation hook used by the broker ─────────────────────────────

    def validate(
        self,
        conn: sqlite3.Connection,
        symbol: str,
        exchange: Exchange,
    ) -> None:
        """Raise ``SymbolDelisted`` (always) or ``SymbolNotFound`` (strict)
        when a symbol can't be traded; return silently otherwise.
        """
        entry = self.get(conn, symbol, exchange)
        if entry is None:
            if self.strict:
                raise SymbolNotFound(
                    f"Symbol {symbol!r} on {exchange.value} not in master "
                    f"(strict mode). Add via SymbolMaster.upsert."
                )
            return
        if entry.delisted_at is not None:
            raise SymbolDelisted(
                f"Symbol {symbol!r} on {exchange.value} was delisted at "
                f"{entry.delisted_at.isoformat()}"
            )
