"""SQLite-backed watchlist store.

A small, ordered favorites list for the UI, persisted in the broker's
own SQLite database (table ``watchlist``, migration 004) rather than a
loose JSON file — so it's atomic, concurrency-safe (via the shared
connection's ``busy_timeout``), and lives in the one datastore that the
Docker volume already persists.

Global, not per-account: it's a display convenience, matching the
single-user design.
"""

from __future__ import annotations

from datetime import datetime

from .persistence import Persistence


class WatchlistStore:
    """CRUD for the ordered symbol watchlist."""

    def __init__(self, persistence: Persistence) -> None:
        self._db = persistence

    def list_symbols(self) -> list[str]:
        """Return symbols in user order."""
        with self._db.read() as conn:
            rows = conn.execute(
                "SELECT symbol FROM watchlist ORDER BY position, symbol"
            ).fetchall()
        return [r["symbol"] for r in rows]

    def set_symbols(self, symbols: list[str]) -> list[str]:
        """Replace the entire watchlist with ``symbols`` (order preserved).

        Symbols are upper-cased and de-duplicated. Returns the cleaned
        list that was stored.
        """
        clean: list[str] = []
        for s in symbols:
            s = str(s).strip().upper()
            if s and s not in clean:
                clean.append(s)
        now = datetime.now().isoformat()
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM watchlist")
            conn.executemany(
                "INSERT INTO watchlist (symbol, position, added_at) "
                "VALUES (?, ?, ?)",
                [(sym, i, now) for i, sym in enumerate(clean)],
            )
        return clean

    def add(self, symbol: str) -> None:
        """Append one symbol (no-op if already present)."""
        sym = symbol.strip().upper()
        if not sym:
            return
        now = datetime.now().isoformat()
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(position), -1) AS m FROM watchlist"
            ).fetchone()
            conn.execute(
                "INSERT OR IGNORE INTO watchlist (symbol, position, added_at) "
                "VALUES (?, ?, ?)",
                (sym, int(row["m"]) + 1, now),
            )

    def remove(self, symbol: str) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "DELETE FROM watchlist WHERE symbol = ?", (symbol.strip().upper(),)
            )


__all__ = ["WatchlistStore"]
