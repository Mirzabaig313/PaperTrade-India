"""Order-book interaction helpers shared across order modules.

Thin wrappers over :class:`OrderBookSimulator` that handle the
best-effort semantics: any failure (no rich quote, book empty) falls
back to the slippage-only price so the legacy behavior stays stable.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..domain.models import OrderSide

if TYPE_CHECKING:  # pragma: no cover
    from .._context import BrokerContext

logger = logging.getLogger(__name__)


def symbol_microstructure(
    ctx: "BrokerContext",
    symbol: str,
) -> tuple[float, int, float | None]:
    """Resolve (tick_size, lot_size, daily_band_pct) for ``symbol``.

    Falls back to :class:`MicrostructureConfig` defaults when the
    symbol master has no override.
    """
    cfg = ctx.microstructure_config
    with ctx.persistence.read() as conn:
        entry = ctx.symbol_master.get(conn, symbol, ctx.default_exchange)
    if entry is None:
        return (cfg.default_tick_size, cfg.default_lot_size, cfg.default_band_pct)
    tick = entry.tick_size if entry.tick_size is not None else cfg.default_tick_size
    lot = entry.lot_size if entry.lot_size > 0 else cfg.default_lot_size
    band = (
        entry.daily_band_pct
        if entry.daily_band_pct is not None
        else cfg.default_band_pct
    )
    return (tick, lot, band)


def maybe_apply_book_impact(
    ctx: "BrokerContext",
    symbol: str,
    qty: float,
    side: OrderSide,
    last_price: float,
) -> float:
    """Walk the synthetic book; return VWAP fill price.

    Only kicks in when the provider supplies a real bid/ask. Without
    bid/ask there's no honest book to synthesize — falls back to the
    slippage-only path so legacy semantics remain stable.
    """
    try:
        mq = ctx.price_feed.get_market_quote(symbol)
    except Exception:  # noqa: BLE001
        return last_price
    if mq is None or mq.bid is None or mq.ask is None:
        return last_price
    tick, _, _ = symbol_microstructure(ctx, symbol)
    book = ctx.book_sim.synthesize(
        symbol=symbol, last=last_price, bid=mq.bid, ask=mq.ask,
        adv=float(mq.volume) if mq.volume else None, tick_size=tick,
    )
    fill = ctx.book_sim.walk_book(book, side, int(round(qty)))
    if fill.filled_qty == 0:
        return last_price
    return fill.avg_price


def maybe_join_book_queue(
    ctx: "BrokerContext",
    symbol: str,
    side: OrderSide,
    limit_price: float,
) -> None:
    """Record a queue position so ``get_queue_position`` works.

    Soft-fails on any error; queue tracking is observability, not
    correctness-critical.
    """
    try:
        mq = ctx.price_feed.get_market_quote(symbol)
    except Exception:  # noqa: BLE001
        return
    tick, _, _ = symbol_microstructure(ctx, symbol)
    book = ctx.book_sim.synthesize(
        symbol=symbol, last=mq.last, bid=mq.bid, ask=mq.ask,
        adv=float(mq.volume) if mq.volume else None, tick_size=tick,
    )
    ctx.book_sim.join_queue(symbol, side, limit_price, book)


__all__ = ["symbol_microstructure", "maybe_apply_book_impact", "maybe_join_book_queue"]
