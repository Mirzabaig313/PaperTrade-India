"""Example: every realism feature, opt-in.

The default broker still behaves exactly like v0.1.x. This script flips
each realism knob and shows what changes:

  1. Tick-size enforcement (rejects ₹2940.327 limits)
  2. Lot-size enforcement (rejects 30 shares when lot=25)
  3. Daily price-band rejection (rejects ₹2750 when prev close is 2500)
  4. Stop-loss + Stop-Limit + Bracket orders
  5. T+1 settlement + intraday auto-square-off
  6. Latency + random rejection simulation
  7. Mark-to-market off bid (real-broker P&L convention)

Run me::

    python examples/09_realism_features.py

Nothing here actually places a real trade. The price feed is a
deterministic stub so the walkthrough produces the same output every
time.
"""

from __future__ import annotations

import sys
from datetime import datetime, time

from papertrade_india import (
    Exchange,
    IndiaPaperBroker,
    LatencyConfig,
    LotSizeViolation,
    MicrostructureConfig,
    OrderBookConfig,
    OrderType,
    PriceBandViolation,
    PriceFeed,
    ProductType,
    RandomBrokerRejection,
    RejectionConfig,
    RejectScenario,
    SettlementConfig,
    SettlementMode,
    TickSizeViolation,
)
from papertrade_india.providers import (
    MarketDataProvider,
    MarketQuote,
    ProviderCapability,
    ProviderInfo,
)

# ── A deterministic price stub for the demo ──────────────────────────


class StubProvider(MarketDataProvider):
    def __init__(self) -> None:
        self.last = {
            "RELIANCE": 2500.0,
            "TCS": 4000.0,
            "INFY": 1800.0,
        }
        self.prev = {
            "RELIANCE": 2500.0,
            "TCS": 4000.0,
            "INFY": 1800.0,
        }

    @property
    def info(self) -> ProviderInfo:
        return ProviderInfo(
            name="stub",
            description="demo stub",
            capabilities=ProviderCapability.LAST_PRICE | ProviderCapability.QUOTE,
        )

    def get_quote(self, symbol: str) -> MarketQuote | None:
        last = self.last.get(symbol)
        if last is None:
            return None
        # Simulate a 5 bps spread + sane volume.
        bid = round(last * 0.9995, 2)
        ask = round(last * 1.0005, 2)
        return MarketQuote(
            last=last, timestamp=datetime.now(),
            bid=bid, ask=ask, prev_close=self.prev.get(symbol),
            volume=1_000_000, source="stub",
        )

    def set(self, symbol: str, last: float) -> None:
        self.last[symbol] = last


def header(s: str) -> None:
    print(f"\n{'─' * 60}\n{s}\n{'─' * 60}")


