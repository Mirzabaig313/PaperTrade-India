"""Multi-account demo.

Multiple brokers can share one SQLite file safely (WAL mode + per-thread
connections + atomic transactions). Each ``account_id`` is fully isolated
in the data model.
"""

from __future__ import annotations

from papertrade_india import IndiaPaperBroker, PriceFeed


def main() -> None:
    # Shared in-memory price feed so we don't hit the network in this demo.
    feed = PriceFeed(providers=[])  # only the cached provider
    feed.prime("RELIANCE", 2500.0)
    feed.prime("INFY", 1800.0)

    db = "examples/_local/multi.db"

    alice = IndiaPaperBroker(
        initial_capital=300_000, db_path=db, account_id="alice",
        price_feed=feed, enforce_market_hours=False,
    )
    bob = IndiaPaperBroker(
        initial_capital=500_000, db_path=db, account_id="bob",
        price_feed=feed, enforce_market_hours=False,
    )

    alice.reset(initial_capital=300_000)
    bob.reset(initial_capital=500_000)

    alice.buy("RELIANCE", 5)
    bob.buy("INFY", 10)
    bob.buy("RELIANCE", 3)

    print("Alice:", alice.get_account())
    print("Alice positions:", [(p.symbol, p.qty) for p in alice.get_positions()])

    print("\nBob:  ", bob.get_account())
    print("Bob positions:  ", [(p.symbol, p.qty) for p in bob.get_positions()])


if __name__ == "__main__":
    main()
