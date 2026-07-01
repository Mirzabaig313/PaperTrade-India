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
- **Versioned schema migrations.** ``migrations.run_migrations`` is
  invoked on first-thread connect. Brand-new DBs run from version 0 to
  the current head; legacy 0.1.x DBs are detected, stamped at v1, and
  upgraded forward. See ``migrations.py`` for the full design.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from . import migrations as _migrations

PathLike = str | Path


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
        # Run migrations on first connect. Brand-new DBs apply from v0
        # to head; legacy DBs are stamped + upgraded forward.
        with self._connect() as conn:
            # File-level PRAGMAs that must be set OUTSIDE any transaction
            # (SQLite refuses ``PRAGMA journal_mode`` inside one). These
            # used to live in the v1 schema string but moved out when
            # we adopted versioned migrations.
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            _migrations.run_migrations(conn)

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
            # Each thread needs its own foreign-key setting. busy_timeout
            # makes a concurrent writer WAIT for the lock (up to 5s)
            # instead of failing immediately with "database is locked" —
            # important once multiple UI sessions share one broker.
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 5000")
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


# ──────────────────────────────────────────────────────────────────────
# Backwards-compat re-exports.
#
# The pre-migrations design exposed a module-level ``SCHEMA`` string
# and an ``EXTENSION_SCHEMAS`` tuple. Some external tests or scripts
# may still touch these. They're now thin aliases over the migrations
# module so behavior is preserved.
# ──────────────────────────────────────────────────────────────────────

SCHEMA = _migrations._INITIAL_SCHEMA_SQL
EXTENSION_SCHEMAS: tuple[str, ...] = ()  # everything is in v1 now
