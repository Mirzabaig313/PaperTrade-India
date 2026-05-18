"""Pre-trade risk controls.

A small set of guardrails an autonomous agent actually needs:

- **Kill switch**: a single flag (or env var ``PAPERTRADE_INDIA_KILL_SWITCH=1``)
  that rejects every new order. Useful when something has clearly gone
  wrong and you want trading to stop immediately without restarting the
  process or recreating the broker.
- **Symbol whitelist**: when set, only listed symbols are tradeable.
  ``None`` (the default) allows everything. Empty list = allow nothing,
  for a "manual approval" workflow.
- **Per-order notional cap**: max INR value of any single order. Prevents
  fat-finger fills.
- **Per-position notional cap**: max INR value of any single position.
  Enforced post-fill (hypothetical).
- **Per-position equity-fraction cap**: position cannot exceed X% of
  current equity. Same enforcement timing as the absolute cap.

Each control is independent; pass ``None`` (or omit) any field to disable.

Risk checks run inside ``IndiaPaperBroker._submit_order`` *before* market
hours / price feed / DB writes. A violation raises ``RiskViolation`` (or
``KillSwitchActive`` for the kill switch). No state is mutated.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from .exceptions import KillSwitchActive, RiskViolation
from .models import OrderSide

logger = logging.getLogger(__name__)


_KILL_SWITCH_ENV = "PAPERTRADE_INDIA_KILL_SWITCH"


@dataclass(frozen=True)
class RiskConfig:
    """Pre-trade risk-control configuration.

    All caps are in INR unless suffixed ``_pct``.
    """

    # Single-flip kill switch. Also honored via env var
    # PAPERTRADE_INDIA_KILL_SWITCH=1.
    kill_switch: bool = False

    # Allow-list of tradeable symbols. ``None`` = unrestricted.
    symbol_whitelist: frozenset[str] | None = None

    # Reject any single order with notional (qty * price) above this cap.
    # ``None`` = no cap.
    max_order_notional: float | None = None

    # Reject the order if the resulting position's market value would
    # exceed this absolute INR cap.
    max_position_notional: float | None = None

    # Reject the order if the resulting position's market value would
    # exceed this fraction of *current* account equity. 0.10 = 10%.
    max_position_pct_of_equity: float | None = None


@dataclass
class RiskContext:
    """Per-order context passed to ``RiskEngine.check``.

    Held as a regular dataclass (not frozen) for ergonomic construction
    in the broker.
    """

    side: OrderSide
    symbol: str
    qty: float
    price: float
    # Currently held qty in this symbol on this account (0 if none).
    existing_qty: float = 0.0
    existing_avg_cost: float = 0.0
    # Account state used by equity-fraction cap.
    equity: float = 0.0


class RiskEngine:
    """Stateless pre-trade risk evaluator."""

    def __init__(self, config: RiskConfig | None = None) -> None:
        self.config = config or RiskConfig()

    # ── Kill switch ────────────────────────────────────────────────────

    def is_killed(self) -> bool:
        if self.config.kill_switch:
            return True
        return os.getenv(_KILL_SWITCH_ENV, "").lower() in (
            "1", "true", "yes", "on",
        )

    # ── Top-level check ────────────────────────────────────────────────

    def check(self, ctx: RiskContext) -> None:
        """Raise ``RiskViolation`` (or subclass) on violation; else return.

        Order of checks is deterministic so failure modes are predictable:
        kill switch first (fail fast), then whitelist (cheapest), then
        notional caps in increasing complexity.
        """
        if self.is_killed():
            raise KillSwitchActive(
                "Kill switch active — all new orders are rejected. "
                f"Clear via RiskConfig.kill_switch=False or unset "
                f"the {_KILL_SWITCH_ENV} env var."
            )

        wl = self.config.symbol_whitelist
        if wl is not None and ctx.symbol not in wl:
            raise RiskViolation(
                f"Symbol {ctx.symbol!r} is not in the whitelist "
                f"({len(wl)} symbol(s) allowed)"
            )

        notional = ctx.qty * ctx.price
        if (
            self.config.max_order_notional is not None
            and notional > self.config.max_order_notional
        ):
            raise RiskViolation(
                f"Order notional ₹{notional:,.2f} exceeds cap "
                f"₹{self.config.max_order_notional:,.2f}"
            )

        # Position-level caps only apply on buys (sells reduce exposure).
        if ctx.side == OrderSide.BUY:
            self._check_post_fill_position(ctx)

    # ── Helpers ────────────────────────────────────────────────────────

    def _check_post_fill_position(self, ctx: RiskContext) -> None:
        """Compute the hypothetical post-fill position value and apply caps."""
        post_qty = ctx.existing_qty + ctx.qty
        # Use the fill price as a proxy for post-fill mark-to-market.
        # Slightly conservative (ignores the fact that other holdings
        # have moved since their entry), but it's a pre-trade check —
        # exactness isn't worth the round-trip cost.
        post_value = post_qty * ctx.price

        cap_abs = self.config.max_position_notional
        if cap_abs is not None and post_value > cap_abs:
            raise RiskViolation(
                f"Position value would be ₹{post_value:,.2f} after fill, "
                f"exceeding cap ₹{cap_abs:,.2f}"
            )

        cap_pct = self.config.max_position_pct_of_equity
        if cap_pct is not None and ctx.equity > 0:
            limit = ctx.equity * cap_pct
            if post_value > limit:
                raise RiskViolation(
                    f"Position value would be ₹{post_value:,.2f} after fill, "
                    f"exceeding {cap_pct * 100:.1f}% of equity "
                    f"(₹{limit:,.2f}). Equity = ₹{ctx.equity:,.2f}."
                )
