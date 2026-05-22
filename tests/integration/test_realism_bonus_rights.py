"""Integration tests: bonus issues and rights issues."""

from __future__ import annotations

import pytest

from papertrade_india import InsufficientFundsError


def test_one_for_one_bonus_doubles_qty(broker) -> None:
    """1:1 bonus → holdings double, avg_cost halves, total basis preserved."""
    broker.buy("RELIANCE", 10)
    pos_before = broker.get_position("RELIANCE")
    basis_before = pos_before.qty * pos_before.avg_cost

    action_id = broker.apply_bonus("RELIANCE", ratio_num=1, ratio_den=1)
    assert action_id

    pos_after = broker.get_position("RELIANCE")
    assert pos_after.qty == 20
    # avg_cost halved (within rounding)
    assert pos_after.avg_cost == pytest.approx(pos_before.avg_cost / 2.0)
    # Total basis preserved.
    assert pos_after.qty * pos_after.avg_cost == pytest.approx(basis_before)


def test_one_for_two_bonus_grows_one_point_five_x(broker) -> None:
    """1:2 bonus → 1 new share per 2 held → holdings × 1.5."""
    broker.buy("RELIANCE", 10)
    broker.apply_bonus("RELIANCE", ratio_num=1, ratio_den=2)
    pos = broker.get_position("RELIANCE")
    assert pos.qty == 15


def test_bonus_with_no_holding_records_action(broker) -> None:
    """The audit row goes in even when the holder has no position."""
    action_id = broker.apply_bonus("RELIANCE", ratio_num=1, ratio_den=1)
    assert action_id
    # No position created.
    assert broker.get_position("RELIANCE") is None


def test_bonus_invalid_ratios_raise(broker) -> None:
    with pytest.raises(ValueError):
        broker.apply_bonus("RELIANCE", ratio_num=0, ratio_den=1)
    with pytest.raises(ValueError):
        broker.apply_bonus("RELIANCE", ratio_num=1, ratio_den=0)
    with pytest.raises(ValueError):
        broker.apply_bonus("RELIANCE", ratio_num=-1, ratio_den=1)


# ── Rights ───────────────────────────────────────────────────────────


def test_rights_records_action_without_subscribing(broker) -> None:
    """Default behavior is to record the rights but let them lapse."""
    broker.buy("RELIANCE", 10)
    pos_before = broker.get_position("RELIANCE")
    cash_before = broker.get_account().cash

    action_id = broker.apply_rights(
        "RELIANCE", ratio_num=1, ratio_den=2,
        subscription_price=2000.00,
    )
    assert action_id

    # No qty change, no cash change.
    pos_after = broker.get_position("RELIANCE")
    assert pos_after.qty == pos_before.qty
    assert pos_after.avg_cost == pos_before.avg_cost
    assert broker.get_account().cash == cash_before


def test_rights_subscribed_buys_new_shares(broker) -> None:
    """subscribe=True buys the entitlement at the subscription price."""
    broker.buy("RELIANCE", 10)
    cash_before = broker.get_account().cash

    broker.apply_rights(
        "RELIANCE", ratio_num=1, ratio_den=2,
        subscription_price=2000.00, subscribe=True,
    )
    # Entitlement: floor(10 * 1 / 2) = 5 new shares.
    pos = broker.get_position("RELIANCE")
    assert pos.qty == 15
    # Cash debited by 5 × 2000 = 10,000.
    cost = 5 * 2000.00
    assert broker.get_account().cash == pytest.approx(cash_before - cost)


def test_rights_avg_cost_reflects_blended_basis(broker) -> None:
    """After subscription, avg_cost should be (old basis + sub cost) / new qty."""
    broker.buy("RELIANCE", 10)
    pos_before = broker.get_position("RELIANCE")
    old_basis = pos_before.qty * pos_before.avg_cost  # 10 × ~2500 = 25,000

    broker.apply_rights(
        "RELIANCE", ratio_num=1, ratio_den=2,
        subscription_price=2000.00, subscribe=True,
    )
    pos = broker.get_position("RELIANCE")
    expected_basis = old_basis + 5 * 2000.00
    assert pos.qty * pos.avg_cost == pytest.approx(expected_basis)


def test_rights_insufficient_funds(broker) -> None:
    """Subscription at a price the account can't afford raises."""
    # Spend most of the cash first.
    broker.buy("RELIANCE", 300)  # ~750k of 1M
    with pytest.raises(InsufficientFundsError):
        broker.apply_rights(
            "RELIANCE",
            ratio_num=1, ratio_den=2,
            subscription_price=10_000.00,  # Way above market
            subscribe=True,
        )


def test_rights_zero_entitlement_records_no_position_change(broker) -> None:
    """Holdings smaller than the ratio denominator floor to zero new shares."""
    broker.buy("RELIANCE", 1)  # holds 1
    broker.apply_rights(
        "RELIANCE", ratio_num=1, ratio_den=10,  # 1:10
        subscription_price=2000.00, subscribe=True,
    )
    # Entitlement: floor(1 × 1 / 10) = 0. No position change.
    pos = broker.get_position("RELIANCE")
    assert pos.qty == 1


def test_rights_invalid_args_raise(broker) -> None:
    with pytest.raises(ValueError):
        broker.apply_rights("X", ratio_num=0, ratio_den=2, subscription_price=100)
    with pytest.raises(ValueError):
        broker.apply_rights("X", ratio_num=1, ratio_den=2, subscription_price=0)
    with pytest.raises(ValueError):
        broker.apply_rights("X", ratio_num=1, ratio_den=2, subscription_price=-100)


# ── Bonus / rights are recorded with distinct action_types ───────────


def test_bonus_and_rights_recorded_with_distinct_action_types(broker) -> None:
    broker.buy("RELIANCE", 10)
    broker.apply_bonus("RELIANCE", 1, 1)
    broker.apply_rights(
        "RELIANCE", 1, 5, subscription_price=2000.00, subscribe=False,
    )
    # Pull from the corporate_actions table directly.
    with broker.persistence.read() as conn:
        types = [
            r["action_type"]
            for r in conn.execute(
                "SELECT action_type FROM corporate_actions "
                "WHERE symbol = 'RELIANCE' ORDER BY applied_at",
            ).fetchall()
        ]
    assert "bonus" in types
    assert "rights" in types
