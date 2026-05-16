"""Integration tests: full buy/hold/sell lifecycle against real SQLite.

No network. Price feed is the in-memory ``StubPriceProvider`` from
``conftest.py``; persistence is real on-disk SQLite under ``tmp_path``.
"""

from __future__ import annotations

import pytest

from papertrade_india import (
    IndiaPaperBroker,
    InsufficientFundsError,
    InsufficientSharesError,
    InvalidOrderError,
    MarketClosedError,
    NSECalendar,
    OrderStatus,
    OrderType,
)

pytestmark = pytest.mark.integration


# ── Happy path ────────────────────────────────────────────────────────


def test_initial_account_state(broker):
    a = broker.get_account()
    assert a.account_id == "test"
    assert a.cash == 1_000_000.0
    assert a.equity == 1_000_000.0
    assert a.portfolio_value == 0.0
    assert a.realized_pl_total == 0.0


def test_market_buy_fills_immediately_and_charges_fees(broker, stub_provider):
    stub_provider.set("RELIANCE", 2500.0)

    order = broker.buy("RELIANCE", 10)

    assert order.status == OrderStatus.FILLED
    assert order.filled_qty == 10
    assert order.filled_avg_price == 2500.0
    assert order.fees_paid > 0
    assert order.realized_pl == 0.0  # no realized P&L on a buy

    a = broker.get_account()
    expected_cash = 1_000_000.0 - (10 * 2500.0) - order.fees_paid
    assert a.cash == pytest.approx(expected_cash, abs=0.01)


def test_buy_creates_position_with_correct_avg_cost(broker, stub_provider):
    stub_provider.set("INFY", 1800.0)
    order = broker.buy("INFY", 5)

    pos = broker.get_position("INFY")
    assert pos is not None
    assert pos.qty == 5
    # avg_cost includes prorated buy fees, so it's slightly > price.
    expected_avg = (1800.0 * 5 + order.fees_paid) / 5
    assert pos.avg_cost == pytest.approx(expected_avg, abs=0.01)
    assert pos.avg_cost > 1800.0
    assert pos.current_price == 1800.0
    # Unrealized P&L equals -fees: cost_basis includes them but
    # market_value is just price*qty.
    assert pos.unrealized_pl == pytest.approx(-order.fees_paid, abs=0.01)


def test_multiple_buys_average_cost(broker, stub_provider):
    stub_provider.set("TCS", 4000.0)
    o1 = broker.buy("TCS", 5)
    stub_provider.set("TCS", 4200.0)
    o2 = broker.buy("TCS", 5)

    pos = broker.get_position("TCS")
    assert pos is not None
    assert pos.qty == 10
    # Volume-weighted INCLUDING prorated fees from both buys:
    #   (4000*5 + 4200*5 + fees_1 + fees_2) / 10
    expected = (4000 * 5 + 4200 * 5 + o1.fees_paid + o2.fees_paid) / 10
    assert pos.avg_cost == pytest.approx(expected, abs=0.01)
    # Sanity: cost_basis equals total cash spent on this position.
    expected_cb = 4000 * 5 + 4200 * 5 + o1.fees_paid + o2.fees_paid
    assert pos.cost_basis == pytest.approx(expected_cb, abs=0.01)


def test_sell_realizes_profit(broker, stub_provider):
    stub_provider.set("RELIANCE", 2000.0)
    buy = broker.buy("RELIANCE", 10)
    stub_provider.set("RELIANCE", 2500.0)
    sell = broker.sell("RELIANCE", 10)

    # Realized P&L should be gross profit minus *both* fee sides.
    gross = (2500 - 2000) * 10
    expected_pl = gross - buy.fees_paid - sell.fees_paid
    assert sell.realized_pl == pytest.approx(expected_pl, abs=0.01)

    # Position closed.
    assert broker.get_position("RELIANCE") is None

    # Account reflects realized P&L exactly.
    a = broker.get_account()
    assert a.realized_pl_total == pytest.approx(expected_pl, abs=0.01)


def test_round_trip_total_fees_match_account_drift(broker, stub_provider):
    """Round-trip cash + realized P&L invariant.

    Net change in cash over a round trip at the same price should equal
    -(buy_fees + sell_fees), and that should equal -realized_pl.
    """
    initial = broker.get_account().cash
    stub_provider.set("RELIANCE", 2500.0)
    buy = broker.buy("RELIANCE", 10)
    sell = broker.sell("RELIANCE", 10)

    a = broker.get_account()
    cash_drift = a.cash - initial
    total_fees = buy.fees_paid + sell.fees_paid

    assert cash_drift == pytest.approx(-total_fees, abs=0.01)
    assert a.realized_pl_total == pytest.approx(-total_fees, abs=0.01)
    assert sell.realized_pl == pytest.approx(-total_fees, abs=0.01)


