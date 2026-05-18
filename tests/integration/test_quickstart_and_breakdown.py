"""Tests for ``quickstart()``, ``get_position_basis_breakdown()``, the
``status`` CLI command, and watcher idempotency cleanup.
"""

from __future__ import annotations

import logging

import pytest

from papertrade_india import (
    Exchange,
    LimitOrderWatcher,
    PriceFeed,
    quickstart,
)

pytestmark = pytest.mark.integration


# ── quickstart() ─────────────────────────────────────────────────────


def test_quickstart_produces_a_working_broker(tmp_path):
    """The factory's defaults are sane and the broker accepts orders."""
    # Disable market-hours so the test runs anytime; this matches what a
    # first-time user would hit immediately.
    b = quickstart(
        db_path=tmp_path / "qs.db",
        symbol_master=None,            # disable strict master for simplicity
        enforce_market_hours=False,
    )
    a = b.get_account()
    assert a.cash == 1_000_000.0
    assert a.currency == "INR"
    # Slippage is on by default.
    assert b.slippage_config.bps == 5.0


def test_quickstart_loads_bundled_nse_universe(tmp_path):
    """By default ``quickstart`` registers the bundled NSE-30 sample so
    ``buy("RELIANCE", 1)`` works under strict mode."""
    b = quickstart(
        db_path=tmp_path / "qs2.db",
        enforce_market_hours=False,
        # ReplayClock would be cleaner for this test, but quickstart
        # doesn't expose it; use the wall-clock default.
    )
    # The strict master should accept RELIANCE because it's in the sample.
    with b.persistence.read() as conn:
        e = b.symbol_master.get(conn, "RELIANCE", Exchange.NSE)
    assert e is not None and e.name == "Reliance Industries"


def test_quickstart_can_disable_symbol_master(tmp_path):
    """``symbol_master=None`` produces a lenient master (allows everything)."""
    b = quickstart(
        db_path=tmp_path / "qs3.db",
        symbol_master=None,
        enforce_market_hours=False,
    )
    assert b.symbol_master.strict is False


def test_quickstart_overrides_pass_through(tmp_path):
    b = quickstart(
        db_path=tmp_path / "qs4.db",
        initial_capital=42_000,
        slippage_bps=15.0,
        enforce_market_hours=False,
        enforce_fresh_prices=False,
        symbol_master=None,
    )
    assert b.get_account().cash == 42_000
    assert b.slippage_config.bps == 15.0
    assert b.enforce_fresh_prices is False


def test_quickstart_strict_mode_rejects_unknown_symbol(tmp_path, stub_provider):
    """Default quickstart ships strict mode + the NSE-30 sample. A
    symbol not in the sample raises SymbolNotFound."""
    from papertrade_india import SymbolNotFound

    feed = PriceFeed(providers=[stub_provider], short_cache_ttl_seconds=0)
    b = quickstart(
        db_path=tmp_path / "qs5.db",
        enforce_market_hours=False,
        # Override the auto price feed so we don't hit network.
    )
    # Patch the broker's feed (quickstart doesn't accept one).
    b.price_feed = feed
    stub_provider.set("UNKNOWN", 1000)

    with pytest.raises(SymbolNotFound):
        b.buy("UNKNOWN", 1)


# ── get_position_basis_breakdown() ───────────────────────────────────


def test_basis_breakdown_returns_none_for_unheld(broker):
    assert broker.get_position_basis_breakdown("UNHELD") is None


def test_basis_breakdown_after_single_buy(broker, stub_provider):
    """qty * avg_cost should split into ledger principal + ledger fees."""
    stub_provider.set("RELIANCE", 1000)
    order = broker.buy("RELIANCE", 5)

    bd = broker.get_position_basis_breakdown("RELIANCE")
    assert bd is not None
    assert bd["qty"] == 5
    assert bd["total_basis"] == pytest.approx(5 * 1000 + order.fees_paid, abs=0.01)
    # Buy fees should round-trip into the breakdown's fees component.
    assert bd["ledger_buy_principal"] == pytest.approx(5000.0, abs=0.01)
    assert bd["ledger_buy_fees"] == pytest.approx(order.fees_paid, abs=0.01)
    assert bd["ledger_sell_principal"] == 0.0
    # principal + fees_in_basis == total_basis (within tolerance).
    assert bd["principal"] + bd["fees_in_basis"] == pytest.approx(
        bd["total_basis"], abs=0.01
    )


def test_basis_breakdown_after_partial_sell(broker, stub_provider):
    """A partial sell drops some shares; remaining basis should still
    decompose cleanly into principal + fees."""
    stub_provider.set("RELIANCE", 1000)
    broker.buy("RELIANCE", 10)
    broker.sell("RELIANCE", 3)

    bd = broker.get_position_basis_breakdown("RELIANCE")
    assert bd is not None
    assert bd["qty"] == 7
    # Sell side now has rows.
    assert bd["ledger_sell_principal"] > 0
    assert bd["ledger_sell_fees"] > 0
    # Decomposition still holds.
    assert bd["principal"] + bd["fees_in_basis"] == pytest.approx(
        bd["total_basis"], abs=0.01
    )


