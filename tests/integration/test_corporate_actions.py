"""Tests for corporate-action handling: splits and cash dividends."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


# ── Splits ────────────────────────────────────────────────────────────


def test_two_for_one_split_doubles_qty_halves_avg(broker, stub_provider):
    """2:1 split: 10 @ ₹2000 → 20 @ ₹1000. Cost basis preserved."""
    stub_provider.set("RELIANCE", 2000)
    buy = broker.buy("RELIANCE", 10)
    pre = broker.get_position("RELIANCE")
    pre_basis = pre.qty * pre.avg_cost  # qty * avg_cost = full basis

    broker.apply_split("RELIANCE", ratio_num=2, ratio_den=1)

    post = broker.get_position("RELIANCE")
    assert post.qty == pytest.approx(20.0)
    assert post.avg_cost == pytest.approx(pre.avg_cost / 2)
    # Total basis preserved.
    assert post.qty * post.avg_cost == pytest.approx(pre_basis)
    # Realized P&L unchanged (split is cash-neutral).
    assert broker.get_account().realized_pl_total == 0
    # Cash unchanged.
    expected_cash = 1_000_000 - 10 * 2000 - buy.fees_paid
    assert broker.get_account().cash == pytest.approx(expected_cash)


def test_one_for_one_bonus_is_two_for_one_split(broker, stub_provider):
    """A 1:1 bonus issue (one new share per share held) is the same math
    as a 2:1 split."""
    stub_provider.set("INFY", 1500)
    buy = broker.buy("INFY", 5)
    pre = broker.get_position("INFY")
    pre_basis = pre.qty * pre.avg_cost  # = 5*1500 + buy_fees

    broker.apply_split("INFY", ratio_num=2, ratio_den=1, notes="1:1 bonus")
    pos = broker.get_position("INFY")

    assert pos.qty == pytest.approx(10.0)
    # Avg cost halves; total basis preserved.
    assert pos.avg_cost == pytest.approx(pre.avg_cost / 2, abs=0.01)
    assert pos.qty * pos.avg_cost == pytest.approx(pre_basis, abs=0.01)
    # Free shares: extra qty came at zero marginal cost (the
    # buy.fees_paid is already inside pre_basis).
    assert buy.fees_paid > 0  # sanity


def test_reverse_split_collapses_qty(broker, stub_provider):
    """1:5 reverse split: 100 @ ₹10 → 20 @ ₹50."""
    stub_provider.set("PENNY", 10)
    broker.buy("PENNY", 100)
    broker.apply_split("PENNY", ratio_num=1, ratio_den=5)
    pos = broker.get_position("PENNY")
    assert pos.qty == pytest.approx(20.0)
    # avg_cost was ~10 (plus tiny buy fees); after 1:5 reverse → ~50.
    assert pos.avg_cost > 49


def test_split_without_holding_records_action_only(broker):
    """A split on an unheld symbol records the audit row without error."""
    action_id = broker.apply_split("UNHELD", ratio_num=2, ratio_den=1)
    assert action_id is not None
    assert broker.get_position("UNHELD") is None


def test_split_invalid_ratio_raises(broker):
    with pytest.raises(ValueError):
        broker.apply_split("RELIANCE", ratio_num=0)
    with pytest.raises(ValueError):
        broker.apply_split("RELIANCE", ratio_num=2, ratio_den=-1)


def test_split_does_not_violate_cash_invariant(broker, stub_provider):
    stub_provider.set("RELIANCE", 1000)
    broker.buy("RELIANCE", 4)
    broker.apply_split("RELIANCE", ratio_num=2, ratio_den=1)
    assert broker.verify_cash_invariant()


# ── Dividends ────────────────────────────────────────────────────────


def test_dividend_credits_cash_proportionally(broker, stub_provider):
    """Dividend ₹50/share × 10 shares = ₹500 credit."""
    stub_provider.set("ITC", 400)
    broker.buy("ITC", 10)
    cash_before = broker.get_account().cash
    broker.apply_dividend("ITC", amount_per_share=50.0)
    cash_after = broker.get_account().cash
    assert cash_after - cash_before == pytest.approx(500.0)


def test_dividend_records_ledger_row(broker, stub_provider):
    stub_provider.set("ITC", 400)
    broker.buy("ITC", 10)
    broker.apply_dividend("ITC", amount_per_share=12.5, notes="Q4 FY26")
    # Most recent ledger row should be the dividend.
    movements = broker.get_cash_movements()
    div = next(m for m in movements if m.reason == "dividend")
    assert div.amount == pytest.approx(125.0)
    assert div.symbol == "ITC"
    assert div.notes is not None
    assert "12.5" in div.notes


def test_dividend_when_no_holding_records_no_credit(broker):
    """Dividend on an unheld symbol is recorded for audit but credits
    nothing — no shares to pay dividends on."""
    cash_before = broker.get_account().cash
    broker.apply_dividend("UNHELD", amount_per_share=10.0)
    cash_after = broker.get_account().cash
    assert cash_after == cash_before


def test_dividend_does_not_violate_cash_invariant(broker, stub_provider):
    stub_provider.set("ITC", 400)
    broker.buy("ITC", 5)
    broker.apply_dividend("ITC", amount_per_share=10.0)
    assert broker.verify_cash_invariant()


def test_invalid_dividend_amount_raises(broker):
    with pytest.raises(ValueError):
        broker.apply_dividend("RELIANCE", amount_per_share=0)
    with pytest.raises(ValueError):
        broker.apply_dividend("RELIANCE", amount_per_share=-5.0)


def test_dividend_does_not_change_realized_pl(broker, stub_provider):
    """Dividends are income, not P&L — they don't roll into
    ``realized_pl_total`` (which is reserved for trading P&L)."""
    stub_provider.set("ITC", 400)
    broker.buy("ITC", 10)
    pl_before = broker.get_account().realized_pl_total
    broker.apply_dividend("ITC", amount_per_share=20.0)
    pl_after = broker.get_account().realized_pl_total
    assert pl_after == pl_before


# ── Combined: split → dividend ──────────────────────────────────────


def test_dividend_after_split_uses_post_split_qty(broker, stub_provider):
    """A dividend declared after a split pays on the post-split share count."""
    stub_provider.set("RELIANCE", 2000)
    broker.buy("RELIANCE", 10)
    broker.apply_split("RELIANCE", ratio_num=2, ratio_den=1)
    cash_before = broker.get_account().cash
    broker.apply_dividend("RELIANCE", amount_per_share=5.0)
    cash_after = broker.get_account().cash
    # Post-split qty = 20; dividend = 20 * 5 = ₹100.
    assert cash_after - cash_before == pytest.approx(100.0)
