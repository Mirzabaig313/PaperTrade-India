"""Unit tests for ``PartialFillConfig.fill_qty``."""

from __future__ import annotations

from papertrade_india import PartialFillConfig


def test_disabled_returns_full_remaining():
    cfg = PartialFillConfig(enabled=False)
    assert cfg.fill_qty(remaining_qty=100) == 100


def test_enabled_with_no_caps_returns_full_remaining():
    cfg = PartialFillConfig(enabled=True)
    # No caps configured. fill_qty floors to whole shares.
    assert cfg.fill_qty(100) == 100


def test_max_per_tick_truncates():
    cfg = PartialFillConfig(enabled=True, max_per_tick=25)
    assert cfg.fill_qty(100) == 25


def test_max_pct_per_tick_truncates():
    cfg = PartialFillConfig(enabled=True, max_pct_per_tick=0.30)
    # 30% of 100 = 30
    assert cfg.fill_qty(100) == 30


def test_both_caps_take_the_smaller():
    cfg = PartialFillConfig(
        enabled=True, max_per_tick=20, max_pct_per_tick=0.50,
    )
    # Smaller of 20 or 50.
    assert cfg.fill_qty(100) == 20


def test_min_fill_qty_floors():
    """When the would-be slice is below min_fill_qty, return 0 (skip)."""
    cfg = PartialFillConfig(enabled=True, max_per_tick=2, min_fill_qty=5)
    assert cfg.fill_qty(100) == 0


def test_below_min_fill_when_remaining_small():
    """If remaining is below ``min_fill_qty`` we fill the whole remainder
    rather than parking it forever — otherwise small or last-sliver
    orders would never complete."""
    cfg = PartialFillConfig(enabled=True, min_fill_qty=10)
    assert cfg.fill_qty(5) == 5
    # Equal to ``min_fill_qty`` also fills in one shot.
    assert cfg.fill_qty(10) == 10


def test_fractional_caps_floored():
    """Indian equity is whole-share. A 33.3% cap on 10 = 3.33 → 3."""
    cfg = PartialFillConfig(enabled=True, max_pct_per_tick=0.333)
    assert cfg.fill_qty(10) == 3


def test_zero_or_negative_remaining_returns_input():
    cfg = PartialFillConfig(enabled=True, max_per_tick=10)
    assert cfg.fill_qty(0) == 0
    # Negative (shouldn't occur in practice) returns as-is to avoid
    # turning a no-op into a filled slug.
    assert cfg.fill_qty(-5) == -5
