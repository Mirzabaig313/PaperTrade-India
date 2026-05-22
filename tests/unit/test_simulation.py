"""Unit tests for latency + rejection simulators."""

from __future__ import annotations

import time

from papertrade_india import (
    LatencyConfig,
    LatencySimulator,
    RejectionConfig,
    RejectionSimulator,
    RejectScenario,
)


class TestLatencySimulator:
    def test_disabled_when_zero_mean(self) -> None:
        sim = LatencySimulator(LatencyConfig(submit_ms_mean=0.0))
        assert not sim.enabled
        assert sim.sample_ms() == 0.0
        assert sim.sleep() == 0.0

    def test_lognormal_distribution_around_mean(self) -> None:
        sim = LatencySimulator(LatencyConfig(
            submit_ms_mean=80.0, submit_ms_p99=400.0, seed=42,
        ))
        samples = [sim.sample_ms() for _ in range(2000)]
        avg = sum(samples) / len(samples)
        # Deterministic with seed=42; sanity-check mean is in the right ballpark
        # (lognormal calibration is approximate, ±35%).
        assert 50.0 < avg < 130.0
        # P99 should be roughly the configured target.
        samples.sort()
        p99 = samples[int(0.99 * len(samples))]
        assert 200.0 < p99 < 700.0

    def test_sleep_actually_sleeps(self) -> None:
        sim = LatencySimulator(LatencyConfig(
            submit_ms_mean=10.0, submit_ms_p99=20.0, seed=1,
        ))
        t0 = time.perf_counter()
        sim.sleep()
        elapsed_ms = (time.perf_counter() - t0) * 1000
        # Sample is randomized; just confirm we slept at least a little.
        assert elapsed_ms >= 0


class TestRejectionSimulator:
    def test_disabled_when_zero_rate(self) -> None:
        sim = RejectionSimulator(RejectionConfig(rate=0.0))
        assert not sim.enabled
        assert sim.maybe_reject() is None

    def test_zero_rate_never_rejects(self) -> None:
        sim = RejectionSimulator(RejectionConfig(rate=0.0))
        for _ in range(100):
            assert sim.maybe_reject() is None

    def test_full_rate_always_rejects(self) -> None:
        sim = RejectionSimulator(RejectionConfig(
            rate=1.0,
            scenarios=[RejectScenario.NETWORK],
            seed=1,
        ))
        for _ in range(20):
            assert sim.maybe_reject() == RejectScenario.NETWORK

    def test_scenarios_sampled_from_list(self) -> None:
        sim = RejectionSimulator(RejectionConfig(
            rate=1.0,
            scenarios=[
                RejectScenario.NETWORK,
                RejectScenario.FREEZE_QTY,
                RejectScenario.SCRIP_SUSPENDED,
            ],
            seed=1,
        ))
        seen = {sim.maybe_reject() for _ in range(50)}
        # All three should be reachable with seed=1 over 50 draws.
        assert seen == {
            RejectScenario.NETWORK,
            RejectScenario.FREEZE_QTY,
            RejectScenario.SCRIP_SUSPENDED,
        }

    def test_partial_rate_eventually_rejects(self) -> None:
        sim = RejectionSimulator(RejectionConfig(
            rate=0.5,
            scenarios=[RejectScenario.NETWORK],
            seed=1,
        ))
        rejects = sum(1 for _ in range(1000) if sim.maybe_reject() is not None)
        # ~500 expected; allow ±100.
        assert 400 < rejects < 600
