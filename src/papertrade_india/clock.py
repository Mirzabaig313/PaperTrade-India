"""Clock abstraction for live vs replay (backtest) mode.

By default the broker uses ``WallClock``, which delegates to
``datetime.now(IST)``. For backtesting, pass a ``ReplayClock`` whose
"now" advances explicitly.

Why this lives in a separate module
-----------------------------------
The broker calls ``datetime.now(IST)`` in many places: order timestamps,
ledger ``recorded_at``, event ``recorded_at``, calendar phase checks,
DAY-tif expiry timestamps, idempotency cleanup, etc. Replacing every
call with ``self._clock.now()`` is a small refactor; centralizing the
implementation behind a ``Clock`` protocol keeps the broker code tidy
and lets us swap clocks at construction time.

Why we don't use ``freezegun`` or similar
-----------------------------------------
``freezegun`` patches ``datetime.now`` globally, which is contagious
and breaks third-party libraries during tests. A first-class clock
parameter is opt-in, scoped, and explicit.

Design
------
- ``Clock`` is a Protocol with one method: ``now() -> datetime``.
- ``WallClock(tz=IST)`` returns the wall clock in IST. It's the default.
- ``ReplayClock(start_at)`` is deterministic. Mutate via ``advance(delta)``
  or ``set(dt)``.

The broker also uses the calendar to decide market-open / phase. The
calendar accepts an explicit ``dt`` argument on every method, so we
just pass ``self._clock.now()`` everywhere we used to pass nothing.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol

from .market_hours import IST


class Clock(Protocol):
    """Anything that can produce a 'now' datetime."""

    def now(self) -> datetime:
        ...


class WallClock:
    """Live wall-clock time in the configured timezone (default: IST).

    This is the broker's default. ``WallClock().now()`` is exactly
    ``datetime.now(tz)``.
    """

    def __init__(self, tz=IST) -> None:
        self.tz = tz

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def __repr__(self) -> str:
        return f"WallClock(tz={self.tz})"


class ReplayClock:
    """Deterministic clock for backtesting.

    Starts at ``start_at`` and only advances when explicitly asked via
    ``advance`` or ``set``. ``now()`` returns the current value.

    Examples
    --------
    >>> from datetime import datetime, timedelta
    >>> from papertrade_india import IST
    >>> c = ReplayClock(datetime(2026, 5, 18, 10, 0, tzinfo=IST))
    >>> c.now().hour
    10
    >>> c.advance(timedelta(hours=2))
    >>> c.now().hour
    12

    Thread-safety
    -------------
    Single-threaded backtests are the expected use case. Concurrent
    advances from multiple threads are not supported; wrap externally
    with a lock if you need that.
    """

    def __init__(self, start_at: datetime) -> None:
        if start_at.tzinfo is None:
            # Coerce to IST for consistency with the rest of the package.
            start_at = start_at.replace(tzinfo=IST)
        self._t = start_at

    def now(self) -> datetime:
        return self._t

    def advance(self, delta: timedelta) -> None:
        """Move the clock forward by ``delta``.

        Negative deltas raise ``ValueError`` — the simulator's audit log
        and ledger assume monotonic time, and going backwards would
        corrupt the ordering invariants.
        """
        if delta.total_seconds() < 0:
            raise ValueError(
                f"ReplayClock.advance requires non-negative delta, got {delta}"
            )
        self._t = self._t + delta

    def set(self, dt: datetime) -> None:
        """Jump to a specific datetime. Must be >= current value."""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)
        if dt < self._t:
            raise ValueError(
                f"ReplayClock.set: cannot move backwards from {self._t} to {dt}"
            )
        self._t = dt

    def __repr__(self) -> str:
        return f"ReplayClock(now={self._t.isoformat()})"
