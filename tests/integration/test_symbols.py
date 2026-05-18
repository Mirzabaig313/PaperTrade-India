"""Tests for the symbol master + broker integration."""

from __future__ import annotations

from pathlib import Path

import pytest

from papertrade_india import (
    Exchange,
    IndiaPaperBroker,
    SymbolDelisted,
    SymbolMaster,
    SymbolNotFound,
)

pytestmark = pytest.mark.integration


# ── SymbolMaster in isolation ─────────────────────────────────────────


def test_upsert_and_get(broker):
    sm = SymbolMaster()
    with broker.persistence.transaction() as conn:
        sm.upsert(conn, "FOO", Exchange.NSE, name="Foo Corp", lot_size=5)
    with broker.persistence.read() as conn:
        e = sm.get(conn, "FOO", Exchange.NSE)
    assert e is not None
    assert e.symbol == "FOO"
    assert e.name == "Foo Corp"
    assert e.lot_size == 5
    assert e.delisted_at is None


def test_upsert_overwrites(broker):
    sm = SymbolMaster()
    with broker.persistence.transaction() as conn:
        sm.upsert(conn, "BAR", Exchange.NSE, name="v1")
        sm.upsert(conn, "BAR", Exchange.NSE, name="v2")
    with broker.persistence.read() as conn:
        e = sm.get(conn, "BAR", Exchange.NSE)
    assert e.name == "v2"


def test_delist_and_relist(broker):
    sm = SymbolMaster()
    with broker.persistence.transaction() as conn:
        sm.upsert(conn, "BAZ", Exchange.NSE)
        assert sm.delist(conn, "BAZ", Exchange.NSE) is True
    with broker.persistence.read() as conn:
        e = sm.get(conn, "BAZ", Exchange.NSE)
    assert e.delisted_at is not None

    with broker.persistence.transaction() as conn:
        assert sm.relist(conn, "BAZ", Exchange.NSE) is True
    with broker.persistence.read() as conn:
        assert sm.get(conn, "BAZ", Exchange.NSE).delisted_at is None


def test_delist_idempotent(broker):
    sm = SymbolMaster()
    with broker.persistence.transaction() as conn:
        sm.upsert(conn, "QUX", Exchange.NSE)
        assert sm.delist(conn, "QUX", Exchange.NSE) is True
        # Second call: no row updated (already delisted).
        assert sm.delist(conn, "QUX", Exchange.NSE) is False


def test_list_all_excludes_delisted_by_default(broker):
    sm = SymbolMaster()
    with broker.persistence.transaction() as conn:
        sm.upsert(conn, "ACTIVE", Exchange.NSE)
        sm.upsert(conn, "DEAD", Exchange.NSE)
        sm.delist(conn, "DEAD", Exchange.NSE)
    with broker.persistence.read() as conn:
        active = sm.list_all(conn, include_delisted=False)
        all_ = sm.list_all(conn, include_delisted=True)
    assert {e.symbol for e in active} == {"ACTIVE"}
    assert {e.symbol for e in all_} == {"ACTIVE", "DEAD"}


# ── Validate behavior ─────────────────────────────────────────────────


def test_lenient_mode_passes_unknown(broker):
    sm = SymbolMaster(strict=False)
    with broker.persistence.read() as conn:
        sm.validate(conn, "NOTREGISTERED", Exchange.NSE)  # no exception


def test_strict_mode_rejects_unknown(broker):
    sm = SymbolMaster(strict=True)
    with broker.persistence.read() as conn, pytest.raises(SymbolNotFound):
        sm.validate(conn, "NOTREGISTERED", Exchange.NSE)


def test_delisted_always_rejected(broker):
    sm = SymbolMaster(strict=False)
    with broker.persistence.transaction() as conn:
        sm.upsert(conn, "OLD", Exchange.NSE)
        sm.delist(conn, "OLD", Exchange.NSE)
    with broker.persistence.read() as conn, pytest.raises(SymbolDelisted):
        sm.validate(conn, "OLD", Exchange.NSE)


# ── Broker integration ───────────────────────────────────────────────


def test_broker_rejects_delisted_symbol(broker, stub_provider):
    """A delisted symbol can't be bought, even in lenient mode."""
    sm = broker.symbol_master
    with broker.persistence.transaction() as conn:
        sm.upsert(conn, "DEAD", Exchange.NSE)
        sm.delist(conn, "DEAD", Exchange.NSE)
    stub_provider.set("DEAD", 100)
    with pytest.raises(SymbolDelisted):
        broker.buy("DEAD", 1)


def test_broker_strict_mode_rejects_unknown(tmp_path, price_feed, stub_provider):
    """A broker with ``SymbolMaster(strict=True)`` requires registration first."""
    stub_provider.set("RELIANCE", 1000)
    broker = IndiaPaperBroker(
        initial_capital=1_000_000,
        db_path=tmp_path / "strict.db",
        account_id="strict",
        price_feed=price_feed,
        symbol_master=SymbolMaster(strict=True),
        enforce_market_hours=False,
    )
    with pytest.raises(SymbolNotFound):
        broker.buy("RELIANCE", 1)


def test_broker_strict_mode_works_after_registration(
    tmp_path, price_feed, stub_provider,
):
    stub_provider.set("RELIANCE", 1000)
    broker = IndiaPaperBroker(
        initial_capital=1_000_000,
        db_path=tmp_path / "strict_ok.db",
        account_id="strict",
        price_feed=price_feed,
        symbol_master=SymbolMaster(strict=True),
        enforce_market_hours=False,
    )
    with broker.persistence.transaction() as conn:
        broker.symbol_master.upsert(conn, "RELIANCE", Exchange.NSE)
    broker.buy("RELIANCE", 1)  # passes


# ── CSV bulk-load ────────────────────────────────────────────────────


def test_load_csv(tmp_path, broker):
    csv_path = tmp_path / "u.csv"
    csv_path.write_text("symbol,name,lot_size\nFOO,Foo Inc,1\nBAR,Bar Ltd,5\n")

    sm = SymbolMaster()
    with broker.persistence.transaction() as conn:
        n = sm.load_csv(conn, csv_path, Exchange.NSE)
    assert n == 2

    with broker.persistence.read() as conn:
        foo = sm.get(conn, "FOO", Exchange.NSE)
        bar = sm.get(conn, "BAR", Exchange.NSE)
    assert foo.name == "Foo Inc"
    assert bar.lot_size == 5


def test_load_bundled_nse_universe_sample(broker):
    """The shipped sample CSV loads cleanly and registers known symbols."""
    csv_path = (
        Path(__file__).resolve().parents[2]
        / "src" / "papertrade_india" / "data" / "nse_universe_sample.csv"
    )
    assert csv_path.exists(), f"sample missing at {csv_path}"

    sm = SymbolMaster()
    with broker.persistence.transaction() as conn:
        n = sm.load_csv(conn, csv_path, Exchange.NSE)
    assert n >= 25  # at least most of the largest 30

    with broker.persistence.read() as conn:
        e = sm.get(conn, "RELIANCE", Exchange.NSE)
    assert e is not None
    assert e.name == "Reliance Industries"
