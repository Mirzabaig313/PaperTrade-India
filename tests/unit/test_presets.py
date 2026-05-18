"""Tests for named broker fee presets."""

from __future__ import annotations

import pytest

from papertrade_india import Exchange, IndianFeeEngine, OrderSide
from papertrade_india.presets import (
    ANGEL_ONE_INTRADAY,
    GROWW_DELIVERY,
    PRESETS,
    UPSTOX_INTRADAY,
    ZERODHA_DELIVERY,
    ZERODHA_INTRADAY,
    get_preset,
)


def test_zerodha_delivery_matches_default_config():
    """Zerodha delivery is the package default."""
    fb_zd = IndianFeeEngine(ZERODHA_DELIVERY).calculate(
        OrderSide.BUY, 10, 2500, Exchange.NSE,
    )
    fb_default = IndianFeeEngine().calculate(
        OrderSide.BUY, 10, 2500, Exchange.NSE,
    )
    assert fb_zd == fb_default


def test_zerodha_intraday_caps_brokerage_at_20():
    """Big trade: 0.03% would be ₹30, but the cap is ₹20."""
    e = IndianFeeEngine(ZERODHA_INTRADAY)
    fb = e.calculate(OrderSide.BUY, qty=100, price=1000, exchange=Exchange.NSE)
    assert fb.brokerage == 20.0


def test_zerodha_intraday_below_cap_uses_pct():
    e = IndianFeeEngine(ZERODHA_INTRADAY)
    # Turnover = 10 * 100 = 1000; 0.03% = ₹0.30
    fb = e.calculate(OrderSide.BUY, qty=10, price=100, exchange=Exchange.NSE)
    assert fb.brokerage == 0.30


def test_groww_delivery_caps_at_20():
    """Groww: ₹20 flat or 0.1% (delivery), whichever is lower."""
    e = IndianFeeEngine(GROWW_DELIVERY)
    # Turnover = 100 * 1000 = 100000; 0.1% = ₹100 → capped at ₹20.
    fb = e.calculate(OrderSide.BUY, qty=100, price=1000, exchange=Exchange.NSE)
    assert fb.brokerage == 20.0


def test_angel_one_intraday_charges_flat_20():
    e = IndianFeeEngine(ANGEL_ONE_INTRADAY)
    # Flat ₹20 regardless of turnover.
    small = e.calculate(OrderSide.BUY, qty=1, price=100, exchange=Exchange.NSE)
    big = e.calculate(OrderSide.BUY, qty=1000, price=10000, exchange=Exchange.NSE)
    assert small.brokerage == 20.0
    assert big.brokerage == 20.0


def test_upstox_intraday_capped_at_20():
    e = IndianFeeEngine(UPSTOX_INTRADAY)
    fb = e.calculate(OrderSide.BUY, qty=200, price=1000, exchange=Exchange.NSE)
    assert fb.brokerage == 20.0


def test_get_preset_known_name():
    cfg = get_preset("zerodha-delivery")
    assert cfg is ZERODHA_DELIVERY


def test_get_preset_case_insensitive_and_underscores():
    assert get_preset("ZERODHA_DELIVERY") is ZERODHA_DELIVERY
    assert get_preset("Zerodha-Delivery") is ZERODHA_DELIVERY


def test_get_preset_unknown_name_raises():
    with pytest.raises(KeyError, match="Unknown preset"):
        get_preset("not-a-broker")


def test_presets_dict_is_complete():
    """All exposed presets are in the lookup dict."""
    expected = {
        "zerodha-delivery", "zerodha-intraday",
        "upstox-delivery", "upstox-intraday",
        "groww-delivery",
        "angel-one-delivery", "angel-one-intraday",
        "icicidirect-delivery",
    }
    assert set(PRESETS) == expected
