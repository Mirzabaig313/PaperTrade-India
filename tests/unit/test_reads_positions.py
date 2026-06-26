"""Subsystem test for reads/positions (architecture_refactor.md Phase 8).

Exercises the extracted ``reads.positions.mark_price`` mark-to-market
basis logic WITHOUT constructing ``IndiaPaperBroker`` — it uses a tiny
duck-typed stand-in exposing only the two attributes the function
touches (``mark_to_bid`` and ``price_feed``). This keeps the subsystem
testable in isolation, which is the whole point of the BrokerContext /
subsystem split.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from papertrade_india.providers import MarketQuote
from papertrade_india.reads import positions as reads_positions


class _FakeFeed:
    """Stand-in price feed. ``mq`` is the rich quote get_market_quote
    returns (or an exception to raise); ``last`` is the bare fallback."""

    def __init__(self, mq: object = None, last: float = 100.0) -> None:
        self._mq = mq
        self._last = last

    def get_market_quote(self, symbol: str) -> MarketQuote:
        if isinstance(self._mq, Exception):
            raise self._mq
        return self._mq

    def get_price(self, symbol: str) -> float:
        return self._last


class _FakeBroker:
    """Minimal duck-typed broker: only what mark_price reads."""

    def __init__(self, *, mark_to_bid: bool, feed: _FakeFeed) -> None:
        self.mark_to_bid = mark_to_bid
        self.price_feed = feed


def _quote(**kw) -> MarketQuote:
    base = {"last": 100.0, "timestamp": datetime.now(), "source": "test"}
    base.update(kw)
    return MarketQuote(**base)


def test_mark_to_bid_uses_bid_for_long() -> None:
    broker = _FakeBroker(
        mark_to_bid=True,
        feed=_FakeFeed(mq=_quote(bid=99.5, ask=100.5)),
    )
    price, basis = reads_positions.mark_price(broker, "RELIANCE")
    assert price == 99.5
    assert basis == "bid"


def test_mark_to_bid_off_falls_back_to_last() -> None:
    broker = _FakeBroker(
        mark_to_bid=False,
        feed=_FakeFeed(mq=_quote(bid=99.5, ask=100.5), last=100.0),
    )
    price, basis = reads_positions.mark_price(broker, "RELIANCE")
    assert price == 100.0
    assert basis == "last"


def test_mark_to_bid_no_quote_falls_back_to_last() -> None:
    broker = _FakeBroker(mark_to_bid=True, feed=_FakeFeed(mq=None, last=101.0))
    price, basis = reads_positions.mark_price(broker, "RELIANCE")
    assert price == 101.0
    assert basis == "last"


def test_mark_to_bid_quote_error_falls_back_to_last() -> None:
    broker = _FakeBroker(
        mark_to_bid=True,
        feed=_FakeFeed(mq=RuntimeError("feed down"), last=102.0),
    )
    price, basis = reads_positions.mark_price(broker, "RELIANCE")
    assert price == 102.0
    assert basis == "last"


def test_mark_to_bid_missing_bid_uses_mid() -> None:
    # bid present-but-nonpositive is treated as invalid; with an ask
    # available the function uses the mid. (Documents the actual branch.)
    broker = _FakeBroker(
        mark_to_bid=True,
        feed=_FakeFeed(mq=_quote(bid=0.0, ask=100.0)),
    )
    price, basis = reads_positions.mark_price(broker, "RELIANCE")
    assert basis == "mid"
    assert price == pytest.approx(50.0)
