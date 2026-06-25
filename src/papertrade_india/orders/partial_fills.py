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
- ``min_fill_qty``: smallest slice we'll fill. Acts as a floor: the
  effective per-tick cap is ``max(computed_cap, min_fill_qty)``, so
  the order always makes forward progress instead of stalling on a
  cap that rounds below the floor.

The effective cap per tick is roughly
``min(max_per_tick, qty * max_pct_per_tick)``, lifted to at least
``min_fill_qty`` so we never return zero when there's still work to do.

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

        Returns 0 (no fill this tick) or a value in
        ``[min_fill_qty, remaining_qty]``, with one exception: when
        ``remaining_qty <= min_fill_qty``, we fill ``remaining_qty``
        in a single shot. Otherwise small orders (or the last sliver
        of a large order) would never fill.

        When the percentage cap rounds below ``min_fill_qty``, we lift
        the cap *up* to ``min_fill_qty`` rather than truncating to 0 —
        otherwise a 5-share order with 25% cap and ``min_fill_qty=1``
        would stall after filling 2 shares (remaining 3 × 0.25 = 0.75,
        floors to 0). The whole point of ``min_fill_qty`` is to
        guarantee forward progress.
        """
        if not self.enabled or remaining_qty <= 0:
            return remaining_qty  # disabled = full fill (legacy)

        # Whole-order-fits-in-one-tick: don't bother slicing.
        if remaining_qty <= self.min_fill_qty:
            return float(remaining_qty)

        candidates: list[float] = [remaining_qty]
        if self.max_per_tick is not None:
            candidates.append(float(self.max_per_tick))
        if self.max_pct_per_tick is not None:
            candidates.append(remaining_qty * self.max_pct_per_tick)

        # Truncate to whole shares (Indian equity is whole-share-only,
        # except for fractional MF units which are out of scope here).
        cap = math.floor(min(candidates))

        # Guarantee forward progress: a cap that rounds below
        # ``min_fill_qty`` is lifted up to it. The earlier "remaining
        # <= min_fill_qty" shortcut handles the small-order case, so
        # by here we know remaining > min_fill_qty and lifting is safe.
        if cap < self.min_fill_qty:
            cap = min(self.min_fill_qty, int(remaining_qty))
        return float(cap)
