"""Tests for ``enforce_fresh_prices=True`` mode.

When set, the broker refuses to fill an order whose underlying price
came from the long-lived stale cache. The order rolls back; for limit
orders it stays PENDING for the next watcher tick.
"""

from __future__ import annotations

import pytest

from papertrade_india import (
    IndiaPaperBroker,
    LimitOrderWatcher,
    OrderStatus,
    OrderType,
    PriceFeed,
    StalePriceRejected,
)

pytestmark = pytest.mark.integration


def _stub_only_feed(monkeypatch, stub_provider):
    """A feed that uses only the stub provider, so we can deterministically
    flip it to "all live providers fail" by clearing prices."""
    return PriceFeed(providers=[stub_provider], short_cache_ttl_seconds=0)


def test_fresh_quote_is_not_stale(tmp_path, stub_provider):
    """Sanity: when the live provider returns a price, the broker fills."""
    feed = PriceFeed(providers=[stub_provider], short_cache_ttl_seconds=0)
    stub_provider.set("RELIANCE", 2500)
    broker = IndiaPaperBroker(
        initial_capital=500_000,
        db_path=tmp_path / "fresh.db",
        account_id="fresh",
        price_feed=feed,
        enforce_fresh_prices=True,
        enforce_market_hours=False,
    )
    order = broker.buy("RELIANCE", 1)
    assert order.status == OrderStatus.FILLED


def test_stale_price_rejected_in_strict_mode(tmp_path, stub_provider):
    """Once the live provider stops responding, the long-lived cache
    serves stale data — strict mode should reject."""
    feed = PriceFeed(providers=[stub_provider], short_cache_ttl_seconds=0)
    stub_provider.set("RELIANCE", 2500)
    broker = IndiaPaperBroker(
        initial_capital=500_000,
        db_path=tmp_path / "strict.db",
        account_id="strict",
        price_feed=feed,
        enforce_fresh_prices=True,
        enforce_market_hours=False,
    )
    # Prime the long-cache with a fresh fetch.
    feed.get_quote("RELIANCE")
    # Now make the live provider fail. The next get_quote will fall
    # back to the long cache and is_stale=True.
    stub_provider.prices.pop("RELIANCE")

    with pytest.raises(StalePriceRejected):
        broker.buy("RELIANCE", 1)

    # No state mutation: no order, no position, no cash drift.
    assert broker.get_orders() == []
    assert broker.get_position("RELIANCE") is None
    assert broker.get_account().cash == 500_000


def test_stale_price_allowed_when_strict_off(tmp_path, stub_provider):
    """Default mode (``enforce_fresh_prices=False``) accepts cached fills."""
    feed = PriceFeed(providers=[stub_provider], short_cache_ttl_seconds=0)
    stub_provider.set("RELIANCE", 2500)
    broker = IndiaPaperBroker(
        initial_capital=500_000,
        db_path=tmp_path / "lenient.db",
        account_id="lenient",
        price_feed=feed,
        enforce_fresh_prices=False,
        enforce_market_hours=False,
    )
    feed.get_quote("RELIANCE")  # prime cache
    stub_provider.prices.pop("RELIANCE")

    # Lenient mode: fill goes through using the cached price.
    order = broker.buy("RELIANCE", 1)
    assert order.status == OrderStatus.FILLED
    assert order.filled_avg_price > 0


def test_stale_price_rejected_on_limit_fill(tmp_path, stub_provider):
    """The watcher's call into ``_execute_limit_fill`` should also honor
    ``enforce_fresh_prices``.

    We test by asking the broker to fill directly with a stale-source
    quote — easier than racing the watcher.
    """
    feed = PriceFeed(providers=[stub_provider], short_cache_ttl_seconds=0)
    stub_provider.set("RELIANCE", 2500)
    broker = IndiaPaperBroker(
        initial_capital=500_000,
        db_path=tmp_path / "limit_strict.db",
        account_id="strict",
        price_feed=feed,
        enforce_fresh_prices=True,
        enforce_market_hours=False,
    )
    # Limit at 2500 (matches current price); queue it.
    order = broker.buy(
        "RELIANCE", 1,
        order_type=OrderType.LIMIT, limit_price=2500.0,
    )
    assert order.status == OrderStatus.PENDING

    # Live provider goes down. Watcher tries to fill from cached price.
    feed.get_quote("RELIANCE")
    stub_provider.prices.pop("RELIANCE")

    watcher = LimitOrderWatcher(broker, interval_seconds=999)
    fills = watcher.tick()
    # The watcher fetched a stale quote and the broker rejected the fill.
    # The watcher catches the resulting exception via its except branch
    # and logs it; no fill recorded.
    assert fills == 0
    # Order still PENDING for the next live tick.
    assert broker.get_order(order.id).status == OrderStatus.PENDING
