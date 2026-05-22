"""Tests for the immutable cash-movement ledger.

Two flavors:

1. **Direct** — verify rows land with the right reasons and signs.
2. **Invariant** — assert ``account.cash == sum(cash_movements.amount)``
   after a series of operations. This is the core auditability claim.
"""

from __future__ import annotations

import pytest

from papertrade_india import (
    IndiaPaperBroker,
    LatencyConfig,
    OrderBookConfig,
    RejectionConfig,
    SettlementConfig,
    SettlementMode,
)

pytestmark = pytest.mark.integration


# ── Direct row checks ────────────────────────────────────────────────


def test_initial_capital_recorded_on_account_creation(broker):
    movements = broker.get_cash_movements()
    assert len(movements) == 1
    m = movements[0]
    assert m.reason == "initial_capital"
    assert m.amount == 1_000_000.0
    assert m.notes == "Account opened"


def test_buy_records_principal_and_fees(broker, stub_provider):
    stub_provider.set("RELIANCE", 1000)
    order = broker.buy("RELIANCE", 5)

    movements = broker.get_cash_movements()
    # initial_capital + buy_principal + buy_fees
    assert len(movements) == 3
    reasons = [m.reason for m in movements]
    assert "buy_principal" in reasons
    assert "buy_fees" in reasons

    by_reason = {m.reason: m for m in movements}
    assert by_reason["buy_principal"].amount == pytest.approx(-5000.0)
    assert by_reason["buy_principal"].order_id == order.id
    assert by_reason["buy_principal"].symbol == "RELIANCE"
    assert by_reason["buy_fees"].amount == pytest.approx(-order.fees_paid)


def test_sell_records_principal_and_fees(broker, stub_provider):
    stub_provider.set("RELIANCE", 1000)
    broker.buy("RELIANCE", 5)
    sell = broker.sell("RELIANCE", 5)

    movements = broker.get_cash_movements()
    sell_principal = next(
        m for m in movements if m.reason == "sell_principal"
    )
    sell_fees = next(m for m in movements if m.reason == "sell_fees")

    assert sell_principal.amount == pytest.approx(5000.0)
    assert sell_principal.order_id == sell.id
    assert sell_fees.amount == pytest.approx(-sell.fees_paid)


def test_buy_with_zero_fees_skips_fee_row(tmp_path, price_feed, stub_provider):
    """A configured zero-fee account writes only the principal row."""
    from papertrade_india import FeeConfig

    stub_provider.set("RELIANCE", 1000)
    # Override every fee component to zero.
    no_fee = FeeConfig(
        brokerage_flat=0, brokerage_pct=0, brokerage_max=0,
        stt_pct_buy=0, stt_pct_sell=0,
        exchange_charge_nse=0, exchange_charge_bse=0,
        gst_pct=0, sebi_charges_pct=0,
        stamp_duty_pct=0, dp_charge_per_sell=0,
    )
    broker = IndiaPaperBroker(
        initial_capital=100_000,
        db_path=tmp_path / "nofee.db",
        account_id="nofee",
        price_feed=price_feed,
        fee_config=no_fee,
        enforce_market_hours=False,
    )
    broker.buy("RELIANCE", 1)
    movements = broker.get_cash_movements()
    reasons = [m.reason for m in movements]
    assert "buy_principal" in reasons
    assert "buy_fees" not in reasons  # no fees row when fees == 0


# ── Invariant: cash == sum(movements) ───────────────────────────────


def test_invariant_after_initial_open(broker):
    assert broker.verify_cash_invariant()


def test_invariant_after_single_buy(broker, stub_provider):
    stub_provider.set("RELIANCE", 1000)
    broker.buy("RELIANCE", 1)
    assert broker.verify_cash_invariant()


def test_invariant_after_round_trip(broker, stub_provider):
    stub_provider.set("RELIANCE", 1000)
    broker.buy("RELIANCE", 5)
    stub_provider.set("RELIANCE", 1100)
    broker.sell("RELIANCE", 5)
    assert broker.verify_cash_invariant()


def test_invariant_after_partial_sells(broker, stub_provider):
    stub_provider.set("INFY", 1500)
    broker.buy("INFY", 10)
    broker.sell("INFY", 3)
    broker.sell("INFY", 2)
    assert broker.verify_cash_invariant()


def test_invariant_after_failed_buy(broker, stub_provider):
    """A rejected buy must leave the ledger unchanged."""
    from papertrade_india import InsufficientFundsError

    stub_provider.set("RELIANCE", 10_000_000)  # way too expensive
    movements_before = len(broker.get_cash_movements())

    with pytest.raises(InsufficientFundsError):
        broker.buy("RELIANCE", 1)

    movements_after = len(broker.get_cash_movements())
    assert movements_before == movements_after
    assert broker.verify_cash_invariant()


def test_invariant_after_reset(broker, stub_provider):
    stub_provider.set("RELIANCE", 1000)
    broker.buy("RELIANCE", 5)
    broker.sell("RELIANCE", 5)
    broker.reset(initial_capital=2_500_000)
    assert broker.verify_cash_invariant()
    movements = broker.get_cash_movements()
    # After reset, only the new initial_capital row remains.
    assert len(movements) == 1
    assert movements[0].reason == "initial_capital"
    assert movements[0].amount == 2_500_000


def test_invariant_after_reset_keeping_cash(broker, stub_provider):
    """Reset without ``initial_capital`` should re-seed the ledger to
    match the existing cash balance."""
    stub_provider.set("INFY", 1500)
    broker.buy("INFY", 1)
    broker.sell("INFY", 1)
    cash = broker.get_account().cash
    broker.reset()
    assert broker.verify_cash_invariant()
    assert broker.get_account().cash == pytest.approx(cash)


# ── Stress: invariant under many random ops ─────────────────────────


def test_invariant_under_random_ops(tmp_path, price_feed, stub_provider):
    """Run a small fuzz to confirm no path mutates cash without the ledger."""
    import random

    from papertrade_india import (
        InsufficientFundsError,
        InsufficientSharesError,
    )

    universe = {"RELIANCE": 2500, "INFY": 1800, "TCS": 4000}
    for s, p in universe.items():
        stub_provider.set(s, p)

    broker = IndiaPaperBroker(
        initial_capital=2_000_000,
        db_path=tmp_path / "fuzz.db",
        account_id="ledger-fuzz",
        price_feed=price_feed,
        enforce_market_hours=False,
        # Property fuzz tests do same-day round-trips of various sizes.
        # Realism layers (T+1, book impact) would trip those without
        # adding signal to the cash-invariant check we actually care
        # about. Disable them here.
        order_book_config=OrderBookConfig(enabled=False),
        settlement_config=SettlementConfig(mode=SettlementMode.T_PLUS_0),
        latency_config=LatencyConfig(submit_ms_mean=0.0),
        rejection_config=RejectionConfig(rate=0.0),
        mark_to_bid=False,
    )

    rng = random.Random(0xF00D)
    for _ in range(200):
        sym = rng.choice(list(universe))
        try:
            if rng.random() < 0.5:
                broker.buy(sym, rng.randint(1, 3))
            else:
                broker.sell(sym, rng.randint(1, 3))
        except (InsufficientFundsError, InsufficientSharesError):
            continue

    assert broker.verify_cash_invariant(), (
        f"cash drift after fuzz: cash={broker.get_account().cash}"
    )