def test_basis_breakdown_zero_fees_config(tmp_path, stub_provider, price_feed):
    """A zero-fee config produces fees_in_basis = 0."""
    from papertrade_india import FeeConfig, IndiaPaperBroker

    no_fee = FeeConfig(
        brokerage_flat=0, brokerage_pct=0, brokerage_max=0,
        stt_pct_buy=0, stt_pct_sell=0,
        exchange_charge_nse=0, exchange_charge_bse=0,
        gst_pct=0, sebi_charges_pct=0,
        stamp_duty_pct=0, dp_charge_per_sell=0,
    )
    stub_provider.set("RELIANCE", 1000)
    b = IndiaPaperBroker(
        initial_capital=100_000, db_path=tmp_path / "bd.db",
        account_id="bd", price_feed=price_feed, fee_config=no_fee,
        enforce_market_hours=False,
    )
    b.buy("RELIANCE", 1)
    bd = b.get_position_basis_breakdown("RELIANCE")
    assert bd is not None
    assert bd["ledger_buy_fees"] == 0.0
    assert bd["fees_in_basis"] == 0.0
    assert bd["principal"] == pytest.approx(1000.0, abs=0.01)


# ── verify_cash_invariant logging ────────────────────────────────────


def test_invariant_logs_warn_on_drift(broker, stub_provider, caplog):
    """If we manually corrupt the account cash, the next invariant check
    should both return False AND emit a structured WARN."""
    stub_provider.set("RELIANCE", 1000)
    broker.buy("RELIANCE", 1)

    # Sneak in a direct cash adjustment that bypasses the ledger.
    with broker.persistence.transaction() as conn:
        conn.execute(
            "UPDATE account SET cash = cash + 12.34 WHERE account_id = ?",
            (broker.account_id,),
        )

    with caplog.at_level(logging.WARNING, logger="papertrade_india.broker"):
        result = broker.verify_cash_invariant()

    assert result is False
    # The WARN should mention the drift magnitude and the account.
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "Cash invariant broken" in msgs
    assert broker.account_id in msgs
    assert "12.34" in msgs or "12.3" in msgs


# ── status CLI ──────────────────────────────────────────────────────


def test_cli_status_command(tmp_path, stub_provider):
    """``status`` should print all four sections (account, positions,
    ledger, events) in one go."""
    from typer.testing import CliRunner

    from papertrade_india import IndiaPaperBroker
    from papertrade_india.cli import app

    feed = PriceFeed(providers=[stub_provider], short_cache_ttl_seconds=0)
    db = tmp_path / "status.db"
    stub_provider.set("RELIANCE", 1000)
    b = IndiaPaperBroker(
        initial_capital=500_000,
        db_path=db,
        account_id="status",
        price_feed=feed,
        enforce_market_hours=False,
    )
    b.buy("RELIANCE", 2)

    runner = CliRunner(env={"COLUMNS": "200"})
    result = runner.invoke(
        app, ["status", "--db", str(db), "--account", "status"],
    )
    assert result.exit_code == 0, result.stdout
    # All sections present.
    assert "Account status" in result.stdout
    assert "Open positions" in result.stdout
    assert "Ledger" in result.stdout
    assert "Events" in result.stdout
    assert "Cash invariant holds" in result.stdout


def test_cli_status_exits_3_on_drift(tmp_path, stub_provider):
    from typer.testing import CliRunner

    from papertrade_india import IndiaPaperBroker
    from papertrade_india.cli import app

    feed = PriceFeed(providers=[stub_provider], short_cache_ttl_seconds=0)
    db = tmp_path / "drift.db"
    stub_provider.set("RELIANCE", 1000)
    b = IndiaPaperBroker(
        initial_capital=100_000,
        db_path=db,
        account_id="drift",
        price_feed=feed,
        enforce_market_hours=False,
    )
    b.buy("RELIANCE", 1)
    # Corrupt cash.
    with b.persistence.transaction() as conn:
        conn.execute(
            "UPDATE account SET cash = cash + 99.99 WHERE account_id = ?",
            ("drift",),
        )

    runner = CliRunner(env={"COLUMNS": "200"})
    result = runner.invoke(
        app, ["status", "--db", str(db), "--account", "drift"],
    )
    assert result.exit_code == 3, result.stdout
    assert "Cash invariant broken" in result.stdout


# ── Watcher idempotency cleanup ──────────────────────────────────────


def test_watcher_idempotency_cleanup_runs_periodically(broker, stub_provider):
    """Watcher with ``idempotency_cleanup_every=2`` should clean up on
    its 2nd, 4th, ... ticks."""
    stub_provider.set("RELIANCE", 1000)
    broker.buy("RELIANCE", 1, idempotency_key="watcher-1")

    watcher = LimitOrderWatcher(
        broker,
        interval_seconds=999,
        idempotency_cleanup_every=2,
        idempotency_ttl_hours=0,  # everything past TTL immediately
    )
    # First tick: no cleanup yet (count=1, 1 % 2 != 0).
    watcher.tick()
    # Second tick: cleanup fires.
    watcher.tick()

    # The key was pruned — a fresh buy with the same key is treated as new.
    broker.buy("RELIANCE", 1, idempotency_key="watcher-1")
    pos = broker.get_position("RELIANCE")
    assert pos is not None
    assert pos.qty == 2  # not 1 — replay would have returned the original


def test_watcher_idempotency_cleanup_disabled_by_default(
    broker, stub_provider,
):
    """With no ``idempotency_cleanup_every``, ticks don't touch idempotency."""
    stub_provider.set("RELIANCE", 1000)
    broker.buy("RELIANCE", 1, idempotency_key="default-1")

    watcher = LimitOrderWatcher(broker, interval_seconds=999)
    for _ in range(5):
        watcher.tick()

    # Key still active — replay returns the original order.
    o1 = broker.buy("RELIANCE", 1, idempotency_key="default-1")
    pos = broker.get_position("RELIANCE")
    assert pos.qty == 1  # replay, not a new fill
    assert o1 is not None
