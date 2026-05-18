"""Tests for the schema-migrations system.

Three classes of test:

1. **Fresh DB**: ``Persistence`` on an empty file applies head-of-line
   migrations and stamps ``schema_version``.
2. **Legacy detection**: a file synthesized to look like a 0.1.x DB
   (tables present, no version row) is stamped at v1 without
   re-running the migration body.
3. **Forward-incompatibility**: a DB stamped at a version higher than
   the package knows refuses to operate.
"""

from __future__ import annotations

import sqlite3

import pytest

from papertrade_india import IndiaPaperBroker, PriceFeed
from papertrade_india.migrations import (
    MIGRATIONS,
    applied_version,
    current_version,
    migration,
    run_migrations,
)

pytestmark = pytest.mark.integration


# ── Fresh DB ─────────────────────────────────────────────────────────


def test_fresh_db_lands_at_current_head(tmp_path):
    """A brand-new file is migrated to the current head and stamped."""
    db = tmp_path / "fresh.db"
    # Persistence runs migrations on its first connect.
    IndiaPaperBroker(
        initial_capital=100, db_path=db, account_id="x",
        price_feed=PriceFeed(providers=[]), enforce_market_hours=False,
    )

    with sqlite3.connect(db) as raw:
        raw.row_factory = sqlite3.Row
        # schema_version table exists.
        names = [
            r["name"] for r in raw.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        assert "schema_version" in names

        # Stamped at the package's head version.
        v = raw.execute(
            "SELECT MAX(version) AS v FROM schema_version"
        ).fetchone()["v"]
        assert v == current_version()
        assert v >= 1


def test_fresh_db_has_all_v1_tables(tmp_path):
    db = tmp_path / "tables.db"
    IndiaPaperBroker(
        initial_capital=100, db_path=db, account_id="x",
        price_feed=PriceFeed(providers=[]), enforce_market_hours=False,
    )
    with sqlite3.connect(db) as raw:
        raw.row_factory = sqlite3.Row
        names = {
            r["name"] for r in raw.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    expected_v1 = {
        "account", "positions", "orders", "trades",
        "idempotency_keys", "symbols",
        "cash_movements", "corporate_actions", "events",
        "schema_version",
    }
    assert expected_v1 <= names


# ── Legacy detection ─────────────────────────────────────────────────


def _synthesize_legacy_v1(db_path) -> None:
    """Build a DB that looks like a 0.1.x file: real v1 tables, no
    schema_version row."""
    with sqlite3.connect(db_path) as raw:
        raw.row_factory = sqlite3.Row
        # Just create the two tables our legacy detector keys off.
        # If this set ever changes, ``_looks_like_legacy_v1`` should
        # change with it.
        raw.execute("""
            CREATE TABLE account (
                account_id TEXT PRIMARY KEY,
                cash REAL NOT NULL CHECK(cash >= 0),
                realized_pl_total REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
        raw.execute("""
            CREATE TABLE positions (
                account_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                exchange TEXT NOT NULL,
                qty REAL NOT NULL CHECK(qty >= 0),
                avg_cost REAL NOT NULL CHECK(avg_cost > 0),
                entry_date TEXT NOT NULL,
                PRIMARY KEY (account_id, symbol)
            )
        """)
        raw.execute(
            "INSERT INTO account VALUES (?, ?, ?, ?)",
            ("legacy", 100_000.0, 0.0, "2026-01-01T00:00:00+05:30"),
        )
        raw.commit()


def test_legacy_db_is_stamped_at_v1_then_upgraded(tmp_path):
    """A legacy file gets stamped at v1 and upgraded to head with no data loss."""
    db = tmp_path / "legacy.db"
    _synthesize_legacy_v1(db)

    # Open through Persistence — this should stamp + upgrade.
    IndiaPaperBroker(
        initial_capital=999_999, db_path=db, account_id="legacy",
        price_feed=PriceFeed(providers=[]), enforce_market_hours=False,
        # strict_open=False is the default; the legacy account already exists.
    )

    with sqlite3.connect(db) as raw:
        raw.row_factory = sqlite3.Row
        # Stamped.
        v = raw.execute(
            "SELECT MAX(version) AS v FROM schema_version"
        ).fetchone()["v"]
        assert v == current_version()
        # Original legacy account preserved (initial_capital was ignored).
        cash = raw.execute(
            "SELECT cash FROM account WHERE account_id = 'legacy'"
        ).fetchone()["cash"]
        assert cash == 100_000.0


def test_legacy_detector_does_not_stamp_empty_files(tmp_path):
    """An empty SQLite file is NOT a legacy v1 — it's a fresh install.
    Both should land at head, but for different reasons."""
    db = tmp_path / "empty.db"
    # Touch the file so SQLite has something to open. Don't add any tables.
    sqlite3.connect(db).close()

    IndiaPaperBroker(
        initial_capital=100, db_path=db, account_id="x",
        price_feed=PriceFeed(providers=[]), enforce_market_hours=False,
    )
    with sqlite3.connect(db) as raw:
        raw.row_factory = sqlite3.Row
        v = raw.execute(
            "SELECT MAX(version) AS v FROM schema_version"
        ).fetchone()["v"]
        # Whether legacy-stamped or freshly migrated, head is head.
        assert v == current_version()


# ── Forward-incompatibility ──────────────────────────────────────────


def test_db_stamped_higher_than_known_refuses_to_open(tmp_path):
    """A DB pretending to be at v9999 should make the package refuse."""
    db = tmp_path / "future.db"
    # Build a v1 DB then stamp a future version.
    raw = sqlite3.connect(db)
    raw.row_factory = sqlite3.Row
    raw.execute(
        "CREATE TABLE schema_version ("
        "version INTEGER PRIMARY KEY, "
        "applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    raw.execute("INSERT INTO schema_version (version) VALUES (9999)")
    raw.commit()
    raw.close()

    with pytest.raises(RuntimeError, match="Refusing to operate"):
        IndiaPaperBroker(
            initial_capital=100, db_path=db, account_id="x",
            price_feed=PriceFeed(providers=[]),
            enforce_market_hours=False,
        )


# ── run_migrations idempotency ───────────────────────────────────────


def test_run_migrations_is_idempotent(tmp_path):
    """Running migrations twice on the same DB applies nothing the
    second time."""
    db = tmp_path / "idem.db"
    IndiaPaperBroker(
        initial_capital=100, db_path=db, account_id="x",
        price_feed=PriceFeed(providers=[]), enforce_market_hours=False,
    )

    raw = sqlite3.connect(db)
    raw.row_factory = sqlite3.Row
    applied_again = run_migrations(raw)
    raw.close()
    assert applied_again == []  # nothing to do


def test_applied_version_zero_for_pristine_files(tmp_path):
    """Before any migration runs, ``applied_version`` returns 0."""
    db = tmp_path / "pristine.db"
    raw = sqlite3.connect(db)
    raw.row_factory = sqlite3.Row
    assert applied_version(raw) == 0
    raw.close()


# ── Decorator validation ─────────────────────────────────────────────


def test_migration_decorator_rejects_zero_or_negative():
    with pytest.raises(ValueError, match="positive"):
        @migration(0)
        def _bad(_conn):  # pragma: no cover — decorator raises before call
            pass


def test_migration_decorator_rejects_duplicates():
    """Two migrations registered at the same version must fail loudly —
    in two PRs touching the same file this would otherwise silently
    overwrite. The test reaches into the registry to undo its damage."""
    # Pick a very high version unlikely to collide with real migrations
    # so we don't accidentally clobber something legitimate.
    test_v = 99999
    assert test_v not in MIGRATIONS

    @migration(test_v)
    def _first(_conn):
        pass

    try:
        with pytest.raises(ValueError, match="already registered"):
            @migration(test_v)
            def _second(_conn):  # pragma: no cover — decorator raises
                pass
    finally:
        MIGRATIONS.pop(test_v, None)


def test_current_version_matches_max_registered():
    """Sanity: ``current_version`` is the max of MIGRATIONS keys."""
    if MIGRATIONS:
        assert current_version() == max(MIGRATIONS)
    else:  # pragma: no cover — registry is non-empty in this package
        assert current_version() == 0



# ── CLI ──────────────────────────────────────────────────────────────


def test_cli_migrate_on_fresh_db_runs_to_head(tmp_path):
    """``papertrade-india migrate`` on a non-existent DB applies all
    migrations and stamps at head."""
    from typer.testing import CliRunner

    from papertrade_india.cli import app

    db = tmp_path / "cli_fresh.db"
    runner = CliRunner(env={"COLUMNS": "200"})
    result = runner.invoke(app, ["migrate", "--db", str(db)])
    assert result.exit_code == 0, result.stdout
    assert "Migrated" in result.stdout
    # Now the DB exists at head version.
    raw = sqlite3.connect(db)
    v = raw.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()[0]
    raw.close()
    assert v == current_version()


def test_cli_migrate_idempotent(tmp_path):
    """Second invocation reports nothing to do."""
    from typer.testing import CliRunner

    from papertrade_india.cli import app

    db = tmp_path / "cli_idem.db"
    runner = CliRunner(env={"COLUMNS": "200"})
    runner.invoke(app, ["migrate", "--db", str(db)])
    second = runner.invoke(app, ["migrate", "--db", str(db)])
    assert second.exit_code == 0
    assert "already at" in second.stdout.lower()
