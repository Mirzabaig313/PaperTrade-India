"""Concurrency tests for the broker's persistence layer.

WAL mode + per-thread connections + ``BEGIN IMMEDIATE`` transactions
should keep cash and position state consistent under concurrent writes.

These tests are intentionally light-weight (~50 ops, not 50,000) so the
suite stays fast in CI; ``test_fuzz.py`` covers heavier fuzzing.
"""

from __future__ import annotations

import contextlib
import threading

import pytest

from papertrade_india import IndiaPaperBroker, InsufficientFundsError

pytestmark = pytest.mark.integration


def _run_concurrent(fn, n_threads: int = 10, ops_per_thread: int = 10) -> list[Exception]:
    """Run ``fn(i)`` from ``n_threads`` threads, ``ops_per_thread`` calls each.

    Returns the list of exceptions raised by any thread (empty if all OK).
    """
    errors: list[Exception] = []
    lock = threading.Lock()

    def runner(thread_id: int):
        for op in range(ops_per_thread):
            try:
                fn(thread_id * ops_per_thread + op)
            except Exception as e:  # noqa: BLE001
                with lock:
                    errors.append(e)

    threads = [threading.Thread(target=runner, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return errors


def test_concurrent_buys_preserve_cash_invariant(tmp_path, stub_provider):
    """50 small buys must reduce cash by exactly the sum of (price+fees) across all fills."""
    from papertrade_india import PriceFeed

    stub_provider.set("RELIANCE", 1000.0)
    feed = PriceFeed(providers=[stub_provider], short_cache_ttl_seconds=0)

    broker = IndiaPaperBroker(
        initial_capital=10_000_000.0,
        db_path=tmp_path / "concurrent.db",
        account_id="conc",
        price_feed=feed,
        enforce_market_hours=False,
    )

    def buy_one(i: int) -> None:
        broker.buy("RELIANCE", 1)

    errors = _run_concurrent(buy_one, n_threads=5, ops_per_thread=10)
    assert errors == [], f"Unexpected errors: {errors}"

    # Verify aggregate invariant: position qty == total fills, cash matches.
    pos = broker.get_position("RELIANCE")
    assert pos is not None
    assert pos.qty == 50

    # Sum of fees across all 50 fills.
    orders = broker.get_orders(limit=200)
    assert len(orders) == 50
    total_fees = sum(o.fees_paid for o in orders)
    expected_cash = 10_000_000.0 - 50 * 1000.0 - total_fees
    a = broker.get_account()
    assert a.cash == pytest.approx(expected_cash, abs=0.01)


def test_concurrent_buys_with_some_failing(tmp_path, stub_provider):
    """When cash runs out mid-stream, failed orders must not corrupt state.

    Start with limited cash so some buys succeed and some hit
    ``InsufficientFundsError``. Final cash + (qty * price) + fees must
    equal the initial ₹.
    """
    from papertrade_india import PriceFeed

    stub_provider.set("RELIANCE", 1000.0)
    feed = PriceFeed(providers=[stub_provider], short_cache_ttl_seconds=0)

    initial_cash = 25_000.0  # only ~24 buys @ ₹1000 will fit (with fees)
    broker = IndiaPaperBroker(
        initial_capital=initial_cash,
        db_path=tmp_path / "concurrent_fail.db",
        account_id="conc",
        price_feed=feed,
        enforce_market_hours=False,
    )

    def buy_one(i: int) -> None:
        with contextlib.suppress(InsufficientFundsError):
            broker.buy("RELIANCE", 1)

    _run_concurrent(buy_one, n_threads=5, ops_per_thread=10)

    pos = broker.get_position("RELIANCE")
    a = broker.get_account()
    qty = pos.qty if pos else 0
    orders = broker.get_orders(limit=200)
    # Only filled orders count toward fees on cash.
    filled_fees = sum(
        o.fees_paid for o in orders if o.status.value == "filled"
    )
    expected_cash = initial_cash - qty * 1000.0 - filled_fees
    assert a.cash == pytest.approx(expected_cash, abs=0.01), (
        f"Cash drift detected: got {a.cash}, expected {expected_cash}"
    )
    assert a.cash >= 0
