"""Per-provider health tracking + circuit breaker.

Every external call must be wrapped in a circuit breaker (per the
backend-rules steering). The breaker has three states:

- CLOSED: requests flow. Failures are counted in a rolling window.
- OPEN: requests fail fast (returning ``None`` to the upstream
  composite, which then tries the next provider). Triggered on
  ≥ ``failure_threshold`` consecutive failures or > 50% failures in
  the rolling window.
- HALF_OPEN: after ``open_seconds``, one probe is allowed. On success
  the breaker closes; on failure it re-opens.

Wrapping is opt-in at construction time:

>>> wrapped = CircuitBreakerProvider(YFinanceProvider("NS"))

The wrapper preserves the underlying provider's :class:`ProviderInfo`
so the registry/CLI sees the original name and capabilities.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from threading import RLock

from .base import (
    OHLCV,
    MarketDataProvider,
    MarketQuote,
    ProviderError,
    ProviderInfo,
)

logger = logging.getLogger(__name__)


class _State(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class ProviderHealth:
    """Snapshot of a provider's circuit-breaker state.

    Surfaced via :attr:`CircuitBreakerProvider.health` for dashboards
    and the CLI.
    """

    name: str
    state: _State = _State.CLOSED
    consecutive_failures: int = 0
    total_calls: int = 0
    total_failures: int = 0
    last_failure_at: datetime | None = None
    last_success_at: datetime | None = None
    opened_at: float | None = None  # monotonic
    recent_results: deque[bool] = field(default_factory=lambda: deque(maxlen=20))

    @property
    def is_open(self) -> bool:
        return self.state == _State.OPEN

    @property
    def failure_rate(self) -> float:
        """Failure rate over the rolling window (0.0–1.0)."""
        if not self.recent_results:
            return 0.0
        fails = sum(1 for ok in self.recent_results if not ok)
        return fails / len(self.recent_results)


class CircuitBreakerProvider(MarketDataProvider):
    """Wraps a :class:`MarketDataProvider` with a circuit breaker.

    Parameters
    ----------
    inner:
        The provider being wrapped.
    failure_threshold:
        Open after this many consecutive failures. Default 5.
    failure_rate_threshold:
        Open when the rolling window's failure rate exceeds this.
        Default 0.5 (50%).
    window_size:
        Rolling-window size in calls. Default 20.
    open_seconds:
        How long the breaker stays OPEN before allowing a probe.
        Default 30 seconds.
    """

    def __init__(
        self,
        inner: MarketDataProvider,
        failure_threshold: int = 5,
        failure_rate_threshold: float = 0.5,
        window_size: int = 20,
        open_seconds: float = 30.0,
    ) -> None:
        self._inner = inner
        self._failure_threshold = int(failure_threshold)
        self._failure_rate_threshold = float(failure_rate_threshold)
        self._open_seconds = float(open_seconds)
        self._lock = RLock()
        self._health = ProviderHealth(
            name=inner.name,
            recent_results=deque(maxlen=int(window_size)),
        )

    # ── Pass-through introspection ────────────────────────────────────

    @property
    def info(self) -> ProviderInfo:
        return self._inner.info

    @property
    def inner(self) -> MarketDataProvider:
        return self._inner

    @property
    def health(self) -> ProviderHealth:
        with self._lock:
            return self._health

    # ── Breaker mechanics ─────────────────────────────────────────────

    def _allow_call(self) -> bool:
        """Decide whether to attempt a call to the inner provider."""
        with self._lock:
            if self._health.state == _State.CLOSED:
                return True
            if self._health.state == _State.HALF_OPEN:
                # Already probing — only one in-flight request.
                return False
            # OPEN: check whether to transition to HALF_OPEN.
            opened_at = self._health.opened_at or 0.0
            if (time.monotonic() - opened_at) >= self._open_seconds:
                self._health.state = _State.HALF_OPEN
                logger.info(
                    "Circuit breaker for %s → HALF_OPEN (probing)",
                    self._inner.name,
                )
                return True
            return False

    def _record_success(self) -> None:
        with self._lock:
            self._health.total_calls += 1
            self._health.consecutive_failures = 0
            self._health.last_success_at = datetime.now()
            self._health.recent_results.append(True)
            if self._health.state in (_State.HALF_OPEN, _State.OPEN):
                logger.info(
                    "Circuit breaker for %s → CLOSED",
                    self._inner.name,
                )
            self._health.state = _State.CLOSED
            self._health.opened_at = None

    def _record_failure(self) -> None:
        with self._lock:
            self._health.total_calls += 1
            self._health.total_failures += 1
            self._health.consecutive_failures += 1
            self._health.last_failure_at = datetime.now()
            self._health.recent_results.append(False)
            should_open = (
                self._health.consecutive_failures >= self._failure_threshold
                or self._health.failure_rate > self._failure_rate_threshold
            )
            if should_open and self._health.state != _State.OPEN:
                self._health.state = _State.OPEN
                self._health.opened_at = time.monotonic()
                logger.warning(
                    "Circuit breaker OPEN for %s "
                    "(consec=%d, rate=%.0f%%, opening for %ss)",
                    self._inner.name,
                    self._health.consecutive_failures,
                    self._health.failure_rate * 100,
                    self._open_seconds,
                )

    def reset(self) -> None:
        """Force the breaker back to CLOSED. For tests + ops."""
        with self._lock:
            self._health.state = _State.CLOSED
            self._health.consecutive_failures = 0
            self._health.opened_at = None
            self._health.recent_results.clear()

    # ── Provider hot path ─────────────────────────────────────────────

    def get_quote(self, symbol: str) -> MarketQuote | None:
        if not self._allow_call():
            return None
        try:
            quote = self._inner.get_quote(symbol)
        except ProviderError:
            self._record_failure()
            return None
        except Exception:  # noqa: BLE001 — defensive
            self._record_failure()
            return None
        # ``None`` for an unknown symbol is *not* a circuit-breaker
        # failure — it's the provider correctly saying "not mine".
        if quote is not None:
            self._record_success()
        return quote

    def get_history(
        self,
        symbol: str,
        start: date,
        end: date,
        interval: str = "1d",
    ) -> list[OHLCV]:
        if not self._allow_call():
            return []
        try:
            bars = self._inner.get_history(symbol, start, end, interval)
        except Exception:  # noqa: BLE001
            self._record_failure()
            return []
        if bars:
            self._record_success()
        return bars
