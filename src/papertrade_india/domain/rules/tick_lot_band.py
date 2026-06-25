"""Tick-size, lot-size, and daily price-band validation rules.

These are pure domain rules. They take a price (or a quantity, or a
prev close) and either return silently or raise a domain exception. No
I/O, no state, no infrastructure dependencies.

Three rules live here, one per axis the real exchange enforces:

1. **Tick size** — limit / stop prices must be a multiple of the
   scrip's tick (₹0.05 is the NSE default for cash equity).
2. **Lot size** — orders must be a whole multiple of the lot. Cash
   equities are mostly lot=1; F&O contracts use real lots.
3. **Daily price band** — orders that would fill outside ``prev_close ×
   (1 ± band_pct)`` are rejected. NSE bands are 2/5/10/20% per scrip,
   set by the exchange; this module just enforces whatever the broker
   passes in.

Configuration knobs and per-symbol overrides live with the broker; this
module is intentionally stateless.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from ..exceptions import (
    LotSizeViolation,
    PriceBandViolation,
    TickSizeViolation,
)


@dataclass(frozen=True)
class MicrostructureConfig:
    """Toggles for tick / lot / band enforcement.

    All three default to ``True`` because rejecting them is what makes
    fills land where a real broker would land them. Set the field to
    ``False`` if you have a backtest that pre-dates a band change and
    you want to suppress the check.
    """

    enforce_tick_size: bool = True
    enforce_lot_size: bool = True
    enforce_price_band: bool = True

    # Default tick size / band when the symbol master has no override.
    # NSE cash equity is ₹0.05 across nearly all scrips; band defaults
    # to a permissive 20% so the legacy "anything fills" tests don't
    # break.
    default_tick_size: float = 0.05
    default_lot_size: int = 1
    default_band_pct: float = 0.20  # ±20%


def round_to_tick(price: float, tick: float) -> float:
    """Round ``price`` to the nearest multiple of ``tick``.

    Uses ``Decimal`` to avoid the binary-float traps that put 2940.05 at
    2940.0499999999999. Returns ``0.0`` when ``tick`` is ``0`` or the
    input is non-positive (callers should validate before calling).
    """
    if tick <= 0 or price <= 0:
        return 0.0
    p = Decimal(str(price))
    t = Decimal(str(tick))
    n = (p / t).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return float(n * t)


def is_aligned_to_tick(price: float, tick: float, atol: float = 1e-6) -> bool:
    """True when ``price`` is within ``atol`` paise of a tick boundary."""
    if tick <= 0:
        return True  # tick disabled
    rounded = round_to_tick(price, tick)
    return abs(price - rounded) <= atol


def validate_tick(price: float | None, tick: float, label: str) -> None:
    """Raise :class:`TickSizeViolation` if a price isn't tick-aligned."""
    if price is None or tick <= 0:
        return
    if not is_aligned_to_tick(price, tick):
        raise TickSizeViolation(
            f"{label} ₹{price:.6f} is not aligned to tick size ₹{tick:.4f}. "
            f"Nearest valid: ₹{round_to_tick(price, tick):.4f}",
        )


def validate_lot(qty: float, lot: int) -> None:
    """Raise :class:`LotSizeViolation` if ``qty`` isn't a multiple of ``lot``."""
    if lot <= 1:
        return
    # Reject fractional qty up front (we don't model MF units here).
    if abs(qty - round(qty)) > 1e-9:
        raise LotSizeViolation(
            f"qty {qty} is fractional; lot size {lot} requires whole shares.",
        )
    rounded_qty = int(round(qty))
    if rounded_qty % lot != 0:
        nearest_down = (rounded_qty // lot) * lot
        nearest_up = nearest_down + lot
        raise LotSizeViolation(
            f"qty {rounded_qty} is not a multiple of lot size {lot}. "
            f"Use {nearest_down} or {nearest_up}.",
        )


def validate_band(
    price: float,
    prev_close: float | None,
    band_pct: float,
) -> None:
    """Raise :class:`PriceBandViolation` if ``price`` is outside the band.

    ``prev_close=None`` skips the check (e.g. first-day listing or the
    simulator hasn't seen a close yet).
    """
    if prev_close is None or prev_close <= 0 or band_pct <= 0:
        return
    upper = prev_close * (1.0 + band_pct)
    lower = prev_close * (1.0 - band_pct)
    if price > upper or price < lower:
        raise PriceBandViolation(
            f"Price ₹{price:.4f} is outside the daily band "
            f"[₹{lower:.4f}, ₹{upper:.4f}] (prev close ₹{prev_close:.4f}, "
            f"band ±{band_pct * 100:.1f}%).",
        )


__all__ = [
    "MicrostructureConfig",
    "round_to_tick",
    "is_aligned_to_tick",
    "validate_tick",
    "validate_lot",
    "validate_band",
]