def test_sell_realizes_loss(broker, stub_provider):
    stub_provider.set("INFY", 2000.0)
    buy = broker.buy("INFY", 10)
    stub_provider.set("INFY", 1800.0)
    sell = broker.sell("INFY", 10)

    # Loss = gross_loss - both_fees.
    gross_loss = (1800 - 2000) * 10  # -2000
    expected = gross_loss - buy.fees_paid - sell.fees_paid
    assert sell.realized_pl == pytest.approx(expected, abs=0.01)
    assert sell.realized_pl < -2000


def test_partial_sell_preserves_remaining_position(broker, stub_provider):
    stub_provider.set("TCS", 4000.0)
    buy = broker.buy("TCS", 10)
    pre_avg = broker.get_position("TCS").avg_cost
    broker.sell("TCS", 4)

    pos = broker.get_position("TCS")
    assert pos is not None
    assert pos.qty == 6
    # avg_cost on the remaining sleeve is unchanged on a partial sell —
    # we don't re-prorate buy fees across leftover shares.
    assert pos.avg_cost == pytest.approx(pre_avg)
    # Sanity: pre_avg includes the buy fees.
    assert pre_avg == pytest.approx(
        (4000 * 10 + buy.fees_paid) / 10, abs=0.01
    )


# ── Error paths ───────────────────────────────────────────────────────


def test_zero_qty_raises(broker):
    with pytest.raises(InvalidOrderError):
        broker.buy("RELIANCE", 0)


def test_negative_qty_raises(broker):
    with pytest.raises(InvalidOrderError):
        broker.buy("RELIANCE", -5)


def test_limit_order_without_price_raises(broker):
    with pytest.raises(InvalidOrderError):
        broker.buy("RELIANCE", 5, order_type=OrderType.LIMIT)


def test_limit_order_with_negative_price_raises(broker):
    with pytest.raises(InvalidOrderError):
        broker.buy("RELIANCE", 5, order_type=OrderType.LIMIT, limit_price=-10)


def test_buy_more_than_cash_raises(broker, stub_provider):
    stub_provider.set("RELIANCE", 2_000_000.0)  # ₹20L per share
    with pytest.raises(InsufficientFundsError):
        broker.buy("RELIANCE", 10)
    # No mutation: cash is unchanged, no order rows.
    assert broker.get_account().cash == 1_000_000.0
    assert broker.get_orders() == []


def test_sell_more_than_held_raises(broker, stub_provider):
    stub_provider.set("RELIANCE", 2500.0)
    broker.buy("RELIANCE", 5)
    with pytest.raises(InsufficientSharesError):
        broker.sell("RELIANCE", 10)
    # Position still has 5 shares.
    assert broker.get_position("RELIANCE").qty == 5


def test_sell_when_no_position_raises(broker):
    with pytest.raises(InsufficientSharesError):
        broker.sell("RELIANCE", 1)


# ── Market hours ──────────────────────────────────────────────────────


def test_market_order_outside_hours_raises(tmp_path, price_feed):
    """With ``enforce_market_hours=True`` and a calendar that always
    reports closed, MARKET orders must be rejected."""

    class AlwaysClosed(NSECalendar):
        def is_market_open(self, dt=None):
            return False

    broker = IndiaPaperBroker(
        initial_capital=1_000_000.0,
        db_path=tmp_path / "closed.db",
        account_id="closed",
        price_feed=price_feed,
        calendar=AlwaysClosed(),
        enforce_market_hours=True,
    )

    with pytest.raises(MarketClosedError):
        broker.buy("RELIANCE", 1)


def test_limit_order_outside_hours_queues(tmp_path, price_feed):
    """Limit orders should still queue when the market is closed."""

    class AlwaysClosed(NSECalendar):
        def is_market_open(self, dt=None):
            return False

    broker = IndiaPaperBroker(
        initial_capital=1_000_000.0,
        db_path=tmp_path / "closed.db",
        account_id="closed",
        price_feed=price_feed,
        calendar=AlwaysClosed(),
        enforce_market_hours=True,
    )
    order = broker.buy(
        "RELIANCE", 1,
        order_type=OrderType.LIMIT, limit_price=2000.0,
    )
    assert order.status == OrderStatus.PENDING


