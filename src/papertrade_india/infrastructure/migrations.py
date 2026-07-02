"""Schema migrations.

Why this module exists
----------------------
Pre-1.0, the package's first persistence layer ran ``executescript(SCHEMA)``
with ``CREATE TABLE IF NOT EXISTS`` on every connect. That's fine for
brand-new DBs but breaks the moment the schema *changes*: a 0.1.0 user
who upgrades to 0.2.0 won't get the new columns or indexes, because
``IF NOT EXISTS`` is a no-op when the table already exists. Versioned
migrations fix this.

Design
------
- Each migration is a function decorated with ``@migration(N)``. ``N`` is
  a positive integer; migrations apply in numeric order.
- A single ``schema_version`` table tracks the highest applied version.
  Brand-new DBs see no table; we create it and start from version 0.
- ``run_migrations(conn)`` applies all migrations strictly greater than
  the recorded version, in a transaction per migration.
- A migration that raises rolls back its own transaction; the package
  refuses to continue at a half-applied version.
- Forward-only: there is no downgrade path. Reverting deployments rarely
  needs DB rollback in practice, and offering it is a footgun.

Detecting legacy DBs (pre-versioning)
-------------------------------------
A 0.1.x DB has all the v1 tables (``account``, ``positions``, ``orders``,
etc.) but no ``schema_version`` row. We detect this and stamp it as
version 1 without re-running migration 001 — its DDL is ``IF NOT EXISTS``
so re-running would be safe, but the stamp is faster and tells future
maintainers what shape the DB started from.

Adding a new migration
----------------------
1. Add a ``@migration(N+1)`` function below the latest one.
2. Use ``ALTER TABLE`` / ``CREATE INDEX`` / DML, not ``CREATE TABLE``.
3. Add a test in ``tests/integration/test_migrations.py`` that
   synthesizes a v(N) DB and verifies the upgrade.
4. Update ``CHANGELOG.md`` with the migration's purpose.

The runner can be invoked from the CLI: ``papertrade-india migrate``.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from typing import TypeVar

logger = logging.getLogger(__name__)

# Registry: version → migration function. Populated by the @migration decorator.
MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {}


F = TypeVar("F", bound=Callable[[sqlite3.Connection], None])


def migration(version: int) -> Callable[[F], F]:
    """Register ``fn`` as the migration for ``version``.

    Versions are positive integers; gaps are not allowed (1, 2, 3, …).
    Re-registering an existing version raises ``ValueError`` immediately
    — duplicate migration numbers in two PRs would silently overwrite,
    which is exactly the kind of bug versioning exists to catch.
    """

    def decorator(fn: F) -> F:
        if version <= 0:
            raise ValueError(f"migration version must be positive, got {version}")
        if version in MIGRATIONS:
            raise ValueError(
                f"migration {version} is already registered "
                f"(by {MIGRATIONS[version].__qualname__})"
            )
        MIGRATIONS[version] = fn
        return fn

    return decorator


# ── Migration 001: initial schema ──────────────────────────────────────


@migration(1)
def _v1_initial_schema(conn: sqlite3.Connection) -> None:
    """The package's day-one schema: account, positions, orders, trades,
    cash_movements, events, idempotency_keys, symbols, corporate_actions.

    Brand-new DBs run this in full. Legacy 0.1.x DBs already have these
    tables (the legacy detector stamps them at v1 without re-running),
    but the DDL is ``IF NOT EXISTS`` everywhere so re-running would be
    safe.

    We execute statements one at a time rather than ``executescript``
    because executescript silently issues COMMIT, which fights with
    our own transaction wrapping in ``run_migrations``.
    """
    for stmt in _split_statements(_INITIAL_SCHEMA_SQL):
        conn.execute(stmt)


# ── Migration 002: microstructure + settlement + new order columns ──


@migration(2)
def _v2_realism_extensions(conn: sqlite3.Connection) -> None:
    """Add the columns and tables needed for tick/lot/band rules,
    T+1 settlement, and the new order types (STOP, BRACKET, INTRADAY).

    Strategy
    --------
    Every change is additive at the data level — no rows lose meaning:

    - ``symbols.tick_size`` / ``daily_band_pct``: NULL means "use
      MicrostructureConfig defaults". Existing rows stay valid.
    - ``orders``: rebuilt to widen the ``order_type`` CHECK and add
      ``stop_price`` / ``target_price`` / ``parent_order_id`` /
      ``triggered_at`` / ``product_type``. Rebuild is necessary because
      SQLite can't ``ALTER TABLE ... DROP/REPLACE CONSTRAINT``. We use
      the canonical 12-step rebuild: create new, copy, drop, rename.
    - ``pending_settlements``: brand-new table, empty initially.

    Rebuild safety
    --------------
    Foreign keys are deferred during the rebuild via
    ``PRAGMA defer_foreign_keys=1`` so the in-flight rename doesn't
    blow up cross-table FK references (``trades.order_id``,
    ``cash_movements.order_id``, etc.). FK enforcement is restored at
    the end. The whole thing runs inside the migration transaction so
    a crash mid-rebuild rolls back cleanly.
    """
    # --- symbols: tick + band (additive) -------------------------------------
    conn.execute("ALTER TABLE symbols ADD COLUMN tick_size REAL")
    conn.execute("ALTER TABLE symbols ADD COLUMN daily_band_pct REAL")

    # --- orders: rebuild with widened CHECK + new columns -------------------
    conn.execute("PRAGMA defer_foreign_keys=ON")

    conn.execute(
        """
        CREATE TABLE orders_new (
            id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            exchange TEXT NOT NULL,
            side TEXT NOT NULL CHECK(side IN ('buy','sell')),
            qty REAL NOT NULL CHECK(qty > 0),
            order_type TEXT NOT NULL CHECK(order_type IN (
                'market','limit','stop_market','stop_limit','bracket'
            )),
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
            stop_price REAL,
            target_price REAL,
            parent_order_id TEXT,
            triggered_at TEXT,
            product_type TEXT NOT NULL DEFAULT 'delivery'
                CHECK(product_type IN ('delivery','intraday')),
            FOREIGN KEY (account_id) REFERENCES account(account_id)
                ON DELETE CASCADE
        )
        """,
    )
    conn.execute(
        """
        INSERT INTO orders_new (
            id, account_id, symbol, exchange, side, qty, order_type, status,
            filled_qty, filled_avg_price, limit_price, fees_paid, realized_pl,
            time_in_force, created_at, filled_at, cancelled_at, expired_at,
            rejection_reason
        )
        SELECT
            id, account_id, symbol, exchange, side, qty, order_type, status,
            filled_qty, filled_avg_price, limit_price, fees_paid, realized_pl,
            time_in_force, created_at, filled_at, cancelled_at, expired_at,
            rejection_reason
        FROM orders
        """,
    )
    conn.execute("DROP TABLE orders")
    conn.execute("ALTER TABLE orders_new RENAME TO orders")

    # Recreate the v1 indexes (rebuild dropped them with the table).
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_orders_account_status "
        "ON orders(account_id, status)",
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_orders_account_created "
        "ON orders(account_id, created_at DESC)",
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_orders_parent "
        "ON orders(parent_order_id)",
    )

    # --- pending settlements (new table) -------------------------------------
    for stmt in _split_statements(_SETTLEMENT_SCHEMA_SQL):
        conn.execute(stmt)

    # FK enforcement restored automatically at COMMIT.


# ── Migration 003: bonus / rights as first-class corporate-action types ──


@migration(3)
def _v3_bonus_and_rights(conn: sqlite3.Connection) -> None:
    """Widen ``corporate_actions.action_type`` to include 'bonus' and
    'rights' (and 'merger', 'spinoff' for forward compat).

    Why a rebuild rather than ALTER + new check
    -------------------------------------------
    SQLite can't drop or relax a CHECK constraint on an existing column
    via ALTER. We rebuild the table with the wider domain and copy the
    data, same as v2 did for ``orders``.

    Existing rows with ``action_type='split'`` or ``'dividend'`` keep
    their meaning. Bonus issues that were previously routed through
    ``apply_split`` are *not* retroactively reclassified — that would
    rewrite history. Going forward, callers should use
    :meth:`IndiaPaperBroker.apply_bonus` and
    :meth:`IndiaPaperBroker.apply_rights` to record the proper type.
    """
    conn.execute("PRAGMA defer_foreign_keys=ON")
    conn.execute(
        """
        CREATE TABLE corporate_actions_new (
            id TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            exchange TEXT NOT NULL,
            action_type TEXT NOT NULL CHECK(action_type IN (
                'split', 'dividend', 'bonus', 'rights', 'merger', 'spinoff'
            )),
            ratio_num INTEGER,
            ratio_den INTEGER,
            amount_per_share REAL,
            ex_date TEXT NOT NULL,
            notes TEXT,
            applied_at TEXT NOT NULL
        )
        """,
    )
    conn.execute(
        """
        INSERT INTO corporate_actions_new
        SELECT id, symbol, exchange, action_type, ratio_num, ratio_den,
               amount_per_share, ex_date, notes, applied_at
        FROM corporate_actions
        """,
    )
    conn.execute("DROP TABLE corporate_actions")
    conn.execute("ALTER TABLE corporate_actions_new RENAME TO corporate_actions")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_corporate_actions_symbol_date "
        "ON corporate_actions(symbol, ex_date)",
    )


# ── Migration 004: UI watchlist ────────────────────────────────────────


@migration(4)
def _v4_watchlist(conn: sqlite3.Connection) -> None:
    """A simple, ordered favorites list for the UI.

    Global (not per-account) — it's a display convenience, not trading
    state, matching the tool's single-user design. ``position`` keeps the
    user's ordering; ``symbol`` is the primary key so re-adding is a
    no-op/upsert.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS watchlist (
            symbol TEXT PRIMARY KEY,
            position INTEGER NOT NULL DEFAULT 0,
            added_at TEXT NOT NULL
        )
        """,
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_watchlist_position "
        "ON watchlist(position)",
    )


# ── Public API ─────────────────────────────────────────────────────────


def current_version() -> int:
    """The highest registered migration number. Brand-new DBs target this."""
    return max(MIGRATIONS) if MIGRATIONS else 0


def applied_version(conn: sqlite3.Connection) -> int:
    """The highest migration recorded in ``schema_version``.

    Returns 0 if the table doesn't exist (or is empty).
    """
    try:
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM schema_version"
        ).fetchone()
    except sqlite3.OperationalError:
        # Table doesn't exist — pre-versioning DB.
        return 0
    return int(row["v"]) if row else 0


def run_migrations(conn: sqlite3.Connection) -> list[int]:
    """Apply all pending migrations to ``conn``.

    Returns the list of versions that were applied (empty if up-to-date).
    Raises ``RuntimeError`` if the DB reports a version higher than the
    package knows about — that means a newer client wrote to it and an
    older one is reading. Refusing to operate is safer than corrupting
    the data.
    """
    _ensure_version_table(conn)

    # Legacy detection: tables exist but no version row. Stamp at v1.
    # If the legacy DB happens to be missing tables that v1 introduced
    # (truly old fragments, or the ``_synthesize_legacy_v1`` test
    # fixture which only seeds the detector keys), run v1's idempotent
    # body to fill the gaps before stamping. v1's DDL is all
    # ``CREATE TABLE IF NOT EXISTS``, so re-running is safe.
    if applied_version(conn) == 0 and _looks_like_legacy_v1(conn):
        logger.info(
            "Detected legacy v0.1.x database (tables present, no schema_version "
            "row). Backfilling missing v1 tables and stamping as schema_version=1.",
        )
        for stmt in _split_statements(_INITIAL_SCHEMA_SQL):
            conn.execute(stmt)
        _stamp_version(conn, 1)

    applied = applied_version(conn)
    target = current_version()

    if applied > target:
        raise RuntimeError(
            f"Database is at schema version {applied} but this client only "
            f"knows up to version {target}. Refusing to operate — "
            f"upgrade the papertrade-india package."
        )

    pending = sorted(v for v in MIGRATIONS if v > applied)
    if not pending:
        return []

    for v in pending:
        logger.info("Applying migration %d → %s", v, MIGRATIONS[v].__qualname__)
        # Each migration runs in its own IMMEDIATE transaction so a
        # failure leaves the previous state intact.
        conn.execute("BEGIN IMMEDIATE")
        try:
            MIGRATIONS[v](conn)
            _stamp_version(conn, v)
        except Exception:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

    return pending


# ── Internals ──────────────────────────────────────────────────────────


def _ensure_version_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)


def _split_statements(sql: str) -> list[str]:
    """Split a multi-statement SQL string into individual statements.

    SQLite's ``executescript`` runs them but auto-commits, which we
    can't tolerate inside our own transaction wrapping. ``execute``
    only accepts one statement at a time, so we split on ``;`` and
    skip blanks/comments.

    We deliberately don't try to handle SQL strings that contain
    semicolons inside string literals — none of our DDL has that.
    """
    out: list[str] = []
    for raw in sql.split(";"):
        s = raw.strip()
        if not s:
            continue
        # Drop SQL line comments. We never embed them inside multi-line
        # statements, so this is safe.
        cleaned = "\n".join(
            line for line in s.splitlines()
            if not line.lstrip().startswith("--")
        ).strip()
        if cleaned:
            out.append(cleaned)
    return out


def _stamp_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO schema_version (version) VALUES (?)",
        (version,),
    )


def _looks_like_legacy_v1(conn: sqlite3.Connection) -> bool:
    """Return True if the DB has v1's tables but no recorded version.

    Heuristic: the presence of ``account`` and ``positions`` is enough.
    A pristine new DB has neither; a v1 DB has both. This avoids
    false-positive stamps on totally empty files.
    """
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name IN ('account', 'positions')"
    ).fetchall()
    return len(rows) == 2


# ── v1 schema body ─────────────────────────────────────────────────────
#
# Kept in one big string so future-archaeologists can read the day-one
# schema in one place. Subsequent migrations should NOT modify this
# string — they should be ALTER statements in their own functions.

_INITIAL_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS account (
    account_id TEXT PRIMARY KEY,
    cash REAL NOT NULL CHECK(cash >= 0),
    realized_pl_total REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    account_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    exchange TEXT NOT NULL,
    qty REAL NOT NULL CHECK(qty >= 0),
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
    FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE,
    FOREIGN KEY (account_id) REFERENCES account(account_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_trades_account_executed
    ON trades(account_id, executed_at DESC);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    account_id TEXT NOT NULL,
    key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    order_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (account_id, key),
    FOREIGN KEY (account_id) REFERENCES account(account_id) ON DELETE CASCADE,
    FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_idempotency_created
    ON idempotency_keys(created_at);

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

CREATE TABLE IF NOT EXISTS cash_movements (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    amount REAL NOT NULL,
    reason TEXT NOT NULL CHECK(reason IN (
        'buy_principal', 'buy_fees',
        'sell_principal', 'sell_fees',
        'dividend', 'adjustment', 'initial_capital'
    )),
    order_id TEXT,
    symbol TEXT,
    notes TEXT,
    recorded_at TEXT NOT NULL,
    FOREIGN KEY (account_id) REFERENCES account(account_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_cash_movements_account_recorded
    ON cash_movements(account_id, recorded_at DESC);

CREATE INDEX IF NOT EXISTS idx_cash_movements_order
    ON cash_movements(order_id);

CREATE TABLE IF NOT EXISTS corporate_actions (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    exchange TEXT NOT NULL,
    action_type TEXT NOT NULL CHECK(action_type IN ('split', 'dividend')),
    ratio_num INTEGER,
    ratio_den INTEGER,
    amount_per_share REAL,
    ex_date TEXT NOT NULL,
    notes TEXT,
    applied_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_corporate_actions_symbol_date
    ON corporate_actions(symbol, ex_date);

CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    account_id TEXT,
    event_type TEXT NOT NULL,
    order_id TEXT,
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


# ── v2 schema additions ──────────────────────────────────────────────


_SETTLEMENT_SCHEMA_SQL = """
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
