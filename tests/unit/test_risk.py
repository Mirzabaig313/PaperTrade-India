"""Tests for the risk engine (unit) and broker integration."""

from __future__ import annotations

import pytest

from papertrade_india import (
    IndiaPaperBroker,
    KillSwitchActive,
    OrderSide,
    RiskConfig,
    RiskContext,
    RiskEngine,
    RiskViolation,
)

# ── RiskEngine in isolation ──────────────────────────────────────────


def _ctx(side=OrderSide.BUY, **overrides):
    base = RiskContext(
        side=side, symbol="RELIANCE", qty=1.0, price=2500.0,
        existing_qty=0.0, existing_avg_cost=0.0, equity=1_000_000.0,
    )
    return RiskContext(**{**base.__dict__, **overrides})


def test_default_config_passes_everything():
    e = RiskEngine()
    e.check(_ctx())  # no exception


def test_kill_switch_via_config():
    e = RiskEngine(RiskConfig(kill_switch=True))
    with pytest.raises(KillSwitchActive):
        e.check(_ctx())


def test_kill_switch_via_env(monkeypatch):
    monkeypatch.setenv("PAPERTRADE_INDIA_KILL_SWITCH", "1")
    e = RiskEngine()
    with pytest.raises(KillSwitchActive):
        e.check(_ctx())


def test_kill_switch_env_truthy_values(monkeypatch):
    for v in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("PAPERTRADE_INDIA_KILL_SWITCH", v)
        assert RiskEngine().is_killed()


def test_kill_switch_env_falsy_values(monkeypatch):
    for v in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("PAPERTRADE_INDIA_KILL_SWITCH", v)
        assert RiskEngine().is_killed() is False


def test_whitelist_blocks_unknown_symbol():
    e = RiskEngine(RiskConfig(symbol_whitelist=frozenset({"INFY", "TCS"})))
    with pytest.raises(RiskViolation, match="not in the whitelist"):
        e.check(_ctx(symbol="RELIANCE"))


def test_whitelist_allows_listed_symbol():
    e = RiskEngine(RiskConfig(symbol_whitelist=frozenset({"RELIANCE"})))
    e.check(_ctx(symbol="RELIANCE"))


def test_empty_whitelist_blocks_everything():
    e = RiskEngine(RiskConfig(symbol_whitelist=frozenset()))
    with pytest.raises(RiskViolation):
        e.check(_ctx())


def test_max_order_notional_blocks_above_cap():
    e = RiskEngine(RiskConfig(max_order_notional=1_000.0))
    with pytest.raises(RiskViolation, match="Order notional"):
        e.check(_ctx(qty=1, price=2_000))


def test_max_order_notional_allows_at_cap():
    e = RiskEngine(RiskConfig(max_order_notional=2_500.0))
    e.check(_ctx(qty=1, price=2_500))  # exactly at cap


def test_max_position_notional_post_fill():
    """Buy 5 at 2500 = 12,500. Cap at 10,000 → reject."""
    e = RiskEngine(RiskConfig(max_position_notional=10_000.0))
    with pytest.raises(RiskViolation, match="Position value"):
        e.check(_ctx(qty=5, price=2500))


def test_max_position_notional_includes_existing_qty():
    """If we already hold 3 and buy 2 more at 100, post = 5 * 100 = 500."""
    e = RiskEngine(RiskConfig(max_position_notional=400.0))
    with pytest.raises(RiskViolation):
        e.check(_ctx(qty=2, price=100, existing_qty=3, existing_avg_cost=100))


def test_position_caps_dont_apply_to_sells():
    """Sells reduce exposure; the post-fill cap shouldn't fire."""
    e = RiskEngine(RiskConfig(max_position_notional=10.0))
    # Sell of 100 shares — would be a huge "post-fill value" if naively
    # computed, but sells don't trigger the post-fill check.
    e.check(_ctx(side=OrderSide.SELL, qty=100, price=1000,
                 existing_qty=200, existing_avg_cost=1000))