# ── Limit orders ──────────────────────────────────────────────────────


def test_limit_buy_queues_pending(broker, stub_provider):
    order = broker.buy(
        "RELIANCE", 5,
        order_type=OrderType.LIMIT, limit_price=2400.0,
    )
    assert order.status == OrderStatus.PENDING
    assert order.limit_price == 2400.0
    # Cash isn't deducted yet (only on fill).
    assert broker.get_account().cash == 1_000_000.0


def test_buying_power_reduced_by_pending_limit(broker, stub_provider):
    broker.buy("RELIANCE", 5,
               order_type=OrderType.LIMIT, limit_price=2400.0)
    a = broker.get_account()
    assert a.cash == 1_000_000.0
    # buying_power = cash - pending_buy_notional = 1,000,000 - 12,000
    assert a.buying_power == pytest.approx(1_000_000.0 - 5 * 2400.0)


def test_limit_watcher_fills_when_price_crosses(broker, stub_provider):
    from papertrade_india import LimitOrderWatcher

    broker.buy("RELIANCE", 5,
               order_type=OrderType.LIMIT, limit_price=2400.0)

    # Market price is above the limit — watcher tick should NOT fill.
    stub_provider.set("RELIANCE", 2500.0)
    watcher = LimitOrderWatcher(broker, interval_seconds=999)
    fills = watcher.tick()
    assert fills == 0
    assert broker.get_orders(status=OrderStatus.PENDING)

    # Drop price to or below limit — watcher tick should now fill.
    stub_provider.set("RELIANCE", 2400.0)
    fills = watcher.tick()
    assert fills == 1
    assert broker.get_orders(status=OrderStatus.PENDING) == []
    pos = broker.get_position("RELIANCE")
    assert pos is not None and pos.qty == 5


def test_limit_sell_fills_when_market_rises(broker, stub_provider):
    from papertrade_india import LimitOrderWatcher

    stub_provider.set("RELIANCE", 2000.0)
    broker.buy("RELIANCE", 5)
    broker.sell("RELIANCE", 5,
                order_type=OrderType.LIMIT, limit_price=2200.0)

    watcher = LimitOrderWatcher(broker, interval_seconds=999)
    # Below the limit — no fill.
    stub_provider.set("RELIANCE", 2100.0)
    assert watcher.tick() == 0

    # At/above the limit — fill.
    stub_provider.set("RELIANCE", 2200.0)
    assert watcher.tick() == 1
    assert broker.get_position("RELIANCE") is None


# ── Cancellation ──────────────────────────────────────────────────────


def test_cancel_pending_limit_order(broker):
    order = broker.buy("RELIANCE", 5,
                       order_type=OrderType.LIMIT, limit_price=2000.0)
    assert broker.cancel_order(order.id) is True
    fetched = broker.get_order(order.id)
    assert fetched.status == OrderStatus.CANCELLED
    assert fetched.cancelled_at is not None


def test_cannot_cancel_filled_order(broker):
    order = broker.buy("RELIANCE", 1)  # market — fills immediately
    assert broker.cancel_order(order.id) is False


def test_cancel_unknown_order_returns_false(broker):
    assert broker.cancel_order("does-not-exist") is False


def test_cancel_all_orders_returns_count(broker):
    broker.buy("RELIANCE", 1, order_type=OrderType.LIMIT, limit_price=2000.0)
    broker.buy("INFY", 1, order_type=OrderType.LIMIT, limit_price=1500.0)
    broker.buy("TCS", 1)  # market — already filled
    assert broker.cancel_all_orders() == 2


# ── Reset ─────────────────────────────────────────────────────────────


def test_reset_clears_state(broker, stub_provider):
    broker.buy("RELIANCE", 5)
    broker.sell("RELIANCE", 5)
    assert broker.get_account().realized_pl_total != 0

    broker.reset(initial_capital=2_000_000.0)
    a = broker.get_account()
    assert a.cash == 2_000_000.0
    assert a.realized_pl_total == 0
    assert broker.get_positions() == []
    assert broker.get_orders() == []


def test_reset_without_capital_keeps_cash_but_clears_pl(broker, stub_provider):
    broker.buy("RELIANCE", 5)
    broker.sell("RELIANCE", 5)
    cash_before = broker.get_account().cash

    broker.reset()
    a = broker.get_account()
    assert a.cash == cash_before
    assert a.realized_pl_total == 0


