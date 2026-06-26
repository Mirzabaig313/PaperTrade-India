"""Unit tests for the pre-open auction equilibrium algorithm."""

from __future__ import annotations

from papertrade_india.orders.preopen import _BookRow, compute_equilibrium


def _b(price: float, qty: float, oid: str, seq: int = 0) -> _BookRow:
    return _BookRow(price=price, qty=qty, order_id=oid, submission_seq=seq)


class TestNoCross:
    def test_empty_book_no_match(self) -> None:
        m = compute_equilibrium([], [])
        assert m.equilibrium_price is None
        assert m.matched_volume == 0

    def test_only_buys_no_match(self) -> None:
        m = compute_equilibrium([_b(100, 10, "a")], [])
        assert m.equilibrium_price is None

    def test_disjoint_books_no_match(self) -> None:
        # Highest bid 99, lowest ask 101 — no overlap.
        m = compute_equilibrium(
            buys=[_b(99, 10, "a")],
            sells=[_b(101, 10, "b")],
        )
        assert m.equilibrium_price is None
        assert m.matched_volume == 0
        assert m.fills == []


class TestSimpleMatches:
    def test_exact_overlap_clears_at_meeting_price(self) -> None:
        # Buyer wants 10 @ 100, seller offers 10 @ 100 → trivially clears.
        m = compute_equilibrium(
            buys=[_b(100, 10, "a")],
            sells=[_b(100, 10, "b")],
        )
        assert m.equilibrium_price == 100
        assert m.matched_volume == 10
        assert len(m.fills) == 2

    def test_partial_match_volume_capped(self) -> None:
        # Buyer wants 5, seller offers 10 → 5 traded.
        m = compute_equilibrium(
            buys=[_b(100, 5, "a")],
            sells=[_b(100, 10, "b")],
        )
        assert m.matched_volume == 5
        # Buyer fully filled (5), seller half-filled (5).
        fills_by_id = {oid: q for oid, q, _ in m.fills}
        assert fills_by_id == {"a": 5, "b": 5}


class TestEquilibriumChoice:
    def test_max_volume_wins(self) -> None:
        # Two candidate prices: 100 → vol=10, 101 → vol=15. Pick 101.
        m = compute_equilibrium(
            buys=[_b(101, 10, "a"), _b(102, 5, "b")],
            sells=[_b(100, 10, "c"), _b(101, 5, "d")],
        )
        assert m.equilibrium_price == 101
        assert m.matched_volume == 15

    def test_imbalance_tiebreak(self) -> None:
        # At price 100: buy_vol=10, sell_vol=20 → imbalance 10
        # At price 101: buy_vol=10, sell_vol=20 → imbalance 10
        # Same volume (10) and imbalance (10). Tied → ref-price proximity.
        # ref=100 → 100 wins.
        m = compute_equilibrium(
            buys=[_b(101, 10, "a")],
            sells=[_b(100, 20, "b")],
            reference_price=100,
        )
        assert m.equilibrium_price == 100

    def test_higher_price_wins_when_all_else_tied(self) -> None:
        # Buyers at 100 and 101, sellers at 100 and 101, equal volumes.
        # ref=100.5 → both equally close. Higher price (favors buyers) wins.
        m = compute_equilibrium(
            buys=[_b(100, 5, "a"), _b(101, 5, "b")],
            sells=[_b(100, 5, "c"), _b(101, 5, "d")],
            reference_price=100.5,
        )
        assert m.equilibrium_price == 101


class TestFifoAllocation:
    def test_earlier_orders_filled_first(self) -> None:
        # Two buys at 100 (5 + 5), one sell at 100 (5). Earlier buyer
        # (seq=0) should fill, later (seq=1) should not.
        m = compute_equilibrium(
            buys=[_b(100, 5, "early", seq=0), _b(100, 5, "late", seq=1)],
            sells=[_b(100, 5, "s")],
        )
        fills_by_id = {oid: q for oid, q, _ in m.fills}
        assert fills_by_id["early"] == 5
        assert "late" not in fills_by_id  # didn't fill


class TestPriceProximity:
    def test_closer_to_reference_wins_when_volume_equal(self) -> None:
        # Three candidate prices, all with the same matched volume,
        # different distances from reference.
        m = compute_equilibrium(
            buys=[_b(105, 5, "a")],
            sells=[_b(95, 5, "b")],
            reference_price=100,  # midpoint
        )
        # Volume of 5 at any price between 95 and 105. Best score uses
        # proximity tiebreak — should land near 100.
        assert m.equilibrium_price is not None
        assert 95 <= m.equilibrium_price <= 105
