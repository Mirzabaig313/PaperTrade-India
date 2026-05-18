"""Slippage model.

The simplest realistic-enough model: fill at ``last_price * (1 ± bps/10000)``,
where ``bps`` is the configured slippage (default 5 bps = 0.05%). Buys pay
*above* last (impact + half-spread); sells receive *below* last.

This is intentionally simple: a single-knob model that makes paper P&L
land closer to reality without requiring per-symbol bid/ask data. For
liquid NSE mid/large caps with daily-cadence trading, 3–10 bps is
realistic; tune ``SlippageConfig.bps`` to match your strategy's fills.

Limit orders use a different rule:
- BUY limit: fills at ``min(limit_price, last + slippage)``. The slippage
  bound prevents an instant-fill of a stale-aggressive limit at the full
  limit price when the market is well below it; with ``bps=0`` the legacy
  "fill at last_price when market crosses" behavior is preserved.
- SELL limit: fills at ``max(limit_price, last - slippage)``.

Set ``bps=0`` to disable slippage entirely (matches pre-Tier-1 behavior).
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import OrderSide, OrderType


@dataclass(frozen=True)
class SlippageConfig:
    """Configurable slippage model.

    Parameters
    ----------
    bps:
        Basis points of slippage applied symmetrically (1 bp = 0.01%).
        Default 5 bps is conservative for liquid NSE mid/large caps.
    apply_to_limits:
        When True, limit orders also pay slippage relative to last
        price (capped by the limit). Default False — most users want
        limit fills at the limit price.
    """

    bps: float = 5.0
    apply_to_limits: bool = False


def apply_slippage(
    config: SlippageConfig,
    side: OrderSide,
    order_type: OrderType,
    last_price: float,
    limit_price: float | None = None,
) -> float:
    """Compute the simulated fill price for an order leg.

    Parameters
    ----------
    config:
        The slippage configuration in effect.
    side:
        BUY pays slippage above last; SELL pays below.
    order_type:
        MARKET orders always pay slippage; LIMIT orders only when
        ``config.apply_to_limits`` is True.
    last_price:
        The price reported by the price feed.
    limit_price:
        Required only for LIMIT orders. Bounds the fill price.

    Returns
    -------
    The simulated fill price.
    """
    if last_price <= 0:
        # Defensive: never produce a non-positive fill price.
        raise ValueError(f"last_price must be positive, got {last_price}")

    if order_type == OrderType.LIMIT and not config.apply_to_limits:
        # Legacy behavior: fill at limit price when crossed. The
        # broker's watcher passes ``last_price=limit_price`` in that
        # case, so this branch returns it as-is.
        return last_price

    bps = max(0.0, config.bps) / 10000.0
    if side == OrderSide.BUY:
        slipped = last_price * (1.0 + bps)
    else:
        slipped = last_price * (1.0 - bps)

    if order_type == OrderType.LIMIT and limit_price is not None:
        # Cap by the limit: a BUY limit will not fill *above* its limit;
        # a SELL limit will not fill *below* its limit.
        if side == OrderSide.BUY:
            slipped = min(slipped, limit_price)
        else:
            slipped = max(slipped, limit_price)

    return slipped
