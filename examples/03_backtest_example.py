"""Tiny backtest harness using a custom price feed.

Drives the broker over a synthetic price series so the example runs
without touching the network or the calendar.
"""

from __future__ import annotations

from papertrade_india import (
    IndiaPaperBroker,
    PriceFeed,
)


class StubProvider:
    """In-memory provider so the example doesn't depend on yfinance."""

    def __init__(self) -> None:
        self.prices: dict[str, float] = {}

    def set(self, symbol: str, price: float) -> None:
        self.prices[symbol] = price

    def get_price(self, symbol: str) -> float | None:
        return self.prices.get(symbol)


def main() -> None:
    stub = StubProvider()
    # short_cache_ttl_seconds=0 so each loop iteration sees the new price.
    feed = PriceFeed(providers=[stub], short_cache_ttl_seconds=0)

    broker = IndiaPaperBroker(
        initial_capital=500_000,
        db_path="examples/_local/backtest.db",
        account_id="backtest",
        price_feed=feed,
        enforce_market_hours=False,
    )
    broker.reset(initial_capital=500_000)

    # Drive a price series for one symbol.
    series: list[float] = [2000, 2050, 2120, 2080, 2200, 2150, 2300]
    held = 0

    for day, price in enumerate(series):
        stub.set("HDFCBANK", price)

        # Trivial strategy: buy 1 share at start, sell all at end.
        if day == 0:
            order = broker.buy("HDFCBANK", qty=10)
            held = 10
            print(f"Day {day}: BUY 10 @ ₹{order.filled_avg_price:,.2f}, "
                  f"fees ₹{order.fees_paid:.2f}")
        elif day == len(series) - 1:
            order = broker.sell("HDFCBANK", qty=held)
            print(f"Day {day}: SELL {held} @ ₹{order.filled_avg_price:,.2f}, "
                  f"realized P&L ₹{order.realized_pl:,.2f}")
        else:
            mtm = held * price
            print(f"Day {day}: hold {held} @ ₹{price:,.2f} "
                  f"(MTM ₹{mtm:,.2f})")

    print("\nFinal account:", broker.get_account())


if __name__ == "__main__":
    main()
