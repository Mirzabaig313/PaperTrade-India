"""Thread-safe SQLite persistence layer.

Why this looks the way it does:

- **Thread-local connections.** SQLite connections aren't safe to share
  across threads by default. We give each thread its own.
- **WAL journal mode.** Allows concurrent readers + one writer without
  blocking, which matters for the limit-order watcher running alongside
  the main broker thread.
- **Manual transactions.** ``isolation_level=None`` disables Python's
  implicit-transaction wrapping. The :meth:`Persistence.transaction`
  context manager wraps multi-statement operations atomically, with
  rollback on any exception.
- **Foreign keys ON.** Enforced explicitly because SQLite's default is
  off, even on modern versions.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

PathLike = str | Path


SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS account (
    account_id TEXT PRIMARY KEY,
    -- Cash invariant: never below zero. The broker's _apply_buy() guards
    -- against overdraft pre-update, so this is a defensive belt-and-braces.
    cash REAL NOT NULL CHECK(cash >= 0),
    realized_pl_total REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    account_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    exchange TEXT NOT NULL,
    qty REAL NOT NULL CHECK(qty >= 0),
    -- avg_cost is the per-share economic cost basis INCLUDING prorated
    -- buy-side fees. So qty*avg_cost == total cash spent acquiring the
    -- position. See broker._apply_buy for the maintenance invariant.
    avg_cost REAL NOT NULL CHECK(avg_cost > 0),
    entry_date TEXT NOT NULL,
    PRIMARY KEY (account_id, symbol),
    FOREIGN KEY (account_id) REFERENCES account(account_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    exchange TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('buy','sell')),
    qty REAL NOT NULL CHECK(qty > 0),
    order_type TEXT NOT NULL CHECK(order_type IN ('market','limit')),
    -- Match the OrderStatus enum values exactly; CHECK keeps stale code
    -- from inserting a bogus status.
    status TEXT NOT NULL CHECK(status IN (
        'pending','filled','partially_filled',
        'cancelled','rejected','expired'
    )),
    filled_qty REAL NOT NULL DEFAULT 0,
    filled_avg_price REAL,
    limit_price REAL,
    fees_paid REAL NOT NULL DEFAULT 0,
    realized_pl REAL NOT NULL DEFAULT 0,
    time_in_force TEXT NOT NULL DEFAULT 'DAY',
    created_at TEXT NOT NULL,
    filled_at TEXT,
    cancelled_at TEXT,
    expired_at TEXT,
    rejection_reason TEXT,
    FOREIGN KEY (account_id) REFERENCES account(account_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_orders_account_status
    ON orders(account_id, status);
CREATE INDEX IF NOT EXISTS idx_orders_account_created
    ON orders(account_id, created_at DESC);

CREATE TABLE IF NOT EXISTS trades (
    id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    price REAL NOT NULL,
    fees REAL NOT NULL DEFAULT 0,
    realized_pl REAL NOT NULL DEFAULT 0,
    executed_at TEXT NOT NULL,
    -- ON DELETE CASCADE so reset() can drop orders without first having
    -- to delete trades manually.
    FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE,
    FOREIGN KEY (account_id) REFERENCES account(account_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_trades_account_executed
    ON trades(account_id, executed_at DESC);
"""


class Persistence:
    """Thread-safe SQLite wrapper.

    One connection per thread (sqlite3 isn't safe to share). WAL mode
    allows concurrent readers + one writer.

    The :meth:`transaction` context manager wraps multi-statement
    operations so partial failures don't corrupt account state.
    """

    def __init__(self, db_path: PathLike) -> None:
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        # Initialize schema on first connection.
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        """Return the thread-local connection, creating it lazily."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                self.db_path,
                isolation_level=None,  # Manual transaction control
                check_same_thread=True,
            )
            conn.row_factory = sqlite3.Row
            # Each thread needs its own foreign-key/WAL settings.
            conn.execute("PRAGMA foreign_keys = ON")
            self._local.conn = conn
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Atomic multi-statement transaction.

        Usage::

            with persistence.transaction() as conn:
                conn.execute("UPDATE account SET cash = cash - ? ...", ...)
                conn.execute("INSERT INTO orders ...", ...)
                # commits on exit; rolls back on exception
        """
        conn = self._connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """Read-only operations don't need explicit transactions."""
        yield self._connect()

    def close(self) -> None:
        """Close this thread's connection. Subsequent calls re-open it lazily.

        Note: this only closes the *calling* thread's connection. Other
        thread-local connections remain open until their threads exit.
        For long-lived daemon threads (e.g. ``LimitOrderWatcher``) that's
        the desired behavior; for short-lived workers, call ``close()``
        from inside the worker before it exits.
        """
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