# ── Multi-account ─────────────────────────────────────────────────────


def test_two_accounts_share_db_independently(tmp_path, price_feed):
    db = tmp_path / "shared.db"
    a = IndiaPaperBroker(
        initial_capital=300_000, db_path=db, account_id="a",
        price_feed=price_feed, enforce_market_hours=False,
    )
    b = IndiaPaperBroker(
        initial_capital=500_000, db_path=db, account_id="b",
        price_feed=price_feed, enforce_market_hours=False,
    )

    a.buy("RELIANCE", 5)
    b.buy("INFY", 3)

    assert a.get_position("RELIANCE") is not None
    assert a.get_position("INFY") is None
    assert b.get_position("RELIANCE") is None
    assert b.get_position("INFY") is not None

    # Account separation verified.
    assert a.get_account().cash != b.get_account().cash
    assert a.get_account().cash < 300_000  # spent some
    assert b.get_account().cash < 500_000


def test_persistence_survives_broker_recreate(tmp_path, price_feed):
    """A position written by one broker instance is visible to another
    instance pointed at the same DB."""
    db = tmp_path / "persist.db"
    b1 = IndiaPaperBroker(
        initial_capital=500_000, db_path=db, account_id="x",
        price_feed=price_feed, enforce_market_hours=False,
    )
    b1.buy("RELIANCE", 5)
    cash_after_buy = b1.get_account().cash

    # New instance, same DB.
    b2 = IndiaPaperBroker(
        initial_capital=999_999, db_path=db, account_id="x",
        price_feed=price_feed, enforce_market_hours=False,
    )
    # Existing account: ``initial_capital`` is ignored.
    assert b2.get_account().cash == pytest.approx(cash_after_buy)
    pos = b2.get_position("RELIANCE")
    assert pos is not None and pos.qty == 5


# ── strict_open / AccountNotFound ────────────────────────────────────


def test_strict_open_raises_for_unknown_account(tmp_path, price_feed):
    """``strict_open=True`` must NOT auto-create accounts."""
    from papertrade_india import AccountNotFoundError

    # Create the DB (and a 'real' account) so the file exists.
    IndiaPaperBroker(
        initial_capital=100, db_path=tmp_path / "s.db", account_id="real",
        price_feed=price_feed, enforce_market_hours=False,
    )
    with pytest.raises(AccountNotFoundError):
        IndiaPaperBroker(
            db_path=tmp_path / "s.db",
            account_id="ghost",
            price_feed=price_feed,
            enforce_market_hours=False,
            strict_open=True,
        )


def test_default_open_creates_account(tmp_path, price_feed):
    """Default ``strict_open=False`` keeps the convenient auto-create."""
    b = IndiaPaperBroker(
        initial_capital=42_000,
        db_path=tmp_path / "auto.db",
        account_id="fresh",
        price_feed=price_feed,
        enforce_market_hours=False,
    )
    assert b.get_account().cash == 42_000


# ── DAY-tif expiry ───────────────────────────────────────────────────


def test_expire_stale_day_orders_marks_pending_as_expired(broker):
    o1 = broker.buy("RELIANCE", 1, order_type=OrderType.LIMIT, limit_price=1000)
    o2 = broker.buy("INFY", 1, order_type=OrderType.LIMIT, limit_price=1000)
    # A non-DAY pending order should NOT expire.
    o3 = broker.buy(
        "TCS", 1, order_type=OrderType.LIMIT, limit_price=1000,
        time_in_force="GTC",
    )

    n = broker.expire_stale_day_orders()
    assert n == 2

    assert broker.get_order(o1.id).status == OrderStatus.EXPIRED
    assert broker.get_order(o1.id).expired_at is not None
    assert broker.get_order(o2.id).status == OrderStatus.EXPIRED
    assert broker.get_order(o3.id).status == OrderStatus.PENDING


def test_expired_orders_dont_get_filled_by_watcher(broker, stub_provider):
    from papertrade_india import LimitOrderWatcher

    broker.buy("RELIANCE", 5,
               order_type=OrderType.LIMIT, limit_price=2400.0)
    broker.expire_stale_day_orders()

    # Drop price below limit — would normally fill, but order is expired.
    stub_provider.set("RELIANCE", 2200.0)
    watcher = LimitOrderWatcher(broker, interval_seconds=999)
    assert watcher.tick() == 0
    assert broker.get_position("RELIANCE") is None


# ── Race-safety: cancel-vs-fill ──────────────────────────────────────


