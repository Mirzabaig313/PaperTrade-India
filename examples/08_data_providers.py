"""Example: composing data providers for higher fidelity.

This script shows the new provider system added in v0.2:

  - The :class:`MarketDataProvider` ABC and rich :class:`MarketQuote`.
  - :class:`CircuitBreakerProvider` to fast-fail repeatedly-failing
    sources.
  - :class:`CompositeProvider` + :class:`MedianAggregation` for a
    median-of-N quote that's harder to skew with a single bad source.
  - :class:`StooqProvider` and :class:`NSEBhavcopyProvider` as new
    free, no-API-key sources.
  - :data:`default_registry` for name-based lookup.

Run me::

    python examples/08_data_providers.py

Nothing here actually places a trade — it's purely a tour of the
provider plumbing so you can see what to feed to ``IndiaPaperBroker``
in your own code.
"""

from __future__ import annotations

import logging
import sys

from papertrade_india import (
    CircuitBreakerProvider,
    CompositeProvider,
    IndiaPaperBroker,
    MedianAggregation,
    NSEBhavcopyProvider,
    PriceFeed,
    StooqProvider,
    YFinanceProvider,
    default_registry,
)


def list_known_providers() -> None:
    """Print every provider the registry knows about."""
    print("\nKnown providers:")
    for name, info in default_registry.all().items():
        installed = name in default_registry.available()
        marker = "✅" if installed else "❌"
        print(f"  {marker} {name:<14} — {info.description}")
        if info.requires_api_key:
            print(f"      (requires API key — see {info.homepage or 'docs'})")


def build_realistic_feed() -> PriceFeed:
    """Build a price feed with circuit breakers + median aggregation.

    The chain looks like:

        composite (median of 3)
            ├── circuit_breaker(yfinance)        — primary, 15-min delayed
            ├── circuit_breaker(stooq)           — EOD CSV, no API key
            └── circuit_breaker(nse_bhavcopy)    — official NSE EOD CSV

    Each leg is independently breakered so one flaky source can't poison
    the others. The composite returns the *median* of whatever live
    quotes came back this tick — a single rogue print can't drag the
    fill price.
    """
    composite = CompositeProvider(
        providers=[
            CircuitBreakerProvider(YFinanceProvider("NS")),
            CircuitBreakerProvider(StooqProvider()),
            CircuitBreakerProvider(NSEBhavcopyProvider(max_lookback_days=5)),
        ],
        aggregation=MedianAggregation(min_providers=1, max_disagreement_bps=200),
    )
    # The PriceFeed is what IndiaPaperBroker consumes. We pass the
    # composite as a single "provider" — the broker doesn't need to
    # know how many sources are inside.
    return PriceFeed(providers=[composite])


def show_quote(feed: PriceFeed, symbol: str) -> None:
    quote = feed.get_market_quote(symbol)
    print(f"\n{symbol}:")
    print(f"  last       = ₹{quote.last:,.2f}")
    print(f"  source     = {quote.source}")
    print(f"  timestamp  = {quote.timestamp.isoformat()}")
    if quote.bid is not None and quote.ask is not None:
        print(f"  bid/ask    = {quote.bid:,.2f} / {quote.ask:,.2f}")
        if quote.spread_bps is not None:
            print(f"  spread     = {quote.spread_bps:.1f} bps")
    if quote.volume is not None:
        print(f"  volume     = {quote.volume:,}")
    if quote.prev_close is not None:
        change_pct = (quote.last - quote.prev_close) / quote.prev_close * 100
        print(f"  prev close = ₹{quote.prev_close:,.2f} ({change_pct:+.2f}%)")


def main() -> int:
    logging.basicConfig(level=logging.WARNING)

    list_known_providers()

    print("\nBuilding a realistic feed (yfinance ⊕ stooq ⊕ nse-bhavcopy)…")
    feed = build_realistic_feed()

    # Plug it into the broker exactly like the legacy single-provider feed.
    broker = IndiaPaperBroker(price_feed=feed, initial_capital=100_000)
    print(f"Broker ready — equity ₹{broker.get_account().equity:,.2f}")

    # Without a network this will just exercise the cache/composite
    # plumbing; with a network you'll see all three sources contribute
    # to the median.
    for symbol in ("RELIANCE", "TCS", "HDFCBANK"):
        try:
            show_quote(feed, symbol)
        except Exception as e:  # noqa: BLE001 — demo: keep walking
            print(f"\n{symbol}: feed unavailable ({type(e).__name__}: {e})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
