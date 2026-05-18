"""Integration tests for the event log and the in-process callback bus."""

from __future__ import annotations

import pytest

from papertrade_india import (
    BrokerEvent,
    EventBus,
    IndiaPaperBroker,
    KillSwitchActive,
    OrderType,
    RiskConfig,
)

pytestmark = pytest.mark.integration


# ── Persisted event log ──────────────────────────────────────────────


def test_market_buy_emits_submitted_filled_opened(broker, stub_provider):
    stub_provider.set("RELIANCE", 1000)
    broker.buy("RELIANCE", 5)

    events = broker.get_events()
    types = [e.event_type for e in events]
    # Event log is newest-first.
    assert "position_opened" in types
    assert "order_filled" in types
    assert "order_submitted" in types


def test_round_trip_emits_position_closed(broker, stub_provider):
    stub_provider.set("RELIANCE", 1000)
    broker.buy("RELIANCE", 5)
    broker.sell("RELIANCE", 5)

    events = broker.get_events()
    types = [e.event_type for e in events]
    assert "position_closed" in types
    assert types.count("order_filled") == 2


def test_event_payload_contains_order_id_and_symbol(broker, stub_provider):
    stub_provider.set("INFY", 1500)
    order = broker.buy("INFY", 1)
    filled_events = broker.get_events(event_types=("order_filled",))
    assert len(filled_events) == 1
    e = filled_events[0]
    assert e.order_id == order.id
    assert e.payload["symbol"] == "INFY"
    assert e.payload["side"] == "buy"
    assert e.payload["qty"] == 1


def test_event_log_filters_by_type(broker, stub_provider):
    stub_provider.set("RELIANCE", 1000)
    broker.buy("RELIANCE", 1)

    only_filled = broker.get_events(event_types=("order_filled",))
    only_submitted = broker.get_events(event_types=("order_submitted",))
    assert all(e.event_type == "order_filled" for e in only_filled)
    assert all(e.event_type == "order_submitted" for e in only_submitted)
    assert len(only_filled) == 1
    assert len(only_submitted) == 1


def test_cancel_emits_event(broker):
    order = broker.buy(
        "RELIANCE", 1,
        order_type=OrderType.LIMIT, limit_price=1000.0,
    )
    broker.cancel_order(order.id)
    events = broker.get_events(event_types=("order_cancelled",))
    assert len(events) == 1
    assert events[0].order_id == order.id


def test_expire_emits_events(broker):
    o1 = broker.buy("RELIANCE", 1, order_type=OrderType.LIMIT, limit_price=1)
    o2 = broker.buy("INFY", 1, order_type=OrderType.LIMIT, limit_price=1)
    broker.expire_stale_day_orders()
    events = broker.get_events(event_types=("order_expired",))
    assert {e.order_id for e in events} == {o1.id, o2.id}


def test_corporate_action_emits_event(broker, stub_provider):
    stub_provider.set("RELIANCE", 2000)
    broker.buy("RELIANCE", 5)
    broker.apply_split("RELIANCE", ratio_num=2, ratio_den=1)
    events = broker.get_events(event_types=("corporate_action",))
    assert len(events) == 1
    assert events[0].payload["type"] == "split"


def test_account_reset_emits_event(broker):
    broker.reset(initial_capital=2_000_000)
    events = broker.get_events(event_types=("account_reset",))
    assert len(events) == 1
    assert events[0].payload["initial_capital"] == 2_000_000


def test_risk_rejection_emits_event(tmp_path, price_feed, stub_provider):
    stub_provider.set("RELIANCE", 1000)
    broker = IndiaPaperBroker(
        initial_capital=100_000,
        db_path=tmp_path / "rrx.db",
        account_id="rrx",
        price_feed=price_feed,
        risk_config=RiskConfig(kill_switch=True),
        enforce_market_hours=False,
    )
    with pytest.raises(KillSwitchActive):
        broker.buy("RELIANCE", 1)

    rejected = broker.get_events(event_types=("order_rejected",))
    assert len(rejected) == 1
    assert rejected[0].payload["reason"] == "KillSwitchActive"


# ── In-process callback bus ─────────────────────────────────────────


def test_subscriber_receives_events(broker, stub_provider):
    received: list[BrokerEvent] = []
    broker.events.subscribe(received.append, name="test-collector")

    stub_provider.set("RELIANCE", 1000)
    broker.buy("RELIANCE", 1)

    types = [e.event_type for e in received]
    assert "order_submitted" in types
    assert "order_filled" in types


def test_subscriber_failure_does_not_break_others(broker, stub_provider):
    """A bad subscriber must not poison the bus for good ones."""
    good: list[BrokerEvent] = []

    def bad(event: BrokerEvent):
        raise RuntimeError("boom")

    broker.events.subscribe(bad, name="bad")
    broker.events.subscribe(good.append, name="good")

    stub_provider.set("RELIANCE", 1000)
    broker.buy("RELIANCE", 1)

    # The good subscriber still received events.
    assert len(good) >= 2  # at least submitted + filled


def test_subscriber_only_sees_committed_events(broker, stub_provider):
    """Events fire AFTER the SQL transaction commits.

    We test the negative case: a subscriber that does its own DB read
    sees the committed state, not a half-applied one. We can only
    approximate this without race conditions; verify the simpler
    invariant that the cash invariant holds at the moment a subscriber
    runs.
    """
    seen_invariant: list[bool] = []

    def assert_invariant_at_event_time(event: BrokerEvent):
        if event.event_type == "order_filled":
            seen_invariant.append(broker.verify_cash_invariant())

    broker.events.subscribe(assert_invariant_at_event_time)

    stub_provider.set("INFY", 1500)
    broker.buy("INFY", 1)
    assert seen_invariant == [True]


def test_unsubscribe_stops_delivery(broker, stub_provider):
    received: list[BrokerEvent] = []

    def collect(e):
        received.append(e)

    broker.events.subscribe(collect)
    stub_provider.set("RELIANCE", 1000)
    broker.buy("RELIANCE", 1)
    n1 = len(received)

    assert broker.events.unsubscribe(collect) is True

    broker.buy("RELIANCE", 1)
    assert len(received) == n1  # no new events after unsubscribe


def test_event_bus_can_be_shared_across_brokers(tmp_path, price_feed, stub_provider):
    """One bus, multiple accounts. Subscribers see events from both."""
    bus = EventBus()
    received: list[BrokerEvent] = []
    bus.subscribe(received.append)

    db = tmp_path / "shared_bus.db"
    a = IndiaPaperBroker(
        initial_capital=100_000, db_path=db, account_id="a",
        price_feed=price_feed, event_bus=bus, enforce_market_hours=False,
    )
    b = IndiaPaperBroker(
        initial_capital=100_000, db_path=db, account_id="b",
        price_feed=price_feed, event_bus=bus, enforce_market_hours=False,
    )
    stub_provider.set("RELIANCE", 1000)
    a.buy("RELIANCE", 1)
    b.buy("RELIANCE", 1)

    accounts_seen = {e.account_id for e in received if e.account_id}
    assert accounts_seen == {"a", "b"}