def test_cancel_during_limit_fill_does_not_double_apply(broker, stub_provider):
    """Reproducer for the cancel-vs-fill race.

    Before the fix: watcher's `_execute_limit_fill` could clobber a
    CANCELLED row back to FILLED and open an unwanted position.

    After the fix: watcher claims the order via
    ``WHERE status='pending'``; if status is already CANCELLED, rowcount
    is zero and ``OrderNoLongerPending`` is raised inside the
    transaction (rolling back the apply_buy/apply_sell changes).
    """
    from papertrade_india import LimitOrderWatcher
    from papertrade_india.exceptions import OrderNoLongerPending

    order = broker.buy(
        "RELIANCE", 5,
        order_type=OrderType.LIMIT, limit_price=2400.0,
    )
    # Simulate the race: cancel immediately. Then call _execute_limit_fill
    # directly — same path the watcher takes after fetching the snapshot.
    assert broker.cancel_order(order.id)

    # Re-fetch was already PENDING in the watcher's snapshot view.
    snapshot = order  # still has status=PENDING from the buy() return.
    with pytest.raises(OrderNoLongerPending):
        broker._execute_limit_fill(snapshot, fill_price=2400.0)

    # No position was opened, no cash drift, order is still cancelled.
    assert broker.get_position("RELIANCE") is None
    assert broker.get_order(order.id).status == OrderStatus.CANCELLED
    assert broker.get_account().cash == 1_000_000.0

    # And the watcher tick handles it gracefully (no crash, no fill).
    stub_provider.set("RELIANCE", 2400.0)
    watcher = LimitOrderWatcher(broker, interval_seconds=999)
    assert watcher.tick() == 0


# ── Stale price flag ─────────────────────────────────────────────────


def test_position_marks_stale_when_feed_fails(broker, stub_provider, monkeypatch):
    """When PriceFeed raises (no live, no cache), Position falls back to
    avg_cost AND sets ``current_price_stale=True``."""
    stub_provider.set("RELIANCE", 2500.0)
    broker.buy("RELIANCE", 5)

    # Tear down the feed so subsequent calls raise.
    from papertrade_india.exceptions import PriceUnavailableError

    def boom(symbol):
        raise PriceUnavailableError("simulated outage")

    monkeypatch.setattr(broker.price_feed, "get_price", boom)

    pos = broker.get_position("RELIANCE")
    assert pos is not None
    assert pos.current_price_stale is True
    # Falls back to avg_cost — unrealized_pl is 0 BUT current_price_stale
    # tells the caller this isn't a real break-even.
    assert pos.current_price == pytest.approx(pos.avg_cost)
    assert pos.unrealized_pl == 0.0


def test_position_not_stale_when_feed_ok(broker, stub_provider):
    stub_provider.set("RELIANCE", 2500.0)
    broker.buy("RELIANCE", 5)
    pos = broker.get_position("RELIANCE")
    assert pos is not None
    assert pos.current_price_stale is False


# ── get_position O(1) lookup ─────────────────────────────────────────


def test_get_position_returns_none_for_unheld(broker):
    assert broker.get_position("NEVERHELD") is None


def test_get_position_only_returns_caller_account(tmp_path, price_feed):
    """``get_position`` is scoped to the broker's own ``account_id``."""
    db = tmp_path / "scoped.db"
    a = IndiaPaperBroker(
        initial_capital=100_000, db_path=db, account_id="alice",
        price_feed=price_feed, enforce_market_hours=False,
    )
    b = IndiaPaperBroker(
        initial_capital=100_000, db_path=db, account_id="bob",
        price_feed=price_feed, enforce_market_hours=False,
    )
    a.buy("RELIANCE", 1)
    assert b.get_position("RELIANCE") is None
    assert a.get_position("RELIANCE") is not None


# ── Schema CHECK constraints ─────────────────────────────────────────


def test_negative_cash_constraint_holds(broker, stub_provider):
    """Cash CHECK(>= 0) is a defensive guard. The InsufficientFundsError
    path prevents overdraft pre-update, so we don't expect to hit the
    constraint in normal flow — but the constraint does exist."""
    import sqlite3
    with broker.persistence.transaction() as conn, pytest.raises(
        sqlite3.IntegrityError
    ):
        # Direct UPDATE bypassing _apply_buy — would corrupt invariant.
        conn.execute(
            "UPDATE account SET cash = -1 WHERE account_id = ?",
            (broker.account_id,),
        )
