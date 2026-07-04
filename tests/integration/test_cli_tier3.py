"""Smoke tests for the observability CLI commands.

These run the CLI through Typer's test runner against a real on-disk
SQLite file under ``tmp_path``. We don't shell out — the goal is to
catch regressions in the CLI wiring itself, not Typer's argument parsing.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from papertrade_india import (
    IndiaPaperBroker,
    LatencyConfig,
    OrderBookConfig,
    PartialFillConfig,
    PriceFeed,
    RejectionConfig,
    SettlementConfig,
    SettlementMode,
    SlippageConfig,
)
from papertrade_india.cli import app

pytestmark = pytest.mark.integration


def _seed_account(tmp_path, stub_provider, account_id="t3"):
    """Seed a real broker on disk so the CLI has something to inspect."""
    feed = PriceFeed(providers=[stub_provider], short_cache_ttl_seconds=0)
    stub_provider.set("RELIANCE", 1000)
    db = tmp_path / "cli.db"
    broker = IndiaPaperBroker(
        initial_capital=100_000,
        db_path=db,
        account_id=account_id,
        price_feed=feed,
        enforce_market_hours=False,
        # CLI tests do same-day buy+sell to populate the ledger; turn
        # off realism layers so settlement, book impact, and friends
        # don't interfere with the seed data.
        order_book_config=OrderBookConfig(enabled=False),
        settlement_config=SettlementConfig(mode=SettlementMode.T_PLUS_0),
        latency_config=LatencyConfig(submit_ms_mean=0.0),
        rejection_config=RejectionConfig(rate=0.0),
        partial_fill_config=PartialFillConfig(enabled=False),
        slippage_config=SlippageConfig(bps=0.0),
        mark_to_bid=False,
        enforce_fresh_prices=False,
    )
    broker.buy("RELIANCE", 1)
    broker.sell("RELIANCE", 1)
    return db


def test_cli_ledger_command(tmp_path, stub_provider):
    db = _seed_account(tmp_path, stub_provider)
    # CliRunner defaults to ~80 cols which truncates Rich tables. Wider
    # terminal = the reasons column shows fully.
    runner = CliRunner(env={"COLUMNS": "200"})
    result = runner.invoke(
        app, ["ledger", "--db", str(db), "--account", "t3"],
    )
    assert result.exit_code == 0, result.stdout
    out = result.stdout
    # Header + at least one of each reason from the seed.
    assert "Cash ledger" in out
    assert "buy_principal" in out
    assert "sell_principal" in out
    assert "initial_capital" in out


def test_cli_verify_invariant_passes(tmp_path, stub_provider):
    db = _seed_account(tmp_path, stub_provider)
    runner = CliRunner(env={"COLUMNS": "200"})
    result = runner.invoke(
        app, ["verify-invariant", "--db", str(db), "--account", "t3"],
    )
    assert result.exit_code == 0, result.stdout
    assert "Cash invariant holds" in result.stdout


def test_cli_events_command_lists_events(tmp_path, stub_provider):
    db = _seed_account(tmp_path, stub_provider)
    runner = CliRunner(env={"COLUMNS": "200"})
    result = runner.invoke(
        app, ["events", "--db", str(db), "--account", "t3"],
    )
    assert result.exit_code == 0, result.stdout
    assert "order_filled" in result.stdout


def test_cli_events_filter_by_type(tmp_path, stub_provider):
    db = _seed_account(tmp_path, stub_provider)
    runner = CliRunner(env={"COLUMNS": "200"})
    result = runner.invoke(
        app, [
            "events", "--db", str(db), "--account", "t3",
            "--type", "order_filled",
        ],
    )
    assert result.exit_code == 0, result.stdout
    # Only order_filled rows; no order_submitted in the table.
    assert "order_filled" in result.stdout
    # The filter should exclude submitted; but the column header may
    # contain "Type", so check we don't see ``order_submitted`` rows.
    assert "order_submitted" not in result.stdout


def test_cli_phase_command(tmp_path, stub_provider):
    db = _seed_account(tmp_path, stub_provider)
    runner = CliRunner(env={"COLUMNS": "200"})
    result = runner.invoke(
        app, ["phase", "--db", str(db), "--account", "t3"],
    )
    assert result.exit_code == 0, result.stdout
    assert "Current phase" in result.stdout


def test_cli_ledger_empty_after_reset(tmp_path, stub_provider):
    """``reset`` re-seeds the ledger with one ``initial_capital`` row."""
    db = _seed_account(tmp_path, stub_provider)
    runner = CliRunner(env={"COLUMNS": "200"})
    # Reset using the CLI itself (no --capital, keep cash).
    result = runner.invoke(
        app,
        ["reset", "--db", str(db), "--account", "t3", "--yes"],
    )
    assert result.exit_code == 0, result.stdout

    # Ledger should now have exactly one row: the new initial_capital.
    result = runner.invoke(
        app, ["ledger", "--db", str(db), "--account", "t3"],
    )
    assert result.exit_code == 0
    assert "initial_capital" in result.stdout
    assert "buy_principal" not in result.stdout
