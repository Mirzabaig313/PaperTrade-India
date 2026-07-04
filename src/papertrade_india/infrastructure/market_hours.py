"""NSE/BSE trading hours and holiday calendar.

We enforce that market orders submitted outside trading hours are rejected
(or queued, in the case of limit orders). Without this, a caller's
back-of-the-envelope P&L would silently drift from reality.

Hours used:
- NSE/BSE equity continuous session: 09:15 to 15:30 IST, Mon–Fri,
  excluding holidays.
- Pre-open auction: 09:00 to 09:08 IST. Limit orders accepted; no
  continuous fills.
- Closing-session window: 15:40 to 16:00 IST (UCC/AMO accepted).

Holiday data ships as JSON in ``data/nse_holidays_*.json``. Each year's
list can be refreshed independently. The community can keep these files
current via PR.

Time zone: Asia/Kolkata (IST). IST does not observe DST, so this is just
UTC+5:30 year-round.

Session phases
--------------
The simulator models four phases per trading day:
- ``CLOSED``: outside any session window.
- ``PRE_OPEN``: 09:00–09:08, limit orders queue, no continuous fills.
- ``REGULAR``: 09:15–15:30, continuous matching (what we simulate).
- ``POST_CLOSE``: 15:40–16:00, AMO/closing-session window.

The broker's market-orders-only-during-REGULAR rule preserves backward
compatibility (``is_market_open`` returns True only during REGULAR).
``current_phase`` exposes the full state for callers that care.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timedelta
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .holidays import UpstoxHolidayProvider

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover — zoneinfo is std-lib in 3.9+
    from backports.zoneinfo import ZoneInfo  # type: ignore

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
NSE_OPEN = time(9, 15)
NSE_CLOSE = time(15, 30)

# Session phase boundaries.
PRE_OPEN_START = time(9, 0)
PRE_OPEN_END = time(9, 8)
POST_CLOSE_START = time(15, 40)
POST_CLOSE_END = time(16, 0)


class SessionPhase(str, Enum):
    """NSE session phases."""

    CLOSED = "closed"
    PRE_OPEN = "pre_open"
    REGULAR = "regular"
    POST_CLOSE = "post_close"


class NSECalendar:
    """NSE trading calendar.

    Holiday lists are loaded from JSON files in the ``data/`` directory
    (one per year). Loading is lazy-friendly: missing or malformed files
    log a warning but don't break the calendar — weekends and explicit
    weekday holidays are still respected for years that *are* loaded.

    Parameters
    ----------
    holidays_dir:
        Override the default data directory (mostly used in tests).
    """

    def __init__(
        self,
        holidays_dir: Path | None = None,
        holiday_provider: UpstoxHolidayProvider | None = None,
    ) -> None:
        self.holidays_dir = (
            holidays_dir or Path(__file__).resolve().parent.parent / "data"
        )
        self._holidays: set[date] = set()
        self._load_holidays()
        # Optional live source (Upstox published calendar). Augments the
        # bundled JSON — the API's dates are added on top, and the JSON
        # remains the offline floor if the API is unavailable. Default
        # None keeps NSECalendar() fully offline (hermetic tests).
        if holiday_provider is not None:
            try:
                self._holidays |= holiday_provider.closed_dates()
            except Exception as e:  # noqa: BLE001 — never break the calendar
                logger.warning("Live holiday provider failed: %s", e)

    # ── Loading ────────────────────────────────────────────────────────

    def _load_holidays(self) -> None:
        if not self.holidays_dir.exists():
            logger.warning(
                "Holiday dir %s does not exist; no holidays loaded",
                self.holidays_dir,
            )
            return

        for path in sorted(self.holidays_dir.glob("nse_holidays_*.json")):
            try:
                with open(path) as f:
                    data = json.load(f)
                for d in data.get("holidays", []):
                    self._holidays.add(date.fromisoformat(d))
            except (OSError, ValueError, json.JSONDecodeError) as e:
                logger.warning("Failed to load holidays from %s: %s", path, e)

    def reload(self) -> None:
        """Re-read holiday files from disk (useful after PR-merging updates)."""
        self._holidays.clear()
        self._load_holidays()

    # ── Queries ────────────────────────────────────────────────────────

    def is_holiday(self, d: date) -> bool:
        return d in self._holidays

    def is_trading_day(self, d: date) -> bool:
        if d.weekday() >= 5:  # Saturday=5, Sunday=6
            return False
        return not self.is_holiday(d)

    def is_market_open(self, dt: datetime | None = None) -> bool:
        """``True`` if NSE is in the continuous (REGULAR) session at ``dt``.

        Pre-open and post-close phases return False — they're separate
        windows where market orders shouldn't fill at last price. Use
        ``current_phase`` to distinguish them.
        """
        return self.current_phase(dt) == SessionPhase.REGULAR

    def current_phase(self, dt: datetime | None = None) -> SessionPhase:
        """Return the active session phase at ``dt`` (IST).

        - ``REGULAR``    09:15–15:30 (continuous matching)
        - ``PRE_OPEN``   09:00–09:08 (limit orders queue, no fills)
        - ``POST_CLOSE`` 15:40–16:00 (AMO / closing session)
        - ``CLOSED``     anywhere else (incl. weekends and holidays)
        """
        if dt is None:
            dt = datetime.now(IST)
        elif dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)
        else:
            dt = dt.astimezone(IST)

        if not self.is_trading_day(dt.date()):
            return SessionPhase.CLOSED

        t = dt.time()
        if NSE_OPEN <= t <= NSE_CLOSE:
            return SessionPhase.REGULAR
        if PRE_OPEN_START <= t <= PRE_OPEN_END:
            return SessionPhase.PRE_OPEN
        if POST_CLOSE_START <= t <= POST_CLOSE_END:
            return SessionPhase.POST_CLOSE
        return SessionPhase.CLOSED

    def next_open(self, dt: datetime | None = None) -> datetime:
        """Next datetime when the market opens (IST)."""
        if dt is None:
            dt = datetime.now(IST)
        elif dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)
        else:
            dt = dt.astimezone(IST)

        candidate = dt.replace(
            hour=NSE_OPEN.hour, minute=NSE_OPEN.minute,
            second=0, microsecond=0,
        )
        # If today's open has already passed, look at tomorrow.
        if dt.time() >= NSE_OPEN:
            candidate += timedelta(days=1)
        # Walk forward over weekends and holidays.
        for _ in range(20):  # Bounded loop — ~3 weeks of guard
            if self.is_trading_day(candidate.date()):
                return candidate
            candidate += timedelta(days=1)
        # Shouldn't happen in practice; return the candidate anyway so
        # callers get a deterministic value.
        return candidate
