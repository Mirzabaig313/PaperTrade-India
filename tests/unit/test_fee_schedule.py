"""Tests for date-versioned fee schedules."""

from __future__ import annotations

from datetime import date

import pytest

from papertrade_india import (
    Exchange,
    FeeConfig,
    FeeSchedule,
    IndianFeeEngine,
    IndiaPaperBroker,
    OrderSide,
)

# ── FeeSchedule in isolation ─────────────────────────────────────────


def test_default_only_returns_default_for_any_date():
    fs = FeeSchedule(default=FeeConfig(stt_pct_buy=0.001))
    assert fs.config_on(date(2020, 1, 1)).stt_pct_buy == 0.001
    assert fs.config_on(date(2030, 1, 1)).stt_pct_buy == 0.001


def test_picks_latest_effective_from_at_or_before_date():
    pre = FeeConfig(stt_pct_buy=0.0010)
    apr_2025 = FeeConfig(stt_pct_buy=0.00125)
    apr_2026 = FeeConfig(stt_pct_buy=0.00150)
    fs = FeeSchedule(default=pre, effective_from={
        date(2025, 4, 1): apr_2025,
        date(2026, 4, 1): apr_2026,
    })

    # Pre-2025: default
    assert fs.config_on(date(2024, 12, 31)) is pre
    # 2025-04-01 onwards (inclusive): 2025 schedule
    assert fs.config_on(date(2025, 4, 1)) is apr_2025
    assert fs.config_on(date(2025, 12, 31)) is apr_2025
    # 2026-04-01 onwards: 2026 schedule
    assert fs.config_on(date(2026, 4, 1)) is apr_2026
    assert fs.config_on(date(2030, 1, 1)) is apr_2026


def test_effective_from_dates_can_be_unordered():
    """The schedule sorts internally — input order doesn't matter."""
    a = FeeConfig(stt_pct_buy=0.001)
    b = FeeConfig(stt_pct_buy=0.002)
    fs = FeeSchedule(default=FeeConfig(), effective_from={
        date(2026, 4, 1): b,
        date(2025, 4, 1): a,
    })
    assert fs.config_on(date(2025, 6, 1)) is a
    assert fs.config_on(date(2026, 6, 1)) is b


def test_effective_on_exact_boundary_date():
    cfg = FeeConfig(stt_pct_buy=0.001)
    fs = FeeSchedule(default=FeeConfig(), effective_from={date(2026, 1, 1): cfg})
    assert fs.config_on(date(2026, 1, 1)) is cfg


# ── Broker integration ──────────────────────────────────────────────


def test_broker_accepts_fee_schedule(tmp_path, price_feed, stub_provider):
    """Broker constructed with a FeeSchedule uses today's config."""
    pre = FeeConfig(stt_pct_buy=0.0)
    new = FeeConfig(stt_pct_buy=0.005)  # 5x default
    fs = FeeSchedule(default=pre, effective_from={date(1900, 1, 1): new})
    # 1900-01-01 is well in the past, so the "new" config is always active.

    stub_provider.set("RELIANCE", 1000)
    broker = IndiaPaperBroker(
        initial_capital=1_000_000,
        db_path=tmp_path / "sched.db",
        account_id="sched",
        price_feed=price_feed,
        fee_config=fs,  # FeeSchedule, not FeeConfig
        enforce_market_hours=False,
    )
    order = broker.buy("RELIANCE", 1)
    # STT alone at 0.5% = ₹5 on a ₹1000 buy
    assert order.fees_paid >= 5.0


def test_broker_accepts_bare_fee_config(tmp_path, price_feed, stub_provider):
    """Bare FeeConfig still works (auto-wrapped in a single-entry schedule)."""
    cfg = FeeConfig(stt_pct_buy=0.002)
    stub_provider.set("RELIANCE", 1000)
    broker = IndiaPaperBroker(
        initial_capital=1_000_000,
        db_path=tmp_path / "bare.db",
        account_id="bare",
        price_feed=price_feed,
        fee_config=cfg,
        enforce_market_hours=False,
    )
    order = broker.buy("RELIANCE", 1)
    # STT alone at 0.2% = ₹2 on a ₹1000 buy
    assert order.fees_paid >= 2.0


def test_fee_engine_property_uses_today(tmp_path, price_feed):
    """``broker.fee_engine`` is a backwards-compat shim returning the
    engine for today (IST)."""
    cfg = FeeConfig(stt_pct_buy=0.003)
    broker = IndiaPaperBroker(
        initial_capital=1_000_000,
        db_path=tmp_path / "fe.db",
        account_id="fe",
        price_feed=price_feed,
        fee_config=cfg,
        enforce_market_hours=False,
    )
    fb = broker.fee_engine.calculate(
        OrderSide.BUY, qty=10, price=1000, exchange=Exchange.NSE,
    )
    # 0.3% STT on 10000 turnover = ₹30
    assert fb.stt == pytest.approx(30.0, abs=0.01)


def test_pre_history_falls_back_to_default():
    """A schedule with future-dated overrides falls back to ``default``
    for past dates."""
    pre = FeeConfig(stt_pct_buy=0.001)
    fs = FeeSchedule(
        default=pre,
        effective_from={date(2027, 4, 1): FeeConfig(stt_pct_buy=0.005)},
    )
    cfg = fs.config_on(date(2026, 1, 1))
    assert cfg is pre


def test_engine_built_from_schedule_calculates_correctly():
    """Sanity: the engine produces a sane breakdown for a config picked
    out of a schedule."""
    new_cfg = FeeConfig(stt_pct_buy=0.002)
    fs = FeeSchedule(default=FeeConfig(), effective_from={
        date(2026, 1, 1): new_cfg,
    })
    cfg = fs.config_on(date(2026, 6, 1))
    fb = IndianFeeEngine(cfg).calculate(
        OrderSide.BUY, qty=10, price=1000, exchange=Exchange.NSE,
    )
    # 0.2% STT on 10000 = ₹20
    assert fb.stt == pytest.approx(20.0, abs=0.01)
