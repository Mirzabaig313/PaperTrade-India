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

from .exceptions import OrderNoLongerPending
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
        if not pending:
            return 0

        # Group by symbol to minimize price fetches.
        symbols = {o.symbol for o in pending}
        prices = {}
        for s in symbols:
            try:
                prices[s] = self.broker.price_feed.get_price(s)
            except Exception as e:  # noqa: BLE001
                logger.warning("Price unavailable for %s: %s", s, e)
                prices[s] = None

        fills = 0
        for order in pending:
            price = prices.get(order.symbol)
            if price is None or order.limit_price is None:
                continue

            should_fill = (
                (order.side == OrderSide.BUY and price <= order.limit_price)
                or (order.side == OrderSide.SELL and price >= order.limit_price)
            )
            if should_fill:
                try:
                    self.broker._execute_limit_fill(order, price)
                    fills += 1
                except OrderNoLongerPending:
                    # Cancelled or expired between our SELECT and fill
                    # attempt; expected, not an error.
                    logger.debug(
                        "Order %s cleared between selection and fill; skip",
                        order.id,
                    )
                except Exception as e:  # noqa: BLE001
                    logger.exception(
                        "Failed to fill limit order %s: %s", order.id, e,
                    )
        return fills

    def stop(self) -> None:
        self._stop_event.set()
