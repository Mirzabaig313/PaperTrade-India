"""Limit-order watcher background loop.

Market orders fill instantly. Limit orders need a loop that periodically
checks if the market crossed the limit price.

Run only if the user wants limit-order support — it's opt-in to keep the
MVP simple. Start with::

    watcher = LimitOrderWatcher(broker, interval_seconds=5)
    watcher.start()

and stop with::

    watcher.stop()
    watcher.join()

The loop is a daemon thread by default, so it does not block process
shutdown if you forget to ``stop()`` it.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

from .exceptions import OrderNoLongerPending, StalePriceRejected
from .models import OrderSide, OrderStatus, OrderType

if TYPE_CHECKING:  # avoid runtime circular import
    from .broker import IndiaPaperBroker

logger = logging.getLogger(__name__)


class LimitOrderWatcher(threading.Thread):
    """Periodically fill pending limit orders that have crossed the market.

    Each tick:
      1. Skip if the market is closed.
      2. Fetch all PENDING limit orders for the broker's account.
      3. Get current price for each unique symbol.
      4. Fill BUY limits when ``current_price <= limit_price``.
      5. Fill SELL limits when ``current_price >= limit_price``.

    Failures inside a tick are logged but never bubble — a flaky price
    feed shouldn't kill the watcher.
    """

    def __init__(
        self,
        broker: IndiaPaperBroker,
        interval_seconds: float = 5.0,
        daemon: bool = True,
    ) -> None:
        super().__init__(daemon=daemon, name="LimitOrderWatcher")
        self.broker = broker
        self.interval = interval_seconds
        self._stop_event = threading.Event()

    def run(self) -> None:  # pragma: no cover — exercised via integration
        logger.info(
            "LimitOrderWatcher started (interval=%ss)", self.interval,
        )
        while not self._stop_event.is_set():
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 — defensive
                logger.exception("LimitOrderWatcher tick failed: %s", e)
            self._stop_event.wait(self.interval)

    def tick(self) -> int:
        """Run a single tick. Returns the number of fills.

        Public so tests can drive the watcher deterministically without
        starting a thread.
        """
        if self.broker.enforce_market_hours and not self.broker.calendar.is_market_open():
            return 0

        pending = [
            o
            for o in self.broker.get_orders(status=OrderStatus.PENDING)
            if o.order_type == OrderType.LIMIT
        ]
        # Partially-filled limit orders also need attention each tick.
        for o in self.broker.get_orders(status=OrderStatus.PARTIALLY_FILLED):
            if o.order_type == OrderType.LIMIT:
                pending.append(o)
        if not pending:
            return 0

        # Group by symbol to minimize price fetches.
        symbols = {o.symbol for o in pending}
        prices = {}
        stale_symbols: set[str] = set()
        for s in symbols:
            try:
                quote = self.broker.price_feed.get_quote(s)
                prices[s] = quote.price
                if quote.is_stale:
                    stale_symbols.add(s)
            except Exception as e:  # noqa: BLE001
                logger.warning("Price unavailable for %s: %s", s, e)
                prices[s] = None

        fills = 0
        for order in pending:
            price = prices.get(order.symbol)
            if price is None or order.limit_price is None:
                continue
            # Pre-check: if enforce_fresh_prices is on and this symbol's
            # quote is stale, skip it now rather than letting the broker
            # raise StalePriceRejected from inside _execute_limit_fill.
            # Reduces log noise (the warning is emitted once per stale
            # symbol per tick).
            if (
                self.broker.enforce_fresh_prices
                and order.symbol in stale_symbols
            ):
                logger.debug(
                    "Skip %s: price is stale and enforce_fresh_prices=True",
                    order.id,
                )
                continue

            should_fill = (
                (order.side == OrderSide.BUY and price <= order.limit_price)
                or (order.side == OrderSide.SELL and price >= order.limit_price)
            )
            if should_fill:
                # Compute the slice qty per the partial-fill config.
                remaining = order.qty - order.filled_qty
                slice_qty = self.broker.partial_fill_config.fill_qty(remaining)
                if slice_qty <= 0:
                    # Cap config returned 0 (e.g. min_fill_qty floor).
                    # Wait for the next tick.
                    continue
                try:
                    self.broker._execute_limit_fill(
                        order, price, fill_qty=slice_qty,
                    )
                    fills += 1
                except OrderNoLongerPending:
                    # Cancelled or expired between our SELECT and fill
                    # attempt; expected, not an error.
                    logger.debug(
                        "Order %s cleared between selection and fill; skip",
                        order.id,
                    )
                except StalePriceRejected:
                    # The price went stale between our quote and the
                    # broker's re-quote inside _execute_limit_fill.
                    # Order stays PENDING for the next tick.
                    logger.debug(
                        "Order %s skipped: stale-price rejection", order.id,
                    )
                except Exception as e:  # noqa: BLE001
                    logger.exception(
                        "Failed to fill limit order %s: %s", order.id, e,
                    )
        return fills

    def stop(self) -> None:
        self._stop_event.set()
