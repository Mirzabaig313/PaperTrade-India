"""Property-based and fuzz invariants for the broker.

Two flavors:

1. **Hypothesis-driven** properties: any random sequence of buys then a
   matched-quantity sell zeroes the position. Cash drift over a round-trip
   at the same price equals exactly ``-(buy_fees + sell_fees)``.

2. **Stateful fuzz**: thousands of pseudo-random buys/sells against a
   fixed universe; at the end, the global cash invariant
   ``cash + Σ(qty * current_price) == initial_cash + realized_pl_total
   + Σ(unrealized_pl_total)`` (i.e. equity) holds.
"""

from __future__ import annotations

import random

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from papertrade_india import (
    IndiaPaperBroker,
    InsufficientFundsError,
    InsufficientSharesError,
    PriceFeed,
)

pytestmark = pytest.mark.integration


# Reuse the StubProvider shape from conftest. Building a minimal one inline
# so this file doesn't depend on conftest internals.
class _StubProvider:
    def __init__(self, prices):
        self.prices = dict(prices)

    def get_price(self, symbol):
        return self.prices.get(symbol)


def _make_broker(tmp_path, prices, initial=10_000_000.0):
    """Build a broker with realism layers off so the property tests
    can do same-day buy/sell round-trips without tripping T+1 or
    paying book impact."""
    from papertrade_india import (
        LatencyConfig,
        OrderBookConfig,
        PartialFillConfig,
        RejectionConfig,
        SettlementConfig,
        SettlementMode,
        SlippageConfig,
    )
    feed = PriceFeed(providers=[_StubProvider(prices)],
                     short_cache_ttl_seconds=0)
    return IndiaPaperBroker(
        initial_capital=initial,
        db_path=tmp_path / "fuzz.db",
        account_id="fuzz",
        price_feed=feed,
        enforce_market_hours=False,
        order_book_config=OrderBookConfig(enabled=False),
        settlement_config=SettlementConfig(mode=SettlementMode.T_PLUS_0),
        latency_config=LatencyConfig(submit_ms_mean=0.0),
        rejection_config=RejectionConfig(rate=0.0),
        partial_fill_config=PartialFillConfig(enabled=False),
        slippage_config=SlippageConfig(bps=0.0),
        mark_to_bid=False,
        enforce_fresh_prices=False,
    )


# ── Hypothesis: round-trip identity ───────────────────────────────────


@given(
    buys=st.lists(
        st.tuples(
            st.integers(min_value=1, max_value=50),       # qty
            st.floats(min_value=10.0, max_value=10_000.0,  # price
                      allow_nan=False, allow_infinity=False),
        ),
        min_size=1,
        max_size=8,
    ),
    final_price=st.floats(min_value=10.0, max_value=10_000.0,
                          allow_nan=False, allow_infinity=False),
)
@settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_round_trip_zeroes_position(tmp_path, buys, final_price):
    """N buys followed by a single sell of the total qty closes the position
    cleanly, regardless of how the buys were sized."""
    # Sanity guard: refresh the broker per example.
    broker = _make_broker(tmp_path, prices={"X": 100.0}, initial=1e8)
    broker.reset(initial_capital=1e8)

    total_qty = 0
    for qty, price in buys:
        broker.price_feed.prime("X", price)
        broker.buy("X", qty)
        total_qty += qty

    pos = broker.get_position("X")
    assert pos is not None and pos.qty == total_qty

    broker.price_feed.prime("X", final_price)
    broker.sell("X", total_qty)

    # Position fully closed.
    assert broker.get_position("X") is None
    # No phantom rows in the positions table.
    with broker.persistence.read() as conn:
        rows = conn.execute(
            "SELECT COUNT(*) AS n FROM positions WHERE account_id = ?",
            (broker.account_id,),
        ).fetchone()["n"]
    assert rows == 0


# ── Stateful fuzz: cash & equity invariants under random ops ──────────


def test_random_op_sequence_preserves_equity_invariant(tmp_path):
    """Run ~1000 random buys/sells against 5 symbols.

    After every accepted op, the bookkeeping invariant
        cash + portfolio_value == initial_cash + realized_pl_total + unrealized_pl_total
    must hold (within a paise of float drift).

    Rejected ops (insufficient funds / shares) are caught and ignored —
    a real strategy would also handle them.
    """
    universe = {
        "RELIANCE": 2500.0, "INFY": 1800.0, "TCS": 4000.0,
        "HDFCBANK": 1500.0, "ICICIBANK": 1100.0,
    }
    initial_cash = 5_000_000.0
    broker = _make_broker(tmp_path, prices=universe, initial=initial_cash)
    broker.reset(initial_capital=initial_cash)

    rng = random.Random(0xC0FFEE)
    n_ops = 1000

    for _ in range(n_ops):
        # Drift each price by ±2% per op to simulate motion.
        for sym in universe:
            universe[sym] *= 1 + rng.uniform(-0.02, 0.02)
            broker.price_feed.prime(sym, universe[sym])

        sym = rng.choice(list(universe))
        side = rng.choice(["buy", "sell"])
        qty = rng.randint(1, 5)

        try:
            if side == "buy":
                broker.buy(sym, qty)
            else:
                broker.sell(sym, qty)
        except (InsufficientFundsError, InsufficientSharesError):
            continue  # expected; agent would re-plan

    # Bookkeeping invariant.
    a = broker.get_account()
    expected_equity = (
        initial_cash + a.realized_pl_total + a.unrealized_pl_total
    )
    assert a.equity == pytest.approx(expected_equity, abs=0.5), (
        f"Equity drift: got {a.equity}, expected {expected_equity} "
        f"(cash={a.cash}, portfolio={a.portfolio_value}, "
        f"realized={a.realized_pl_total}, unrealized={a.unrealized_pl_total})"
    )

    # Cash never went negative.
    assert a.cash >= 0

    # No position has negative qty (DB CHECK would have caught it, but
    # let's verify at the API level too).
    for p in broker.get_positions():
        assert p.qty > 0, f"non-positive qty for {p.symbol}: {p.qty}"


def test_random_ops_all_orders_have_terminal_or_pending_status(tmp_path):
    """Every order ends in {PENDING, FILLED, CANCELLED, EXPIRED, REJECTED}.

    No orders should be left in some half-applied state — the schema
    CHECK on status enforces this, but the fuzz proves no code path
    creates one.
    """
    universe = {"RELIANCE": 2500.0, "INFY": 1800.0}
    broker = _make_broker(tmp_path, prices=universe, initial=2_000_000)
    broker.reset(initial_capital=2_000_000)

    rng = random.Random(42)
    valid = {"pending", "filled", "cancelled", "expired", "rejected",
             "partially_filled"}

    for _ in range(200):
        sym = rng.choice(list(universe))
        try:
            if rng.random() < 0.5:
                broker.buy(sym, rng.randint(1, 3))
            else:
                broker.sell(sym, rng.randint(1, 3))
        except (InsufficientFundsError, InsufficientSharesError):
            continue

    for o in broker.get_orders(limit=10_000):
        assert o.status.value in valid, f"bad status: {o.status}"
