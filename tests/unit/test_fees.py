"""Unit tests for the Indian fee engine.

These tests pin the default fee schedule against a few hand-checked
worked examples. If the defaults change in :class:`FeeConfig`, these
expectations need to update.
"""

from __future__ import annotations

import pytest

from papertrade_india import (
    Exchange,
    FeeBreakdown,
    FeeConfig,
    IndianFeeEngine,
    OrderSide,
)


@pytest.fixture
def engine() -> IndianFeeEngine:
    return IndianFeeEngine()


def test_buy_default_schedule_is_nonzero(engine: IndianFeeEngine):
    fb = engine.calculate(OrderSide.BUY, qty=10, price=2500.0,
                           exchange=Exchange.NSE)
    # Brokerage is ₹0 by default (delivery), but STT + GST + stamp duty
    # are positive, so the total should be > 0.
    assert fb.brokerage == 0.0
    assert fb.stt > 0
    assert fb.stamp_duty > 0
    assert fb.dp_charges == 0.0  # buy side
    assert fb.total > 0


def test_sell_pays_dp_charge_buy_does_not(engine: IndianFeeEngine):
    buy = engine.calculate(OrderSide.BUY, qty=10, price=2500.0,
                            exchange=Exchange.NSE)
    sell = engine.calculate(OrderSide.SELL, qty=10, price=2500.0,
                             exchange=Exchange.NSE)
    assert buy.dp_charges == 0.0
    assert sell.dp_charges == 13.5  # default
    assert sell.stamp_duty == 0.0
    assert buy.stamp_duty > 0


def test_total_equals_components(engine: IndianFeeEngine):
    fb = engine.calculate(OrderSide.SELL, qty=100, price=1500.0,
                           exchange=Exchange.NSE)
    components = (
        fb.brokerage + fb.stt + fb.exchange_charge + fb.gst
        + fb.sebi_charges + fb.stamp_duty + fb.dp_charges
    )
    # Allow ₹0.05 of paise-level rounding tolerance: each component is
    # rounded individually, so the sum can drift slightly from the rounded total.
    assert fb.total == pytest.approx(components, abs=0.05)


def test_zero_qty_returns_zero_fees(engine: IndianFeeEngine):
    fb = engine.calculate(OrderSide.BUY, qty=0, price=100.0,
                           exchange=Exchange.NSE)
    assert fb == FeeBreakdown(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)


def test_negative_qty_returns_zero_fees(engine: IndianFeeEngine):
    """Defensive: invalid input shouldn't blow up here, just zero out."""
    fb = engine.calculate(OrderSide.BUY, qty=-5, price=100.0,
                           exchange=Exchange.NSE)
    assert fb.total == 0.0


def test_bse_costs_more_than_nse_for_same_trade(engine: IndianFeeEngine):
    nse = engine.calculate(OrderSide.BUY, qty=10, price=2500.0,
                            exchange=Exchange.NSE)
    bse = engine.calculate(OrderSide.BUY, qty=10, price=2500.0,
                            exchange=Exchange.BSE)
    # BSE exchange charge is higher than NSE, so total fees on BSE > NSE.
    assert bse.exchange_charge > nse.exchange_charge
    assert bse.total > nse.total


def test_intraday_brokerage_capped(engine_intraday=None):
    """A broker config with percentage + cap should respect the cap."""
    engine = IndianFeeEngine(FeeConfig(
        brokerage_pct=0.0003,    # 0.03%
        brokerage_max=20.0,      # ₹20 cap
    ))
    # Big trade: pct alone = 100,000 * 0.0003 = ₹30 → capped at ₹20.
    fb = engine.calculate(OrderSide.BUY, qty=100, price=1000.0,
                           exchange=Exchange.NSE)
    assert fb.brokerage == 20.0


def test_intraday_brokerage_below_cap_uses_pct():
    engine = IndianFeeEngine(FeeConfig(
        brokerage_pct=0.0003,
        brokerage_max=20.0,
    ))
    # Small trade: pct alone = 10,000 * 0.0003 = ₹3.0 → uses pct.
    fb = engine.calculate(OrderSide.BUY, qty=100, price=100.0,
                           exchange=Exchange.NSE)
    assert fb.brokerage == 3.0


def test_gst_applies_to_brokerage_and_exchange_only():
    """GST is 18% of (brokerage + exchange), not on STT/stamp/SEBI."""
    cfg = FeeConfig(brokerage_flat=20.0, gst_pct=0.18)
    engine = IndianFeeEngine(cfg)
    fb = engine.calculate(OrderSide.SELL, qty=10, price=100.0,
                           exchange=Exchange.NSE)
    # Expected GST = 18% * (20 + 10*100*0.0000322)
    expected = (20.0 + 10 * 100 * 0.0000322) * 0.18
    assert fb.gst == pytest.approx(expected, abs=0.01)


def test_str_format_includes_all_components(engine: IndianFeeEngine):
    fb = engine.calculate(OrderSide.SELL, qty=1, price=100.0,
                           exchange=Exchange.NSE)
    s = str(fb)
    for label in ["Brokerage", "STT", "Exchange", "GST", "SEBI",
                  "Stamp", "DP", "Total"]:
        assert label in s