def main() -> int:
    provider = StubProvider()
    feed = PriceFeed(providers=[provider], short_cache_ttl_seconds=0)

    # ── 1. Microstructure: tick / lot / band ─────────────────────────
    header("1. Tick / lot / band enforcement")
    broker = IndiaPaperBroker(
        initial_capital=1_000_000,
        db_path="data/realism_demo.db",
        price_feed=feed,
        enforce_market_hours=False,
        microstructure_config=MicrostructureConfig(
            enforce_tick_size=True,
            enforce_lot_size=True,
            enforce_price_band=True,
        ),
    )
    # Seed RELIANCE with NSE-realistic metadata.
    with broker.persistence.transaction() as conn:
        broker.symbol_master.upsert(
            conn, symbol="RELIANCE", exchange=Exchange.NSE,
            tick_size=0.05, lot_size=1, daily_band_pct=0.10,
        )
        # Pretend TCS trades in lots of 25 (it doesn't really, but for demo).
        broker.symbol_master.upsert(
            conn, symbol="TCS", exchange=Exchange.NSE,
            tick_size=0.05, lot_size=25, daily_band_pct=0.10,
        )

    print("Trying RELIANCE limit at ₹2940.327 (not tick-aligned)…")
    try:
        broker.buy("RELIANCE", 1, order_type=OrderType.LIMIT, limit_price=2940.327)
    except TickSizeViolation as e:
        print(f"  → rejected: {e}")

    print("\nTrying TCS qty=30 (lot=25)…")
    try:
        broker.buy("TCS", 30)
    except LotSizeViolation as e:
        print(f"  → rejected: {e}")

    print("\nTrying RELIANCE limit at ₹2900 (above 10% band of 2500)…")
    try:
        broker.buy("RELIANCE", 1, order_type=OrderType.LIMIT, limit_price=2900)
    except PriceBandViolation as e:
        print(f"  → rejected: {e}")

    # ── 2. Stop / Stop-Limit / Bracket ───────────────────────────────
    header("2. Stop-loss + Stop-Limit + Bracket orders")
    broker.reset(initial_capital=1_000_000)
    # T+1 is on by default now, so use INTRADAY product type for the
    # same-day buy/sell pattern this demo wants to show.
    broker.buy("RELIANCE", 10, product_type=ProductType.INTRADAY)
    print("Bought 10 RELIANCE @ ₹2500 (INTRADAY)")

    sl = broker.sell(
        "RELIANCE", 10,
        order_type=OrderType.STOP_MARKET,
        stop_price=2400.00,
        product_type=ProductType.INTRADAY,
    )
    print(f"Placed SELL STOP @ ₹2400 (id={sl.id}) — currently {sl.status.value}")
    provider.set("RELIANCE", 2399.0)
    print("Price falls to ₹2399 — running watcher tick…")
    from papertrade_india import LimitOrderWatcher
    # Disable auto-square-off in this demo so the watcher doesn't close
    # the intraday position before the stop fires (this script may be
    # run after 15:15 IST).
    LimitOrderWatcher(broker, auto_square_off_intraday=False).tick()
    sl_after = broker.get_order(sl.id)
    print(f"  → stop {sl_after.status.value} @ ₹{sl_after.filled_avg_price:.2f}")

    # Bracket
    print("\nPlacing bracket: BUY 5 RELIANCE entry market, SL=2380, TGT=2420")
    provider.set("RELIANCE", 2400.0)
    parent = broker.buy(
        "RELIANCE", 5,
        order_type=OrderType.BRACKET,
        stop_price=2380.00,
        target_price=2420.00,
        product_type=ProductType.INTRADAY,
    )
    children = [o for o in broker.get_orders(limit=10) if o.parent_order_id == parent.id]
    print(f"  → parent {parent.status.value}, {len(children)} children pending")

    print("Price rallies to ₹2421 — target should fill, stop should cancel…")
    provider.set("RELIANCE", 2421.0)
    LimitOrderWatcher(broker, auto_square_off_intraday=False).tick()
    children = [o for o in broker.get_orders(limit=10) if o.parent_order_id == parent.id]
    for c in children:
        print(f"  → child {c.order_type.value}: {c.status.value}")

    # ── 3. T+1 + intraday square-off ────────────────────────────────
    header("3. T+1 settlement + intraday auto-square-off")
    broker = IndiaPaperBroker(
        initial_capital=1_000_000,
        db_path="data/realism_demo_t1.db",
        price_feed=feed,
        enforce_market_hours=False,
        settlement_config=SettlementConfig(
            mode=SettlementMode.T_PLUS_1,
            auto_square_off_at=time(15, 15),
        ),
    )
    broker.reset(initial_capital=1_000_000)
    broker.buy("INFY", 5)  # default DELIVERY
    print("Bought 5 INFY (DELIVERY). Trying to sell same-day…")
    try:
        broker.sell("INFY", 5)
        print("  → succeeded (unexpected!)")
    except Exception as e:
        print(f"  → rejected: {e}")
    print("Buying 5 INFY as INTRADAY…")
    broker.buy("INFY", 5, product_type=ProductType.INTRADAY)
    broker.sell("INFY", 5, product_type=ProductType.INTRADAY)
    print("  → same-day round-trip succeeded.")

    # ── 4. Latency + rejection simulation ───────────────────────────
    header("4. Latency + random rejection simulation")
    flaky = IndiaPaperBroker(
        initial_capital=1_000_000,
        db_path="data/realism_demo_flaky.db",
        price_feed=feed,
        enforce_market_hours=False,
        latency_config=LatencyConfig(
            submit_ms_mean=80, submit_ms_p99=400, seed=1,
        ),
        rejection_config=RejectionConfig(
            rate=0.3,
            scenarios=[RejectScenario.NETWORK, RejectScenario.FREEZE_QTY],
            seed=1,
        ),
    )
    flaky.reset(initial_capital=1_000_000)
    print("Submitting 10 orders with 30% rejection rate + 80ms median latency…")
    successes = rejects = 0
    for _ in range(10):
        try:
            flaky.buy("INFY", 1)
            successes += 1
        except RandomBrokerRejection as e:
            rejects += 1
            print(f"  rejected: {e}")
    print(f"  → {successes} succeeded, {rejects} rejected")

    # ── 5. Mark-to-bid + order book impact ──────────────────────────
    header("5. Mark-to-bid + synthetic order-book impact")
    # Reset the price stub so this section starts clean.
    provider.set("RELIANCE", 2500.0)
    realistic = IndiaPaperBroker(
        initial_capital=20_000_000,  # large enough for the 5000-share demo
        db_path="data/realism_demo_book.db",
        price_feed=feed,
        enforce_market_hours=False,
        mark_to_bid=True,
        order_book_config=OrderBookConfig(
            enabled=True, levels=10, depth_pct_of_adv=0.005,
            shape_decay=0.6, almgren_coeff_bps=50.0,
        ),
    )
    realistic.reset(initial_capital=20_000_000)
    realistic.buy("RELIANCE", 1)
    pos = realistic.get_position("RELIANCE")
    print(
        f"After buy of 1 RELIANCE: avg_cost=₹{pos.avg_cost:.2f}, "
        f"mark_basis={pos.mark_basis}, current=₹{pos.current_price:.2f}, "
        f"unrealized=₹{pos.unrealized_pl:.2f} "
        f"(spread cost = paid above bid)",
    )
    # Big market order — book impact should kick in.
    # The default ADV (used when the provider doesn't supply volume) drives
    # synthesized depth. Tighten it so 5000 shares walks visible levels.
    realistic._book_sim.config = OrderBookConfig(
        enabled=True, levels=10, depth_pct_of_adv=0.005,
        shape_decay=0.6, almgren_coeff_bps=50.0,
        default_adv=200_000,
    )
    # Force the provider's volume override by clearing it on the stub.
    provider_volume_was = StubProvider.get_quote
    def thin_get_quote(self, symbol):
        q = provider_volume_was(self, symbol)
        if q is None:
            return None
        return MarketQuote(
            last=q.last, timestamp=q.timestamp,
            bid=q.bid, ask=q.ask, prev_close=q.prev_close,
            volume=100_000, source=q.source,
        )
    StubProvider.get_quote = thin_get_quote  # type: ignore[assignment]
    big = realistic.buy("RELIANCE", 5000)
    StubProvider.get_quote = provider_volume_was  # restore
    print(
        f"Bought 5000 RELIANCE @ VWAP ₹{big.filled_avg_price:.2f} "
        f"(touch ask ~₹{2500.0 * 1.0005:.2f}, book walked deeper)",
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
