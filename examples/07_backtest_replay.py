"""Tier-A demo: deterministic backtest with ReplayClock.

A 5-day synthetic backtest of a momentum strategy on RELIANCE. Uses
``ReplayClock`` so order timestamps, market-hour checks, and watcher
ticks all run on simulated time. No wall-clock waiting.

The strategy: buy when price is above its 3-day SMA, sell when below.
Trivial, just enough to exercise the broker over multiple sessions.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta

from papertrade_india import (
    IST,
    IndiaPaperBroker,
    PriceFeed,
    ReplayClock,
    SlippageConfig,
)


class StubProvider:
    def __init__(self):
        self.prices: dict[str, float] = {}

    def set(self, symbol: str, price: float) -> None:
        self.prices[symbol] = price

    def get_price(self, symbol: str) -> float | None:
        return self.prices.get(symbol)


def main() -> None:
    # Five trading days, daily closes only (intraday is irrelevant for
    # a momentum strategy at daily cadence).
    daily_closes = [2400, 2450, 2480, 2520, 2550, 2540, 2580, 2620, 2600, 2650]

    # Start at the open of the first trading day. NSE opens 09:15 IST.
    start_at = datetime(2026, 5, 18, 9, 15, tzinfo=IST)  # Monday
    clock = ReplayClock(start_at)

    stub = StubProvider()
    feed = PriceFeed(providers=[stub], short_cache_ttl_seconds=0)

    broker = IndiaPaperBroker(
        initial_capital=500_000,
        db_path="examples/_local/backtest_replay.db",
        account_id="bt-replay",
        price_feed=feed,
        clock=clock,
        slippage_config=SlippageConfig(bps=5.0),
        # We're advancing the clock manually, but each "day" we land
        # at 09:15 IST inside REGULAR; market_hours stays on.
        enforce_market_hours=True,
    )
    broker.reset(initial_capital=500_000)

    sma_window: deque[float] = deque(maxlen=3)
    held = 0
    print(f"=== Backtest start: {clock.now().isoformat()} ===")
    print(f"Initial cash: ₹{broker.get_account().cash:,.2f}\n")

    day_offset = 0
    for close in daily_closes:
        # Skip weekends and holidays so we always land on a trading day.
        # The replay clock advances day-by-day; the calendar tells us
        # whether to trade or wait.
        candidate = start_at + timedelta(days=day_offset)
        while not broker.calendar.is_trading_day(candidate.date()):
            day_offset += 1
            candidate = start_at + timedelta(days=day_offset)
        # Land at 10:00 IST inside REGULAR.
        clock.set(candidate.replace(hour=10, minute=0))
        day_offset += 1

        stub.set("RELIANCE", close)
        sma_window.append(close)

        if len(sma_window) < 3:
            print(
                f"{clock.now().date()}  "
                f"close=₹{close:,.0f}  warmup"
            )
            continue

        sma = sum(sma_window) / 3
        signal = "BUY" if close > sma else "SELL"
        print(
            f"{clock.now().date()}  "
            f"close=₹{close:,.0f}  3d-SMA=₹{sma:,.2f}  signal={signal}",
            end="  ",
        )

        if signal == "BUY" and held == 0:
            order = broker.buy("RELIANCE", 50)
            held = 50
            print(f"BOUGHT 50 @ ₹{order.filled_avg_price:,.2f}")
        elif signal == "SELL" and held > 0:
            order = broker.sell("RELIANCE", held)
            print(
                f"SOLD {held} @ ₹{order.filled_avg_price:,.2f} "
                f"(realized P&L ₹{order.realized_pl:,.2f})"
            )
            held = 0
        else:
            print("(hold)")

    # Close any remaining position at the last close.
    if held > 0:
        order = broker.sell("RELIANCE", held)
        print(
            f"\nFlatten at end: SOLD {held} @ ₹{order.filled_avg_price:,.2f} "
            f"(realized P&L ₹{order.realized_pl:,.2f})"
        )

    a = broker.get_account()
    print(f"\n=== Backtest end: {clock.now().isoformat()} ===")
    print(f"Final cash:        ₹{a.cash:,.2f}")
    print(f"Total realized P&L: ₹{a.realized_pl_total:,.2f}")
    print(f"Cash invariant:    {broker.verify_cash_invariant()}")

    # All order timestamps should fall within our simulated window.
    orders = broker.get_orders(limit=50)
    print(f"\nOrders placed: {len(orders)}")
    for o in orders:
        print(
            f"  {o.created_at.isoformat()}  "
            f"{o.side.value:<4s} {o.qty:>3g} @ ₹{o.filled_avg_price:,.2f}"
        )


if __name__ == "__main__":
    main()
