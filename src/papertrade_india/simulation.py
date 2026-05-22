"""Latency + rejection simulation.

Real broker APIs aren't deterministic. They:
- Take 50–500ms to acknowledge an order (more on a bad day).
- Sporadically reject orders for non-business reasons (network, throttle,
  exchange freeze quantity, scrip just suspended, OFS window, …).

This module lets you opt into both, so agent tests can confirm their
code handles a slow / flaky broker without ever touching a real one.

Usage:

    broker = IndiaPaperBroker(
        latency_config=LatencyConfig(submit_ms_mean=80, submit_ms_p99=400),
        rejection_config=RejectionConfig(
            rate=0.001,
            scenarios=[RejectScenario.FREEZE_QTY, RejectScenario.NETWORK],
        ),
    )

Defaults are off (zero latency, zero rejection rate) so existing tests
and behavior are unchanged.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


# ── Latency ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LatencyConfig:
    """Approximate the wall-clock delay of a real broker submit.

    We model latency as a log-normal so the tail behaves like real
    broker submit latencies (occasional 1s outliers on top of an 80ms
    median). All times in milliseconds.

    Defaults assume a typical Indian discount broker (Zerodha,
    Upstox, Groww) on a healthy network: median ~80ms, p99 ~400ms.
    Set ``submit_ms_mean=0`` to disable for deterministic backtests.

    The default seed is fixed so the simulator is reproducible across
    runs. Set ``seed=None`` for non-deterministic latency.
    """

    submit_ms_mean: float = 80.0
    submit_ms_p99: float = 400.0
    seed: int | None = 42


class LatencySimulator:
    """Sleeps the calling thread by a sampled latency."""

    def __init__(self, config: LatencyConfig | None = None) -> None:
        self.config = config or LatencyConfig()
        self._rng = random.Random(self.config.seed)

    @property
    def enabled(self) -> bool:
        return self.config.submit_ms_mean > 0

    def sample_ms(self) -> float:
        """Draw a single latency in milliseconds."""
        cfg = self.config
        if cfg.submit_ms_mean <= 0:
            return 0.0
        if cfg.submit_ms_p99 <= cfg.submit_ms_mean:
            # Degenerate inputs — fall back to constant latency.
            return cfg.submit_ms_mean
        # Log-normal calibrated so mean ≈ submit_ms_mean and 99th
        # percentile ≈ submit_ms_p99. Solve for mu/sigma.
        # mu = ln(mean) - sigma^2 / 2
        # p99 = exp(mu + 2.326 * sigma)
        # Solve numerically (cheap, runs once per call).
        import math
        mean = cfg.submit_ms_mean
        p99 = cfg.submit_ms_p99
        # Crude bisection on sigma.
        lo, hi = 0.01, 3.0
        for _ in range(50):
            sigma = (lo + hi) / 2.0
            mu = math.log(mean) - sigma * sigma / 2.0
            est_p99 = math.exp(mu + 2.326 * sigma)
            if est_p99 < p99:
                lo = sigma
            else:
                hi = sigma
        sigma = (lo + hi) / 2.0
        mu = math.log(mean) - sigma * sigma / 2.0
        return self._rng.lognormvariate(mu, sigma)

    def sleep(self) -> float:
        """Sample and sleep. Returns the actual delay in ms."""
        if not self.enabled:
            return 0.0
        ms = max(0.0, self.sample_ms())
        time.sleep(ms / 1000.0)
        return ms


# ── Rejection scenarios ──────────────────────────────────────────────


class RejectScenario(str, Enum):
    """Why a real broker might bounce an order back at you.

    Documented for callers' reference; the simulator uses these as
    string codes attached to the rejection.
    """

    NETWORK = "network"            # transient connectivity blip
    THROTTLED = "throttled"        # broker rate-limit hit
    FREEZE_QTY = "freeze_qty"      # exchange freeze qty exceeded
    SCRIP_SUSPENDED = "scrip_suspended"
    AUCTION_WINDOW = "auction_window"
    OFS_WINDOW = "ofs_window"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RejectionConfig:
    """Probabilistic rejection injection.

    ``rate`` is per-order. ``scenarios`` weights which scenario gets
    chosen on a hit (uniform over the list).

    Default rate is ``0.001`` (1 in 1000) — small enough that day-to-day
    flows look normal, frequent enough that an agent run of any length
    will see at least one rejection and have to handle it. Set
    ``rate=0`` if you want a deterministic backtest.

    The default seed is fixed so simulator runs are reproducible.
    """

    rate: float = 0.001
    scenarios: list[RejectScenario] = field(
        default_factory=lambda: [
            RejectScenario.NETWORK,
            RejectScenario.THROTTLED,
            RejectScenario.FREEZE_QTY,
        ],
    )
    seed: int | None = 42


class RejectionSimulator:
    """Coin-flip rejection injector. Independent RNG so it's reproducible."""

    def __init__(self, config: RejectionConfig | None = None) -> None:
        self.config = config or RejectionConfig()
        self._rng = random.Random(self.config.seed)

    @property
    def enabled(self) -> bool:
        return self.config.rate > 0 and bool(self.config.scenarios)

    def maybe_reject(self) -> RejectScenario | None:
        """Return a scenario to reject with, or ``None`` to allow.

        Caller raises :class:`RandomBrokerRejection` with the returned
        scenario as context.
        """
        if not self.enabled:
            return None
        if self._rng.random() >= self.config.rate:
            return None
        return self._rng.choice(self.config.scenarios)
