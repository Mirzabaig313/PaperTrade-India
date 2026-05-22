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
from .market_hours import SessionPhase
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
        idempotency_cleanup_every: int | None = 100,
        idempotency_ttl_hours: int = 24,
        settle_due_every: int | None = None,
        auto_square_off_intraday: bool = True,
        run_pre_open_auction: bool = True,
        fire_amo_at_open: bool = True,
        expire_day_orders_at_close: bool = True,
    ) -> None:
        """Construct a watcher.

        Parameters
        ----------
        broker:
            The broker to drive.
        interval_seconds:
            Sleep between ticks.
        daemon:
            Whether the thread is a daemon (default True; doesn't block
            process shutdown).
        idempotency_cleanup_every:
            When set, every Nth tick the watcher runs
            ``broker.cleanup_idempotency_keys(idempotency_ttl_hours)`` so
            users get bounded-table-size for free without setting up
            their own cron. ``None`` (default) skips it.
        idempotency_ttl_hours:
            TTL passed to the cleanup call when enabled.
        settle_due_every:
            When set, every Nth tick the watcher runs
            ``broker.settle_due()`` so T+1 rows roll over without an
            external cron.
        auto_square_off_intraday:
            When True, after the configured square-off time the watcher
            calls :meth:`IndiaPaperBroker.square_off_intraday` once per
            session. Default False — the broker's settlement engine
            still tracks the time-of-day; this flag opts in to the
            automatic execution.
        """
        super().__init__(daemon=daemon, name="LimitOrderWatcher")
        self.broker = broker
        self.interval = interval_seconds
        self._stop_event = threading.Event()
        self._idempotency_cleanup_every = idempotency_cleanup_every
        self._idempotency_ttl_hours = idempotency_ttl_hours
        self._settle_due_every = settle_due_every
        self._auto_square_off_intraday = auto_square_off_intraday
        self._run_pre_open_auction = run_pre_open_auction
        self._fire_amo_at_open = fire_amo_at_open
        self._expire_day_orders_at_close = expire_day_orders_at_close
        self._squared_off_today = False
        self._auction_done_today = False
        self._amo_fired_today = False
        self._day_expired_today = False
        self._last_settle_date = None
        self._tick_count = 0

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
        self._tick_count += 1
        # Periodic idempotency cleanup, opt-in.
        if (
            self._idempotency_cleanup_every is not None
            and self._tick_count % self._idempotency_cleanup_every == 0
        ):
            try:
                n = self.broker.cleanup_idempotency_keys(
                    hours=self._idempotency_ttl_hours,
                )
                if n:
                    logger.info(
                        "Idempotency cleanup: pruned %d expired key(s)", n,
                    )
            except Exception as e:  # noqa: BLE001 — never let cleanup kill the loop
                logger.exception("Idempotency cleanup failed: %s", e)

        # Periodic T+1 roll, opt-in.
        if (
            self._settle_due_every is not None
            and self._tick_count % self._settle_due_every == 0
        ):
            self._maybe_settle_due()

        # Daily roll: also settle once per day independent of N-tick cadence,
        # so a watcher running at 5s interval doesn't miss a roll just because
        # the user didn't set ``settle_due_every``.
        today = self.broker.clock.now().date()
        if self._last_settle_date != today:
            self._maybe_settle_due()
            self._last_settle_date = today
            self._squared_off_today = False
            self._auction_done_today = False
            self._amo_fired_today = False
            self._day_expired_today = False

        # Pre-open auction at the PRE_OPEN → REGULAR transition.
        # Run it at the first REGULAR-phase tick of the day.
        # When ``enforce_market_hours`` is off the broker is being
        # driven manually (typical in tests / backtests), so we leave
        # phase-driven housekeeping to the test code rather than firing
        # auction / AMO / expiry against arbitrary wall-clock state.
        phase = self.broker.calendar.current_phase(self.broker.clock.now())
        run_phase_hooks = self.broker.enforce_market_hours
        if (
            run_phase_hooks
            and self._run_pre_open_auction
            and not self._auction_done_today
            and phase == SessionPhase.REGULAR
        ):
            try:
                match = self.broker.run_pre_open_auction()
                if match.matched_volume > 0:
                    logger.info(
                        "Pre-open auction: %d shares matched at ₹%.2f",
                        int(match.matched_volume),
                        match.equilibrium_price or 0.0,
                    )
            except Exception as e:  # noqa: BLE001
                logger.exception("Pre-open auction failed: %s", e)
            self._auction_done_today = True

        # Fire any pending AMO market orders at session open.
        if (
            run_phase_hooks
            and self._fire_amo_at_open
            and not self._amo_fired_today
            and phase == SessionPhase.REGULAR
        ):
            try:
                self.broker.fire_amo_orders()
            except Exception as e:  # noqa: BLE001
                logger.exception("AMO firing failed: %s", e)
            self._amo_fired_today = True

        # Expire DAY orders once we're past close.
        if (
            run_phase_hooks
            and self._expire_day_orders_at_close
            and not self._day_expired_today
            and phase in (SessionPhase.POST_CLOSE, SessionPhase.CLOSED)
        ):
            # Only expire if we're past 15:30 today; CLOSED on a holiday
            # / weekend morning shouldn't expire orders submitted
            # against the previous trading day's session — those should
            # still queue for the next REGULAR open. The simplest
            # heuristic: only expire when CLOSED/POST_CLOSE on what was
            # actually a trading day.
            if self.broker.calendar.is_trading_day(today):
                try:
                    self.broker.expire_stale_day_orders()
                except Exception as e:  # noqa: BLE001
                    logger.exception("DAY-order expiry failed: %s", e)
            self._day_expired_today = True

        # Intraday auto-square-off, opt-in.
        if (
            self._auto_square_off_intraday
            and not self._squared_off_today
            and self.broker.settlement.is_square_off_time(
                self.broker.clock.now(),
            )
        ):
            try:
                n = self.broker.square_off_intraday()
                self._squared_off_today = True
                if n:
                    logger.info(
                        "Auto-squared off %d intraday position(s)", n,
                    )
            except Exception as e:  # noqa: BLE001
                logger.exception("Auto square-off failed: %s", e)

        if self.broker.enforce_market_hours and not self.broker.calendar.is_market_open(
            self.broker.clock.now()
        ):
            return 0

        # Pull every order type the watcher cares about.
        all_pending = self.broker.get_orders(status=OrderStatus.PENDING, limit=1000)
        partials = self.broker.get_orders(
            status=OrderStatus.PARTIALLY_FILLED, limit=1000,
        )

        limits: list = []
        stops: list = []
        bracket_children_pending: list = []
        for o in all_pending + partials:
            if o.order_type == OrderType.LIMIT:
                # Bracket children stay quiet until parent fills.
                if o.parent_order_id is not None:
                    parent = self.broker.get_order(o.parent_order_id)
                    if parent is None or parent.status != OrderStatus.FILLED:
                        bracket_children_pending.append(o)
                        continue
                limits.append(o)
            elif o.order_type in (OrderType.STOP_MARKET, OrderType.STOP_LIMIT):
                # Same gating for stop children.
                if o.parent_order_id is not None:
                    parent = self.broker.get_order(o.parent_order_id)
                    if parent is None or parent.status != OrderStatus.FILLED:
                        bracket_children_pending.append(o)
                        continue
                stops.append(o)

        if not limits and not stops:
            return 0

        # One price fetch per symbol per tick.
        symbols = {o.symbol for o in (limits + stops)}
        prices: dict[str, float | None] = {}
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

        # ── Stop triggers first: a stop firing this tick could cancel a
        # bracket sibling and shorten the limits list we'd otherwise hit.
        for stop in stops:
            price = prices.get(stop.symbol)
            if price is None or stop.stop_price is None:
                continue
            if (
                self.broker.enforce_fresh_prices
                and stop.symbol in stale_symbols
            ):
                continue
            triggered = (
                (stop.side == OrderSide.BUY and price >= stop.stop_price)
                or (stop.side == OrderSide.SELL and price <= stop.stop_price)
            )
            if triggered:
                try:
                    self.broker._trigger_stop_order(stop, price)
                    fills += 1
                except OrderNoLongerPending:
                    logger.debug(
                        "Stop %s cleared between selection and trigger",
                        stop.id,
                    )
                except StalePriceRejected:
                    logger.debug("Stop %s skipped: stale price", stop.id)
                except Exception as e:  # noqa: BLE001
                    logger.exception(
                        "Failed to trigger stop %s: %s", stop.id, e,
                    )

        # ── Limit fills (existing behavior, with bracket-aware gating).
        # Re-pull pending limits because stop triggers may have produced
        # new ones (STOP_LIMIT becomes a LIMIT) or cancelled some
        # (bracket OCO).
        if stops:
            limits = [
                o for o in self.broker.get_orders(
                    status=OrderStatus.PENDING, limit=1000,
                ) + self.broker.get_orders(
                    status=OrderStatus.PARTIALLY_FILLED, limit=1000,
                )
                if o.order_type == OrderType.LIMIT
                and (
                    o.parent_order_id is None
                    or (
                        (parent := self.broker.get_order(o.parent_order_id))
                        is not None and parent.status == OrderStatus.FILLED
                    )
                )
            ]

        for order in limits:
            price = prices.get(order.symbol)
            if price is None or order.limit_price is None:
                continue
            if (
                self.broker.enforce_fresh_prices
                and order.symbol in stale_symbols
            ):
                continue
            should_fill = (
                (order.side == OrderSide.BUY and price <= order.limit_price)
                or (order.side == OrderSide.SELL and price >= order.limit_price)
            )
            if should_fill:
                remaining = order.qty - order.filled_qty
                slice_qty = self.broker.partial_fill_config.fill_qty(remaining)
                if slice_qty <= 0:
                    continue
                try:
                    self.broker._execute_limit_fill(
                        order, price, fill_qty=slice_qty,
                    )
                    fills += 1
                except OrderNoLongerPending:
                    logger.debug(
                        "Order %s cleared between selection and fill",
                        order.id,
                    )
                except StalePriceRejected:
                    logger.debug("Order %s skipped: stale price", order.id)
                except Exception as e:  # noqa: BLE001
                    logger.exception(
                        "Failed to fill limit order %s: %s", order.id, e,
                    )
        return fills

    def _maybe_settle_due(self) -> None:
        """Roll T+1 settlements for the broker. Defensive — never raises."""
        try:
            n = self.broker.settle_due()
            if n:
                logger.info("Settled %d T+1 row(s) on watcher tick", n)
        except Exception as e:  # noqa: BLE001
            logger.exception("settle_due failed: %s", e)

    def stop(self) -> None:
        self._stop_event.set()
