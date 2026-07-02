"""Tests for the SQLite-backed WatchlistStore (migration 004)."""

from __future__ import annotations

import pytest

from papertrade_india.infrastructure.persistence import Persistence
from papertrade_india.infrastructure.watchlist import WatchlistStore


@pytest.fixture()
def store(tmp_path) -> WatchlistStore:
    return WatchlistStore(Persistence(tmp_path / "wl.db"))


def test_empty_by_default(store: WatchlistStore) -> None:
    assert store.list_symbols() == []


def test_set_and_get_preserves_order(store: WatchlistStore) -> None:
    store.set_symbols(["TCS", "RELIANCE", "INFY"])
    assert store.list_symbols() == ["TCS", "RELIANCE", "INFY"]


def test_set_replaces(store: WatchlistStore) -> None:
    store.set_symbols(["A", "B", "C"])
    store.set_symbols(["X", "Y"])
    assert store.list_symbols() == ["X", "Y"]


def test_set_uppercases_and_dedupes(store: WatchlistStore) -> None:
    stored = store.set_symbols([" reliance ", "RELIANCE", "tcs", ""])
    assert stored == ["RELIANCE", "TCS"]
    assert store.list_symbols() == ["RELIANCE", "TCS"]


def test_add_appends_and_is_idempotent(store: WatchlistStore) -> None:
    store.set_symbols(["A"])
    store.add("b")
    store.add("B")  # dup — no-op
    assert store.list_symbols() == ["A", "B"]


def test_remove(store: WatchlistStore) -> None:
    store.set_symbols(["A", "B", "C"])
    store.remove("b")
    assert store.list_symbols() == ["A", "C"]


def test_persists_across_instances(tmp_path) -> None:
    db = tmp_path / "wl.db"
    WatchlistStore(Persistence(db)).set_symbols(["RELIANCE"])
    # New store on the same DB file sees the saved data.
    assert WatchlistStore(Persistence(db)).list_symbols() == ["RELIANCE"]


def test_broker_exposes_watchlist(tmp_path) -> None:
    from papertrade_india import quickstart

    broker = quickstart(db_path=str(tmp_path / "b.db"), symbol_master=None)
    assert broker.get_watchlist() == []
    broker.set_watchlist(["RELIANCE", "TCS"])
    assert broker.get_watchlist() == ["RELIANCE", "TCS"]
    broker.add_to_watchlist("INFY")
    broker.remove_from_watchlist("TCS")
    assert broker.get_watchlist() == ["RELIANCE", "INFY"]
