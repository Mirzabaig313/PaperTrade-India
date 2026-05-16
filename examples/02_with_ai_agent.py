"""Plug the broker behind a ``BrokerInterface`` so an AI agent stays
broker-agnostic.

This pattern is the whole point of the package: the agent sees one
interface (``buy``, ``sell``, ``get_positions``, ``get_account``) and
doesn't know whether the underlying broker is Alpaca (US) or
``IndiaPaperBroker`` (NSE/BSE).
"""

from __future__ import annotations

from papertrade_india import (
    BrokerInterface,
    IndiaPaperBroker,
    OrderType,
)


def trade_idea(broker: BrokerInterface, symbol: str, target_alloc: float) -> None:
    """Toy 'agent': allocate up to ``target_alloc`` of cash to ``symbol``."""
    acct = broker.get_account()
    capital = acct.cash * target_alloc
    if capital < 1000:
        print(f"  Skip {symbol}: only ₹{capital:,.2f} of cash budget.")
        return

    # Naive sizing: assume current price ~ ₹2,000. A real agent would
    # use the price feed.
    qty = max(1, int(capital / 2000))
    print(f"  Buying {qty} {symbol} (~₹{capital:,.2f} budget).")
    broker.buy(symbol, qty=qty, order_type=OrderType.MARKET)


def main() -> None:
    broker = IndiaPaperBroker(
        db_path="examples/_local/agent.db",
        account_id="ai-agent",
        enforce_market_hours=False,
    )

    print("Before agent run:")
    print(" ", broker.get_account())

    # Agent-style: hand the abstract interface around.
    for symbol, alloc in [("RELIANCE", 0.2), ("INFY", 0.2), ("TCS", 0.1)]:
        trade_idea(broker, symbol, alloc)

    print("\nAfter agent run:")
    print(" ", broker.get_account())
    for p in broker.get_positions():
        print(f"  {p.symbol}: {p.qty} @ ₹{p.avg_cost:,.2f}")


if __name__ == "__main__":
    main()
