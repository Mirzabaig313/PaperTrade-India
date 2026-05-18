"""Property-based tests for the Indian fee engine.

These hold across the full input space, not just hand-picked values.
Use ``hypothesis`` to generate plausible orders and assert structural
properties of the breakdown.
"""

from __future__ import annotations

import math

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from papertrade_india import (
    Exchange,
    FeeBreakdown,
    FeeConfig,
    IndianFeeEngine,
    OrderSide,
)

# Plausible-trade strategies. Bound qty/price so we don't generate
# turnovers > 10^15 INR (which exists nowhere outside synthetic tests
# and would only stress floating-point precision, not engine logic).
qtys = st.floats(min_value=1.0, max_value=1e6,
                 allow_nan=False, allow_infinity=False)
prices = st.floats(min_value=0.01, max_value=1e6,
                   allow_nan=False, allow_infinity=False)
sides = st.sampled_from([OrderSide.BUY, OrderSide.SELL])
exchanges = st.sampled_from([Exchange.NSE, Exchange.BSE])

# Turnover-floor: below this, statutory components round to ₹0.00 and
# break "always positive" properties even though the formula is correct.
# Use this for tests that need each component to be strictly > 0.
qtys_meaningful = st.floats(min_value=10.0, max_value=1e6,
                            allow_nan=False, allow_infinity=False)
prices_meaningful = st.floats(min_value=10.0, max_value=1e6,
                              allow_nan=False, allow_infinity=False)


@given(side=sides, qty=qtys, price=prices, exch=exchanges)
@settings(max_examples=200, deadline=None)
def test_fee_components_are_non_negative(side, qty, price, exch):
    """Every fee component is >= 0 for any (qty, price, side, exch)."""
    fb = IndianFeeEngine().calculate(side, qty, price, exch)
    for name in (
        "brokerage", "stt", "exchange_charge", "gst",
        "sebi_charges", "stamp_duty", "dp_charges", "total",
    ):
        v = getattr(fb, name)
        assert v >= 0, f"{name} negative: {v}"
        assert math.isfinite(v), f"{name} not finite: {v}"


@given(side=sides, qty=qtys, price=prices, exch=exchanges)
@settings(max_examples=200, deadline=None)
def test_total_within_paise_of_components(side, qty, price, exch):
    """``total`` equals the sum of components within paise rounding."""
    fb = IndianFeeEngine().calculate(side, qty, price, exch)
    components = (
        fb.brokerage + fb.stt + fb.exchange_charge + fb.gst
        + fb.sebi_charges + fb.stamp_duty + fb.dp_charges
    )
    # Each of 7 components rounded independently; max drift is 7 * 0.005
    assert fb.total == pytest.approx(components, abs=0.05)


@given(qty=qtys_meaningful, price=prices_meaningful, exch=exchanges)
@settings(max_examples=200, deadline=None)
def test_buy_pays_stamp_duty_sell_pays_dp(qty, price, exch):
    """Stamp duty is buy-only; DP charge is sell-only. Always.

    For meaningful turnovers (qty*price >= ₹100), each is strictly > 0.
    Below that, paise rounding can floor stamp_duty to ₹0.00 — the
    formula still holds, just the rounded representation is zero.
    """
    e = IndianFeeEngine()
    buy = e.calculate(OrderSide.BUY, qty, price, exch)
    sell = e.calculate(OrderSide.SELL, qty, price, exch)
    # Buy-only / sell-only structural property holds for any turnover.
    assert buy.dp_charges == 0.0
    assert sell.stamp_duty == 0.0
    # For meaningful turnover, the buy-side stamp and sell-side DP
    # actually round above zero.
    assert buy.stamp_duty > 0
    assert sell.dp_charges > 0


@given(qty=qtys, price=prices)
@settings(max_examples=100, deadline=None)
def test_bse_more_expensive_than_nse_per_side(qty, price):
    """BSE exchange charge > NSE, with all other rates equal,
    so BSE total > NSE total for the same trade. Must hold for both sides."""
    e = IndianFeeEngine()
    for side in (OrderSide.BUY, OrderSide.SELL):
        nse = e.calculate(side, qty, price, Exchange.NSE)
        bse = e.calculate(side, qty, price, Exchange.BSE)
        assert bse.exchange_charge >= nse.exchange_charge
        assert bse.total >= nse.total


@given(qty=qtys, price=prices, side=sides, exch=exchanges)
@settings(max_examples=100, deadline=None)
def test_zero_brokerage_config_yields_zero_brokerage(qty, price, side, exch):
    """With brokerage rates all zero, brokerage component is zero."""
    cfg = FeeConfig(brokerage_flat=0.0, brokerage_pct=0.0, brokerage_max=0.0)
    fb = IndianFeeEngine(cfg).calculate(side, qty, price, exch)
    assert fb.brokerage == 0.0


@given(qty=qtys, price=prices, side=sides, exch=exchanges,
       cap=st.floats(min_value=0.01, max_value=100.0,
                     allow_nan=False, allow_infinity=False))
@settings(max_examples=100, deadline=None)
def test_capped_brokerage_never_exceeds_cap(qty, price, side, exch, cap):
    """When ``brokerage_max > 0``, the brokerage component <= cap.

    Tolerance: each component is rounded to paise (half-up) AFTER the
    min(...) call, so a cap of e.g. ₹1.375 can land at ₹1.38 (one paise
    above the raw cap). The relevant property is that the
    *paise-rounded* cap is respected.
    """
    cfg = FeeConfig(brokerage_pct=0.001, brokerage_max=cap)
    fb = IndianFeeEngine(cfg).calculate(side, qty, price, exch)
    # Round the cap to paise the same way the engine does.
    paise_cap = round(cap + 1e-12, 2)
    assert fb.brokerage <= paise_cap + 1e-9, (
        f"brokerage={fb.brokerage} exceeds paise-rounded cap {paise_cap} "
        f"(raw cap={cap})"
    )


@given(qty=qtys, price=prices)
@settings(max_examples=100, deadline=None)
def test_total_is_breakdown_dataclass(qty, price):
    """Calculate always returns a ``FeeBreakdown`` (sanity check on the
    zero-input branch)."""
    fb = IndianFeeEngine().calculate(OrderSide.BUY, qty, price, Exchange.NSE)
    assert isinstance(fb, FeeBreakdown)
