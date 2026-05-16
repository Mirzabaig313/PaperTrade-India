"""Basic usage: open a broker, buy, sell, inspect.

Run::

    python examples/01_basic_usage.py

Note: the buy/sell calls hit yfinance for live prices and require the
NSE to be open (or set ``enforce_market_hours=False`` for a quick demo
outside trading hours).
"""

from __future__ import annotations

import logging

from papertrade_india import IndiaPaperBroker, OrderType


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    broker = IndiaPaperBroker(
        initial_capital=1_000_000.0,
        db_path="examples/_local/basic.db",
        account_id="basic-demo",
        # Set to True in real use; False here so the example runs anytime.
        enforce_market_hours=False,
    )

    print(repr(broker))
    print("Initial account:", broker.get_account())

    # Place a market buy
    order = broker.buy("RELIANCE", qty=5, order_type=OrderType.MARKET)
    print("\nBuy order:", order)
    print("Fees paid:", order.fees_paid)

    print("\nPositions after buy:")
    for p in broker.get_positions():
        print(f"  {p.symbol}: {p.qty} @ avg ₹{p.avg_cost:,.2f} "
              f"(now ₹{p.current_price:,.2f}, P&L ₹{p.unrealized_pl:,.2f})")

    # Sell to realize
    sell_order = broker.sell("RELIANCE", qty=5)
    print("\nSell order:", sell_order)
    print(f"Realized P&L: ₹{sell_order.realized_pl:,.2f}")

    print("\nFinal account:", broker.get_account())


if __name__ == "__main__":
    main()
