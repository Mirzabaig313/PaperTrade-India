"""Unit tests for the slippage model."""

from __future__ import annotations

import pytest

from papertrade_india import (
    OrderSide,
    OrderType,
    SlippageConfig,
    apply_slippage,
)


def test_zero_bps_is_identity():
    cfg = SlippageConfig(bps=0)
    for side in (OrderSide.BUY, OrderSide.SELL):
        assert apply_slippage(cfg, side, OrderType.MARKET, 1000.0) == 1000.0


def test_buy_pays_above_last():
    cfg = SlippageConfig(bps=10)  # 0.10%
    p = apply_slippage(cfg, OrderSide.BUY, OrderType.MARKET, 1000.0)
    assert p == pytest.approx(1001.0)


def test_sell_pays_below_last():
    cfg = SlippageConfig(bps=10)
    p = apply_slippage(cfg, OrderSide.SELL, OrderType.MARKET, 1000.0)
    assert p == pytest.approx(999.0)


def test_limit_orders_unaffected_by_default():
    """Default ``apply_to_limits=False`` preserves the legacy behavior:
    LIMIT fills land at exactly the supplied price."""
    cfg = SlippageConfig(bps=50)
    p = apply_slippage(
        cfg, OrderSide.BUY, OrderType.LIMIT,
        last_price=1000.0, limit_price=1000.0,
    )
    assert p == 1000.0


def test_limit_buy_capped_by_limit_price():
    """Even with apply_to_limits=True, a buy never fills above its limit."""
    cfg = SlippageConfig(bps=50, apply_to_limits=True)  # 0.5%
    # last=995, slipped buy = 995 * 1.005 = 1000.0; limit = 998
    p = apply_slippage(
        cfg, OrderSide.BUY, OrderType.LIMIT,
        last_price=995.0, limit_price=998.0,
    )
    assert p == 998.0  # capped


def test_limit_sell_capped_by_limit_price():
    cfg = SlippageConfig(bps=50, apply_to_limits=True)
    # last=1005, slipped sell = 1005 * 0.995 = 999.975; limit = 1000
    p = apply_slippage(
        cfg, OrderSide.SELL, OrderType.LIMIT,
        last_price=1005.0, limit_price=1000.0,
    )
    assert p == 1000.0  # floored at limit


def test_limit_apply_when_within_bound():
    """When the slipped price is between last and limit, slippage applies."""
    cfg = SlippageConfig(bps=50, apply_to_limits=True)
    # last=1000, slipped buy = 1005, limit = 1010 — slipped <= limit, use it
    p = apply_slippage(
        cfg, OrderSide.BUY, OrderType.LIMIT,
        last_price=1000.0, limit_price=1010.0,
    )
    assert p == pytest.approx(1005.0)


def test_negative_last_price_rejected():
    cfg = SlippageConfig(bps=5)
    with pytest.raises(ValueError):
        apply_slippage(cfg, OrderSide.BUY, OrderType.MARKET, -10)


def test_negative_bps_clamped_to_zero():
    """Defensive: a misconfigured negative bps doesn't move the price."""
    cfg = SlippageConfig(bps=-5)
    p = apply_slippage(cfg, OrderSide.BUY, OrderType.MARKET, 1000.0)
    assert p == 1000.0


# ── Broker integration ────────────────────────────────────────────────


def test_broker_market_buy_pays_slippage(tmp_path, stub_provider, price_feed):
    """A market buy through the broker fills above last when bps > 0."""
    from papertrade_india import IndiaPaperBroker

    stub_provider.set("RELIANCE", 1000.0)
    broker = IndiaPaperBroker(
        initial_capital=1_000_000,
        db_path=tmp_path / "slip.db",
        account_id="slip",
        price_feed=price_feed,
        slippage_config=SlippageConfig(bps=10),  # 0.10%
        enforce_market_hours=False,
    )
    order = broker.buy("RELIANCE", 1)
    assert order.filled_avg_price == pytest.approx(1001.0)


def test_broker_market_sell_receives_below(tmp_path, stub_provider, price_feed):
    from papertrade_india import IndiaPaperBroker

    stub_provider.set("RELIANCE", 1000.0)
    broker = IndiaPaperBroker(
        initial_capital=1_000_000,
        db_path=tmp_path / "slip.db",
        account_id="slip",
        price_feed=price_feed,
        slippage_config=SlippageConfig(bps=10),
        enforce_market_hours=False,
    )
    broker.buy("RELIANCE", 5)  # establish position (also at slipped price)
    sell = broker.sell("RELIANCE", 2)
    # SELL slipped = 1000 * (1 - 0.001) = 999.0
    assert sell.filled_avg_price == pytest.approx(999.0)


def test_default_broker_has_zero_slippage(tmp_path, stub_provider, price_feed):
    """A broker constructed without ``slippage_config`` matches legacy
    behavior: fill price == last price."""
    from papertrade_india import IndiaPaperBroker

    stub_provider.set("RELIANCE", 2500.0)
    broker = IndiaPaperBroker(
        initial_capital=1_000_000,
        db_path=tmp_path / "noslip.db",
        account_id="noslip",
        price_feed=price_feed,
        enforce_market_hours=False,
    )
    order = broker.buy("RELIANCE", 1)
    assert order.filled_avg_price == 2500.0
