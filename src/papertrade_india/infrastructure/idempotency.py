"""Idempotency-key tracking.

Backend convention (per the global steering rules): mutating operations
should accept an idempotency key, replay the same response on repeat
delivery, and reject mismatched parameters under the same key.

Storage model
-------------
Per (account_id, key) we store:

- The originating order id (so replays return the same ``Order``).
- A stable hash of the request parameters (so we can detect mismatched
  replays — that's a client bug we want to surface, not silently allow).
- A creation timestamp; rows are pruned by ``cleanup_expired`` per a TTL
  (default 24h, matching common backend conventions).

Key scope is per-account to prevent cross-client collisions. Same key
across two different accounts is allowed.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from .market_hours import IST

SCHEMA = """
CREATE TABLE IF NOT EXISTS idempotency_keys (
    account_id TEXT NOT NULL,
    key TEXT NOT NULL,
    -- SHA-256 hex of canonical request params, so a replay with
    -- different params raises IdempotencyConflict instead of silently
    -- returning the first response.
    request_hash TEXT NOT NULL,
    order_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (account_id, key),
    FOREIGN KEY (account_id) REFERENCES account(account_id) ON DELETE CASCADE,
    FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_idempotency_created
    ON idempotency_keys(created_at);
"""


@dataclass(frozen=True)
class IdempotencyEntry:
    account_id: str
    key: str
    request_hash: str
    order_id: str
    created_at: datetime


def hash_request(side: str, symbol: str, qty: float,
                 order_type: str, limit_price: float | None,
                 time_in_force: str) -> str:
    """Stable hash of the request parameters.

    Order matters and floats are formatted to a fixed precision so trivial
    representation drift (e.g. ``2500.0`` vs ``2500``) doesn't cause
    spurious conflicts.
    """
    parts = (
        side,
        symbol,
        f"{qty:.10g}",
        order_type,
        f"{limit_price:.10g}" if limit_price is not None else "-",
        time_in_force,
    )
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def lookup(
    conn: sqlite3.Connection,
    account_id: str,
    key: str,
) -> IdempotencyEntry | None:
    row = conn.execute(
        "SELECT account_id, key, request_hash, order_id, created_at "
        "FROM idempotency_keys WHERE account_id = ? AND key = ?",
        (account_id, key),
    ).fetchone()
    if row is None:
        return None
    return IdempotencyEntry(
        account_id=row["account_id"],
        key=row["key"],
        request_hash=row["request_hash"],
        order_id=row["order_id"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def store(
    conn: sqlite3.Connection,
    account_id: str,
    key: str,
    request_hash: str,
    order_id: str,
    now_iso: str,
) -> None:
    conn.execute(
        "INSERT INTO idempotency_keys "
        "(account_id, key, request_hash, order_id, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (account_id, key, request_hash, order_id, now_iso),
    )


def cleanup_expired(
    conn: sqlite3.Connection,
    ttl: timedelta = timedelta(hours=24),
    now: datetime | None = None,
) -> int:
    """Delete idempotency rows older than ``ttl``. Returns count deleted.

    Idempotency keys past their TTL are no longer protected — a replay
    with the same key will be treated as a fresh request. Backend
    convention is 24–48h; we default to 24h.
    """
    now = now or datetime.now(IST)
    cutoff = (now - ttl).isoformat()
    cur = conn.execute(
        "DELETE FROM idempotency_keys WHERE created_at < ?",
        (cutoff,),
    )
    return cur.rowcount
