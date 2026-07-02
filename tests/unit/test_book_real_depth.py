"""Tests for real-depth order-book construction (book_from_levels).

Verifies the simulator uses a provider's actual L2 ladder (e.g. Upstox
5-level depth) rather than a synthetic one, and extends past the real
depth with a modeled tail so large orders still fill.
"""

from __future__ import annotations

from papertrade_india.domain.models import OrderSide
from papertrade_india.execution.book import OrderBookConfig, OrderBookSimulator


def _sim(levels: int = 10) -> OrderBookSimulator:
    return OrderBookSimulator(OrderBookConfig(levels=levels))


def test_book_uses_real_levels_verbatim() -> None:
    sim = _sim()
    bids = [(100.0, 50), (99.5, 60)]
    asks = [(100.5, 40), (101.0, 70)]
    book = sim.book_from_levels("X", last=100.0, bids=bids, asks=asks, tick_size=0.5)
    # First two levels on each side are exactly what the provider gave.
    assert (book.bids[0].price, book.bids[0].size) == (100.0, 50)
    assert (book.bids[1].price, book.bids[1].size) == (99.5, 60)
    assert (book.asks[0].price, book.asks[0].size) == (100.5, 40)
    assert (book.asks[1].price, book.asks[1].size) == (101.0, 70)


def test_small_order_fills_at_best_level() -> None:
    sim = _sim()
    asks = [(100.5, 40), (101.0, 70)]
    book = sim.book_from_levels("X", 100.0, [(100.0, 50)], asks, tick_size=0.5)
    fill = sim.walk_book(book, OrderSide.BUY, 30)  # < best-level size
    assert fill.fully_filled
    assert fill.avg_price == 100.5  # all at the touch


def test_large_order_walks_multiple_real_levels() -> None:
    sim = _sim()
    asks = [(100.5, 40), (101.0, 70)]
    book = sim.book_from_levels("X", 100.0, [(100.0, 50)], asks, tick_size=0.5)
    fill = sim.walk_book(book, OrderSide.BUY, 100)  # eats both real levels + tail
    assert fill.fully_filled
    # VWAP is above the best ask (walked deeper) → positive impact.
    assert fill.avg_price > 100.5
    assert fill.impact_bps > 0


def test_tail_extends_beyond_real_depth() -> None:
    # Only 2 real levels but config asks for 10 → 8 synthetic tail levels.
    sim = _sim(levels=10)
    book = sim.book_from_levels(
        "X", 100.0, [(100.0, 50), (99.5, 60)], [(100.5, 40), (101.0, 70)],
        tick_size=0.5,
    )
    assert len(book.asks) == 10
    assert len(book.bids) == 10
    # Tail is tick-spaced and decaying from the last real level.
    assert book.asks[2].price == 101.5
    assert book.asks[2].size <= 70


def test_empty_side_yields_no_levels() -> None:
    sim = _sim()
    book = sim.book_from_levels("X", 100.0, [], [], tick_size=0.5)
    assert book.bids == []
    assert book.asks == []
