"""Append-only event log.

A parallel audit stream of every domain event the broker emits. This is
NOT event-sourcing — the broker still owns its state in ``account``,
``positions``, ``orders``, etc. The event log is purely additive: a
chronological narrative of what happened, useful for replay analysis,
debugging, and post-hoc audits.

Why a separate stream from the cash ledger
------------------------------------------
- The ledger answers "where did the cash go?" (financial truth).
- The event log answers "what did the broker do, and when?" (operational
  truth — including events that don't move cash, like order placement,
  cancellations, splits, kill-switch trips).

Schema
------
Single table with a typed ``event_type``, optional ``account_id`` (some
events are not account-scoped), an ``order_id`` cross-reference, a JSON
``payload`` blob for free-form context, and a wall-clock timestamp.

Event types defined today
-------------------------
- ``order_submitted``     — an order entered the broker (any status)
- ``order_filled``        — moved to FILLED
- ``order_cancelled``     — moved to CANCELLED
- ``order_expired``       — DAY-tif sweep moved it to EXPIRED
- ``order_rejected``      — pre-trade rejection (risk, validation, market closed)
- ``position_opened``     — first share of a symbol acquired
- ``position_closed``     — last share of a symbol disposed
- ``corporate_action``    — split or dividend applied
- ``kill_switch_tripped`` — risk engine refused an order
- ``account_reset``       — broker.reset() called
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    -- Some events (e.g. corporate_action) aren't account-scoped, so
    -- this is nullable. Account-scoped events should always populate it.
    account_id TEXT,
    event_type TEXT NOT NULL,
    -- Optional cross-reference to the order this event relates to.
    order_id TEXT,
    -- JSON-encoded payload for free-form context. Keep it small —
    -- consumers query SQLite, not Mongo.
    payload TEXT NOT NULL DEFAULT '{}',
    recorded_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_account_recorded
    ON events(account_id, recorded_at DESC);

CREATE INDEX IF NOT EXISTS idx_events_type_recorded
    ON events(event_type, recorded_at DESC);

CREATE INDEX IF NOT EXISTS idx_events_order
    ON events(order_id);
"""


@dataclass(frozen=True)
class Event:
    id: str
    account_id: str | None
    event_type: str
    order_id: str | None
    payload: dict[str, Any]
    recorded_at: datetime


def emit(
    conn: sqlite3.Connection,
    event_type: str,
    recorded_at_iso: str,
    account_id: str | None = None,
    order_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> str:
    """Append one immutable event row. Returns the event id."""
    event_id = uuid.uuid4().hex[:12]
    payload_json = json.dumps(payload or {}, sort_keys=True, default=str)
    conn.execute(
        "INSERT INTO events "
        "(id, account_id, event_type, order_id, payload, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (event_id, account_id, event_type, order_id,
         payload_json, recorded_at_iso),
    )
    return event_id


def list_for_account(
    conn: sqlite3.Connection,
    account_id: str,
    limit: int = 200,
    event_types: tuple[str, ...] | None = None,
) -> list[Event]:
    """Recent events for an account, newest first.

    ``event_types`` filters by a tuple of types when provided.
    """
    if event_types:
        placeholders = ",".join("?" for _ in event_types)
        rows = conn.execute(
            f"SELECT id, account_id, event_type, order_id, payload, recorded_at "
            f"FROM events WHERE account_id = ? AND event_type IN ({placeholders}) "
            f"ORDER BY recorded_at DESC, id DESC LIMIT ?",
            (account_id, *event_types, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, account_id, event_type, order_id, payload, recorded_at "
            "FROM events WHERE account_id = ? "
            "ORDER BY recorded_at DESC, id DESC LIMIT ?",
            (account_id, limit),
        ).fetchall()
    return [
        Event(
            id=r["id"],
            account_id=r["account_id"],
            event_type=r["event_type"],
            order_id=r["order_id"],
            payload=json.loads(r["payload"]),
            recorded_at=datetime.fromisoformat(r["recorded_at"]),
        )
        for r in rows
    ]
