"""Tests for idempotency-key handling."""

from __future__ import annotations

import pytest

from papertrade_india import (
    IdempotencyConflict,
    OrderType,
)

pytestmark = pytest.mark.integration


def test_replay_returns_same_order(broker, stub_provider):
    """Same key + same params → second call returns the original order."""
    stub_provider.set("RELIANCE", 2500)
    o1 = broker.buy("RELIANCE", 1, idempotency_key="abc-123")
    o2 = broker.buy("RELIANCE", 1, idempotency_key="abc-123")
    assert o1.id == o2.id


def test_replay_does_not_duplicate_fills(broker, stub_provider):
    """A replayed buy must not deduct cash twice or open a 2-share position."""
    stub_provider.set("RELIANCE", 1000)
    cash0 = broker.get_account().cash
    broker.buy("RELIANCE", 1, idempotency_key="dup-1")
    broker.buy("RELIANCE", 1, idempotency_key="dup-1")  # replay

    pos = broker.get_position("RELIANCE")
    assert pos is not None
    assert pos.qty == 1  # not 2

    # Cash drift = one buy's worth, not two.
    cash1 = broker.get_account().cash
    drift = cash0 - cash1
    # 1 share at 1000 + fees ~few rupees
    assert 1000 < drift < 1100


def test_mismatched_params_raise_conflict(broker, stub_provider):
    """Same key + different params → IdempotencyConflict."""
    stub_provider.set("RELIANCE", 1000)
    broker.buy("RELIANCE", 1, idempotency_key="key-x")
    with pytest.raises(IdempotencyConflict):
        broker.buy("RELIANCE", 2, idempotency_key="key-x")  # different qty


def test_mismatched_side_raises_conflict(broker, stub_provider):
    stub_provider.set("RELIANCE", 1000)
    broker.buy("RELIANCE", 1, idempotency_key="k1")
    with pytest.raises(IdempotencyConflict):
        broker.sell("RELIANCE", 1, idempotency_key="k1")


def test_different_keys_create_separate_orders(broker, stub_provider):
    stub_provider.set("RELIANCE", 500)
    a = broker.buy("RELIANCE", 1, idempotency_key="key-a")
    b = broker.buy("RELIANCE", 1, idempotency_key="key-b")
    assert a.id != b.id

    pos = broker.get_position("RELIANCE")
    assert pos is not None
    assert pos.qty == 2


def test_no_key_means_no_replay(broker, stub_provider):
    """Two buys without keys are independent fills, even with identical params."""
    stub_provider.set("RELIANCE", 500)
    a = broker.buy("RELIANCE", 1)
    b = broker.buy("RELIANCE", 1)
    assert a.id != b.id


def test_replay_works_for_limit_orders(broker, stub_provider):
    """Pending limit orders also dedupe by key."""
    o1 = broker.buy(
        "RELIANCE", 5,
        order_type=OrderType.LIMIT, limit_price=2000.0,
        idempotency_key="limit-1",
    )
    o2 = broker.buy(
        "RELIANCE", 5,
        order_type=OrderType.LIMIT, limit_price=2000.0,
        idempotency_key="limit-1",
    )
    assert o1.id == o2.id
    # Only one pending order in the table.
    pending = broker.get_orders()
    assert sum(1 for o in pending if o.id == o1.id) == 1


def test_replay_after_idempotency_record_orphaned(broker, stub_provider):
    """If the original order is wiped (e.g. by reset()), the idempotency
    table has a dangling pointer. The replay logic falls through to a
    fresh submission rather than crashing."""
    stub_provider.set("RELIANCE", 1000)
    broker.buy("RELIANCE", 1, idempotency_key="orphan")
    broker.reset(initial_capital=1_000_000)  # clears orders + idempotency rows via FK CASCADE

    # After reset, key is gone too — fresh submission works.
    new = broker.buy("RELIANCE", 1, idempotency_key="orphan")
    assert new.id is not None


def test_cleanup_idempotency_keys_drops_old(broker, stub_provider):
    """``cleanup_idempotency_keys`` removes stale rows."""
    stub_provider.set("RELIANCE", 1000)
    broker.buy("RELIANCE", 1, idempotency_key="old-key")

    # TTL = 0 hours means everything is past TTL.
    n = broker.cleanup_idempotency_keys(hours=0)
    assert n == 1

    # With the key cleared, replay no longer dedupes.
    new = broker.buy("RELIANCE", 1, idempotency_key="old-key")
    assert new is not None  # new fill, not a replay


def test_idempotency_key_scoped_per_account(tmp_path, price_feed, stub_provider):
    """Same key across two accounts → independent orders. No cross-account
    contamination."""
    from papertrade_india import IndiaPaperBroker

    db = tmp_path / "scope.db"
    a = IndiaPaperBroker(
        initial_capital=100_000, db_path=db, account_id="alice",
        price_feed=price_feed, enforce_market_hours=False,
    )
    b = IndiaPaperBroker(
        initial_capital=100_000, db_path=db, account_id="bob",
        price_feed=price_feed, enforce_market_hours=False,
    )

    stub_provider.set("RELIANCE", 1000)
    o_a = a.buy("RELIANCE", 1, idempotency_key="shared-key")
    o_b = b.buy("RELIANCE", 1, idempotency_key="shared-key")
    assert o_a.id != o_b.id
    # Each account holds 1 share.
    assert a.get_position("RELIANCE").qty == 1
    assert b.get_position("RELIANCE").qty == 1