def test_max_position_pct_of_equity():
    """Cap at 10%, equity ₹1,000,000 → 100,000. Buy 1 at 200,000 → reject."""
    e = RiskEngine(RiskConfig(max_position_pct_of_equity=0.10))
    with pytest.raises(RiskViolation, match="of equity"):
        e.check(_ctx(qty=1, price=200_000, equity=1_000_000))


def test_kill_switch_evaluated_before_other_checks():
    """Kill switch should fire even when other checks would pass."""
    e = RiskEngine(RiskConfig(
        kill_switch=True,
        symbol_whitelist=frozenset({"RELIANCE"}),
        max_order_notional=1e9,
    ))
    with pytest.raises(KillSwitchActive):
        e.check(_ctx(symbol="RELIANCE", qty=1, price=1))


# ── Broker integration ───────────────────────────────────────────────


@pytest.fixture
def broker_with_risk(tmp_path, stub_provider, price_feed):
    """Helper to spin up a broker with a custom risk_config."""

    def _make(risk_config: RiskConfig):
        return IndiaPaperBroker(
            initial_capital=1_000_000,
            db_path=tmp_path / "risk.db",
            account_id="risk",
            price_feed=price_feed,
            risk_config=risk_config,
            enforce_market_hours=False,
        )

    return _make


def test_broker_kill_switch_blocks_buy(broker_with_risk):
    b = broker_with_risk(RiskConfig(kill_switch=True))
    with pytest.raises(KillSwitchActive):
        b.buy("RELIANCE", 1)


def test_broker_kill_switch_blocks_sell(broker_with_risk):
    """Even sells (which reduce exposure) are blocked when killed."""
    b = broker_with_risk(RiskConfig(kill_switch=True))
    with pytest.raises(KillSwitchActive):
        b.sell("RELIANCE", 1)


def test_broker_whitelist_blocks_off_list(broker_with_risk, stub_provider):
    b = broker_with_risk(RiskConfig(symbol_whitelist=frozenset({"INFY"})))
    stub_provider.set("RELIANCE", 1000)
    with pytest.raises(RiskViolation):
        b.buy("RELIANCE", 1)


def test_broker_max_order_notional(broker_with_risk, stub_provider):
    b = broker_with_risk(RiskConfig(max_order_notional=1_000.0))
    stub_provider.set("RELIANCE", 2500)
    with pytest.raises(RiskViolation):
        b.buy("RELIANCE", 1)


def test_broker_position_pct_of_equity(broker_with_risk, stub_provider):
    """5% of ₹1M = ₹50K. A 100-share buy at ₹600 = ₹60K → reject."""
    b = broker_with_risk(RiskConfig(max_position_pct_of_equity=0.05))
    stub_provider.set("RELIANCE", 600)
    with pytest.raises(RiskViolation):
        b.buy("RELIANCE", 100)


def test_broker_risk_failure_does_not_persist_anything(
    broker_with_risk, stub_provider,
):
    """A risk-rejected order leaves zero rows behind."""
    b = broker_with_risk(RiskConfig(kill_switch=True))
    stub_provider.set("RELIANCE", 1000)
    with pytest.raises(KillSwitchActive):
        b.buy("RELIANCE", 1)
    # No order, no position, no cash drift.
    assert b.get_orders() == []
    assert b.get_position("RELIANCE") is None
    assert b.get_account().cash == 1_000_000


def test_broker_default_has_no_risk_controls(tmp_path, stub_provider, price_feed):
    """A broker constructed without ``risk_config`` accepts everything
    (legacy behavior preserved)."""
    stub_provider.set("RELIANCE", 2500)
    b = IndiaPaperBroker(
        initial_capital=1_000_000,
        db_path=tmp_path / "default.db",
        account_id="d",
        price_feed=price_feed,
        enforce_market_hours=False,
    )
    b.buy("RELIANCE", 1)  # no exception
