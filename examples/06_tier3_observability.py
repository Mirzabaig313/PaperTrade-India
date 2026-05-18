"""Tier-3 demo: observability bus + partial fills + per-symbol slippage.

Wires a small "metrics counter" callback to the broker's event bus, then
runs a synthetic trading session that produces a mix of full fills,
partial fills, cancellations, and a corporate action. At the end we
print:

  - A live-counted summary from the metrics callback.
  - The persisted event log for the same period.
  - The cash-ledger invariant check.

Run this without network: the broker uses an in-memory price stub so it
doesn't touch yfinance.
"""

from __future__ import annotations

from collections import Counter

from papertrade_india import (
    BrokerEvent,
    IndiaPaperBroker,
    LimitOrderWatcher,
    OrderType,
    PartialFillConfig,
    PriceFeed,
    SlippageConfig,
)


class StubProvider:
    """Predictable in-memory provider so the example doesn't hit network."""

    def __init__(self):
        self.prices: dict[str, float] = {}

    def set(self, symbol: str, price: float) -> None:
        self.prices[symbol] = price

    def get_price(self, symbol: str) -> float | None:
        return self.prices.get(symbol)


def main() -> None:
    stub = StubProvider()
    feed = PriceFeed(providers=[stub], short_cache_ttl_seconds=0)

    broker = IndiaPaperBroker(
        initial_capital=2_000_000,
        db_path="examples/_local/tier3.db",
        account_id="tier3-demo",
        price_feed=feed,
        # Per-symbol slippage: liquid 5 bps, illiquid 50 bps.
        slippage_config=SlippageConfig(
            bps=5.0, per_symbol_bps={"PENNY": 50.0},
        ),
        # Partial fills: 5 shares per watcher tick, min 1 share.
        partial_fill_config=PartialFillConfig(
            enabled=True, max_per_tick=5, min_fill_qty=1,
        ),
        enforce_market_hours=False,
    )
    broker.reset(initial_capital=2_000_000)

    # ── Subscribe a small metrics counter ────────────────────────────
    counters: Counter[str] = Counter()

    def metrics(ev: BrokerEvent) -> None:
        counters[ev.event_type] += 1

    broker.events.subscribe(metrics, name="metrics-counter")

    # And a more selective subscriber: filled-only.
    fill_log: list[BrokerEvent] = []
    broker.events.subscribe(
        fill_log.append,
        name="fill-only",
        event_types=("order_filled", "order_partially_filled"),
    )

    # ── Drive a few trades ───────────────────────────────────────────
    stub.set("RELIANCE", 2_500.0)
    stub.set("PENNY", 100.0)
    stub.set("INFY", 1_800.0)

    # Market buy on a liquid name (small slippage applies).
    broker.buy("RELIANCE", 1)

    # Big limit buy on an illiquid name; partial-fill cap will slice it.
    broker.buy(
        "PENNY", 12,
        order_type=OrderType.LIMIT, limit_price=100.0,
    )
    watcher = LimitOrderWatcher(broker, interval_seconds=999)
    # 12 shares at cap=5 → 5 + 5 + 2 across three ticks.
    for _ in range(3):
        watcher.tick()

    # Cancel a fresh limit before any fills.
    cancellable = broker.buy(
        "INFY", 10,
        order_type=OrderType.LIMIT, limit_price=1_500.0,  # below market
    )
    broker.cancel_order(cancellable.id)

    # Apply a 2:1 split on RELIANCE (corporate_action event).
    broker.apply_split("RELIANCE", ratio_num=2, ratio_den=1, notes="demo split")

    # ── Show what the metrics callback observed ─────────────────────
    print("\n=== Metrics counters (live, in-process) ===")
    for k, v in sorted(counters.items()):
        print(f"  {k:25s}  {v}")

    print(f"\nFilled / partially-filled events seen: {len(fill_log)}")
    for e in fill_log:
        print(
            f"  {e.event_type:24s}  "
            f"{e.payload.get('symbol'):<10s}  "
            f"{e.payload}"
        )

    # ── Show the persisted event log ─────────────────────────────────
    print("\n=== Persisted event log (newest first) ===")
    for ev in broker.get_events(limit=20):
        print(f"  {ev.recorded_at.isoformat()}  {ev.event_type}")

    # ── Verify the cash-ledger invariant ────────────────────────────
    print("\n=== Cash invariant ===")
    print(
        "  cash invariant holds:",
        broker.verify_cash_invariant(),
    )
    a = broker.get_account()
    print(f"  cash = ₹{a.cash:,.2f}, equity = ₹{a.equity:,.2f}")

    # ── Replay against a brand new subscriber ────────────────────────
    # A fresh subscriber registered now would have missed everything.
    # ``replay_from_broker`` catches it up.
    print("\n=== Replay demo ===")
    backfill: list[BrokerEvent] = []
    broker.events.subscribe(backfill.append, name="backfill")
    n = broker.events.replay_from_broker(broker, event_types=("order_filled",))
    print(f"  replayed {n} order_filled event(s) to the new subscriber")


if __name__ == "__main__":
    main()
