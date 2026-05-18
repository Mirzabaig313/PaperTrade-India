"""Configurable partial-fill model.

Honest framing: real partial fills require an order-book and per-symbol
liquidity. We don't have that — we have last-traded price.

What we model instead is a **configurable per-tick fill cap**: when the
limit-order watcher would fill a 1000-share order, the cap says "fill at
most N shares this tick; the rest stays PENDING." Across multiple ticks
the order eventually fills.

This isn't real bid/ask depth, but it's a useful knob for stress-testing
strategies that assume infinite liquidity. Tune ``PartialFillConfig``
to your beliefs about per-symbol average ticket size.

Configuration
-------------
- ``enabled``: master switch. False = legacy all-or-nothing fills.
- ``max_per_tick``: absolute share cap per watcher tick. ``None`` = no cap.
- ``max_pct_per_tick``: cap as a fraction of the order's qty. e.g. 0.25 =
  fill ~25% per tick. ``None`` = no cap.
- ``min_fill_qty``: don't bother filling slivers smaller than this. The
  order stays PENDING until enough has accumulated.

The effective cap for any tick is ``min(max_per_tick, qty * max_pct_per_tick)``,
floored by ``min_fill_qty``.

Market orders
-------------
Market orders still fill in one shot. Partial fills are limit-only —
they're a watcher-tick concept. (A market order that can't fully fill
in one print would, in reality, hit the order book at multiple price
levels — that needs a real LOB simulator.)
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class PartialFillConfig:
    """Configuration for partial-fill simulation."""

    enabled: bool = False
    max_per_tick: int | None = None
    max_pct_per_tick: float | None = None
    min_fill_qty: int = 1

    def fill_qty(self, remaining_qty: float) -> float:
        """Compute how many shares to fill on this tick.

        Returns 0 (no fill this tick) or a value in [min_fill_qty, remaining_qty].
        """
        if not self.enabled or remaining_qty <= 0:
            return remaining_qty  # disabled = full fill (legacy)

        candidates: list[float] = [remaining_qty]
        if self.max_per_tick is not None:
            candidates.append(float(self.max_per_tick))
        if self.max_pct_per_tick is not None:
            candidates.append(remaining_qty * self.max_pct_per_tick)

        # Truncate to whole shares (Indian equity is whole-share-only,
        # except for fractional MF units which are out of scope here).
        cap = math.floor(min(candidates))

        if cap < self.min_fill_qty:
            # Slice would be smaller than the configured slug — wait for
            # the next tick. This avoids 100 1-share fills with their
            # 100x DP charges and 100x ledger rows.
            return 0.0
        return float(cap)
