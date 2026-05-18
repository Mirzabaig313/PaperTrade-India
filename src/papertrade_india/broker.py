"""IndiaPaperBroker — the public broker class.

Drop-in replacement for an Alpaca-style ``TradingService``: same method
signatures, same dataclass-shaped return values. Plug into a broker router
keyed on ``market`` and the agent's call sites don't change.

Design notes
------------
- Multi-account: ``account_id`` parameter keys every row in the schema, so
  multiple agents (or the same agent with multiple personas) can share one
  database file.
- Fees are realistic Indian-broker fees (see ``fees.py``). They reduce cash
  on both buy and sell.
- ``avg_cost`` is the per-share *economic* cost basis: it includes prorated
  buy-side fees. This means ``qty * avg_cost`` always equals total cash
  spent acquiring the position. Realized P&L on a sell is therefore
  ``(price - avg_cost) * qty - sell_fees`` and naturally captures *both*
  sides of fees over a round-trip.
- All order placement runs inside a SQLite ``IMMEDIATE`` transaction so
  partial failures (e.g. a failed insert after the cash UPDATE) roll back
  cleanly.
- Outside market hours, MARKET orders are rejected with ``MarketClosedError``
  but LIMIT orders are queued — same as a real broker that supports AMO
  (after-market orders). Use ``expire_stale_day_orders()`` to sweep DAY
  limit orders at session close.

Tier-1 adds
-----------
- **Slippage**: configurable basis-point slippage on market fills (off
  by default for backwards compatibility — pass ``SlippageConfig(bps=5)``
  to enable). See ``slippage.py``.
- **Risk controls**: pre-trade kill switch, symbol whitelist, per-order
  notional cap, per-position notional and equity-fraction caps. See
  ``risk.py``.
- **Idempotency**: ``buy(...)`` and ``sell(...)`` accept an
  ``idempotency_key``. Re-submitting the same key with the same params
  returns the prior order. Different params → ``IdempotencyConflict``.
- **Symbol master**: optional ``SymbolMaster`` rejects orders for
  delisted symbols (always) and unknown symbols (in strict mode).
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from datetime import datetime

from . import corporate_actions as _corporate_actions
from . import events as _events
from . import idempotency as _idempotency
from . import ledger as _ledger
from .clock import Clock, WallClock
from .exceptions import (
    AccountNotFoundError,
    IdempotencyConflict,
    InsufficientFundsError,
    InsufficientSharesError,
    InvalidOrderError,
    MarketClosedError,
    OrderNoLongerPending,
    StalePriceRejected,
)
from .fees import FeeConfig, FeeSchedule, IndianFeeEngine
from .interface import BrokerInterface
from .market_hours import NSECalendar, SessionPhase
from .models import (
    Account,
    Exchange,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
)
from .observability import BrokerEvent, EventBus
from .partial_fills import PartialFillConfig
from .persistence import PathLike, Persistence
from .price_feed import PriceFeed, Quote
from .risk import RiskConfig, RiskContext, RiskEngine
from .slippage import SlippageConfig, apply_slippage
from .symbols import SymbolMaster

logger = logging.getLogger(__name__)

_QTY_EPSILON = 1e-9


class IndiaPaperBroker(BrokerInterface):
    """Production-grade simulated broker for NSE/BSE paper trading.

    Features
    --------
    - Real NSE/BSE prices via yfinance (with jugaad-data fallback)
    - Realistic Indian fees (STT, GST, exchange, SEBI, stamp, DP)
    - Thread-safe SQLite persistence with WAL mode
    - Market-hours and holiday-calendar awareness
    - Limit-order support (queue + optional ``LimitOrderWatcher``)
    - Multi-account support via ``account_id``

    Examples
    --------
    >>> broker = IndiaPaperBroker(initial_capital=1_000_000)
    >>> broker.buy("RELIANCE", 10)
    >>> positions = broker.get_positions()
    >>> account = broker.get_account()
    """

    def __init__(
        self,
        initial_capital: float = 1_000_000.0,
        db_path: PathLike = "data/india_paper.db",
        account_id: str = "default",
        exchange: Exchange = Exchange.NSE,
        fee_config: FeeConfig | FeeSchedule | None = None,
        price_feed: PriceFeed | None = None,
        calendar: NSECalendar | None = None,
        enforce_market_hours: bool = True,
        strict_open: bool = False,
        slippage_config: SlippageConfig | None = None,
        risk_config: RiskConfig | None = None,
        symbol_master: SymbolMaster | None = None,
        enforce_fresh_prices: bool = False,
        partial_fill_config: PartialFillConfig | None = None,
        event_bus: EventBus | None = None,
        clock: Clock | None = None,
    ) -> None:
        """Construct a broker bound to ``account_id`` in ``db_path``.

        If the account row doesn't exist:
          - ``strict_open=False`` (default): create it with ``initial_capital``.
          - ``strict_open=True``: raise ``AccountNotFoundError``.

        ``strict_open=True`` is what inspection tools (the CLI) should use
        so they don't silently spawn bogus accounts.

        Tier-1 collaborators
        --------------------
        ``slippage_config``: defaults to ``SlippageConfig(bps=0)`` (no
        slippage), preserving backward compatibility. Pass
        ``SlippageConfig(bps=5)`` for a realistic 5-bp impact.

        ``risk_config``: defaults to ``RiskConfig()`` with everything
        disabled (no kill switch, no whitelist, no caps).

        ``symbol_master``: defaults to ``SymbolMaster(strict=False)`` —
        unknown symbols pass through, delisted symbols are rejected. Pass
        ``SymbolMaster(strict=True)`` to require every symbol be
        registered first.

        Tier-2 collaborators
        --------------------
        ``fee_config``: accepts either a bare ``FeeConfig`` (legacy) or
        a ``FeeSchedule`` for date-versioned fee schedules. Bare configs
        are wrapped automatically.

        ``enforce_fresh_prices``: when True, reject order fills whose
        underlying price came from the long-lived stale-price cache.
        Use for autonomous-agent deployments where halting is safer
        than executing on a stale price.

        Tier-3 collaborators
        --------------------
        ``partial_fill_config``: configurable per-tick fill cap on limit
        orders. Defaults to ``PartialFillConfig(enabled=False)`` —
        legacy all-or-nothing fills.

        ``event_bus``: in-process pub/sub for ``BrokerEvent`` callbacks.
        Defaults to a fresh empty bus. Subscribe via ``broker.events``.
        Events are also persisted to the ``events`` table regardless of
        whether the bus has subscribers.
        """
        self.account_id = account_id
        self.default_exchange = exchange
        self.enforce_market_hours = enforce_market_hours
        self.enforce_fresh_prices = enforce_fresh_prices

        self.persistence = Persistence(db_path)
        self.price_feed = price_feed or PriceFeed()
        self.calendar = calendar or NSECalendar()

        schedule: FeeSchedule
        if fee_config is None:
            schedule = FeeSchedule(default=FeeConfig())
        elif isinstance(fee_config, FeeSchedule):
            schedule = fee_config
        else:
            schedule = FeeSchedule(default=fee_config)
        self.fee_schedule = schedule

        self.slippage_config = slippage_config or SlippageConfig(bps=0.0)
        self.risk_engine = RiskEngine(risk_config or RiskConfig())
        self.symbol_master = symbol_master or SymbolMaster(strict=False)

        self.partial_fill_config = partial_fill_config or PartialFillConfig()
        self.events: EventBus = event_bus or EventBus()
        self._pending_events: list[BrokerEvent] = []
        self._clock: Clock = clock or WallClock()

        self._ensure_account_exists(initial_capital, strict_open=strict_open)

    def _fee_engine_for(self, when_iso: str) -> IndianFeeEngine:
        """Build the fee engine for an order's trade date."""
        d = datetime.fromisoformat(when_iso).date()
        return IndianFeeEngine(self.fee_schedule.config_on(d))

    @property
    def fee_engine(self) -> IndianFeeEngine:
        """Backwards-compat shim: the engine for *today* (IST).

        Existing tests and external callers that read ``broker.fee_engine``
        keep working. New code should prefer ``_fee_engine_for(when)``
        so date-versioned schedules apply.
        """
        return IndianFeeEngine(
            self.fee_schedule.config_on(self._clock.now().date())
        )

    # ── Clock helpers ──────────────────────────────────────────────────

    def _now_iso(self) -> str:
        """ISO timestamp of the broker's current 'now' (clock-aware).

        Replaces direct ``self._now_iso()`` calls so a
        ``ReplayClock`` can drive the broker deterministically in
        backtests.
        """
        return self._clock.now().isoformat()

    @property
    def clock(self) -> Clock:
        """The clock this broker uses. WallClock by default; ReplayClock
        for backtests."""
        return self._clock

    # ── Tier-3: event emission ─────────────────────────────────────────

    def _emit_event(
        self,
        conn: sqlite3.Connection,
        event_type: str,
        order_id: str | None = None,
        payload: dict | None = None,
    ) -> None:
        """Persist an event row AND publish to the in-process bus.

        Persistence is part of the same transaction as the state change
        that triggered the event, so the event log can never disagree
        with what was committed. Bus delivery happens after commit
        (caller's responsibility — we collect the BrokerEvent and the
        caller flushes via ``_drain_pending_events``). For the simple
        case where the caller is inside a ``with persistence.transaction()``
        block, see ``_emit_event_now`` which queues for post-commit
        delivery.
        """
        recorded_at = self._now_iso()
        _events.emit(
            conn,
            event_type=event_type,
            account_id=self.account_id,
            order_id=order_id,
            payload=payload or {},
            recorded_at_iso=recorded_at,
        )
        self._pending_events.append(
            BrokerEvent(
                event_type=event_type,
                account_id=self.account_id,
                order_id=order_id,
                payload=dict(payload or {}),
                recorded_at=datetime.fromisoformat(recorded_at),
            )
        )

    def _drain_pending_events(self) -> None:
        """Publish queued events to the bus. Call after each transaction
        commits, so subscribers don't see uncommitted state."""
        if not self._pending_events:
            return
        events_to_send = self._pending_events
        self._pending_events = []
        for ev in events_to_send:
            self.events.publish(ev)

    def _symbol_position_qty(
        self, conn: sqlite3.Connection, symbol: str,
    ) -> float:
        """Return the current qty for (account_id, symbol), or 0.0."""
        row = conn.execute(
            "SELECT qty FROM positions WHERE account_id = ? AND symbol = ?",
            (self.account_id, symbol),
        ).fetchone()
        return row["qty"] if row else 0.0

    def _emit_position_events(
        self,
        conn: sqlite3.Connection,
        order: Order,
        qty_before: bool,
        qty_after: float,
    ) -> None:
        """Emit position_opened / position_closed when the position
        boundary crosses zero. ``qty_before`` is a bool (held / not held);
        we only care about the open/close edges."""
        if not qty_before and qty_after > 0:
            self._emit_event(
                conn,
                event_type="position_opened",
                order_id=order.id,
                payload={"symbol": order.symbol, "qty": qty_after},
            )
        elif qty_before and qty_after <= _QTY_EPSILON:
            self._emit_event(
                conn,
                event_type="position_closed",
                order_id=order.id,
                payload={"symbol": order.symbol},
            )

    # ── Account lifecycle ───────────────────────────────────────────────

    def _ensure_account_exists(
        self, initial_capital: float, strict_open: bool,
    ) -> None:
        with self.persistence.transaction() as conn:
            row = conn.execute(
                "SELECT cash FROM account WHERE account_id = ?",
                (self.account_id,),
            ).fetchone()
            if row is None:
                if strict_open:
                    raise AccountNotFoundError(
                        f"Account {self.account_id!r} does not exist in "
                        f"{self.persistence.db_path}"
                    )
                now = self._now_iso()
                conn.execute(
                    "INSERT INTO account (account_id, cash, created_at) "
                    "VALUES (?, ?, ?)",
                    (self.account_id, float(initial_capital), now),
                )
                _ledger.record(
                    conn,
                    account_id=self.account_id,
                    amount=float(initial_capital),
                    reason="initial_capital",
                    recorded_at_iso=now,
                    notes="Account opened",
                )

    # ── Public API: order placement ────────────────────────────────────

    def buy(
        self,
        symbol: str,
        qty: float,
        order_type: OrderType = OrderType.MARKET,
        limit_price: float | None = None,
        time_in_force: str = "DAY",
        idempotency_key: str | None = None,
    ) -> Order:
        return self._submit_order(
            symbol, qty, OrderSide.BUY, order_type, limit_price,
            time_in_force, idempotency_key,
        )

    def sell(
        self,
        symbol: str,
        qty: float,
        order_type: OrderType = OrderType.MARKET,
        limit_price: float | None = None,
        time_in_force: str = "DAY",
        idempotency_key: str | None = None,
    ) -> Order:
        return self._submit_order(
            symbol, qty, OrderSide.SELL, order_type, limit_price,
            time_in_force, idempotency_key,
        )

    # ── Order execution ─────────────────────────────────────────────────

    def _submit_order(
        self,
        symbol: str,
        qty: float,
        side: OrderSide,
        order_type: OrderType,
        limit_price: float | None,
        time_in_force: str,
        idempotency_key: str | None = None,
    ) -> Order:
        if qty <= 0:
            raise InvalidOrderError("qty must be positive")
        if order_type == OrderType.LIMIT and limit_price is None:
            raise InvalidOrderError(
                "limit_price required for LIMIT orders"
            )
        if order_type == OrderType.LIMIT and limit_price is not None and limit_price <= 0:
            raise InvalidOrderError("limit_price must be positive")

        if idempotency_key is not None:
            replay = self._idempotency_replay(
                key=idempotency_key,
                side=side,
                symbol=symbol,
                qty=qty,
                order_type=order_type,
                limit_price=limit_price,
                time_in_force=time_in_force,
            )
            if replay is not None:
                return replay

        with self.persistence.read() as conn:
            self.symbol_master.validate(conn, symbol, self.default_exchange)

        risk_price_for_check = (
            limit_price if order_type == OrderType.LIMIT and limit_price is not None
            else self._safe_last_price_for_risk(symbol)
        )
        self._risk_check(side, symbol, qty, risk_price_for_check)

        market_open = self.calendar.is_market_open(self._clock.now())
        if (
            self.enforce_market_hours
            and not market_open
            and order_type == OrderType.MARKET
        ):
            phase = self.calendar.current_phase(self._clock.now())
            raise MarketClosedError(
                f"Cannot fill MARKET order — current phase: {phase.value}. "
                f"Next REGULAR open: {self.calendar.next_open(self._clock.now())}. "
                f"Use a LIMIT order to queue for the next session."
            )

        if order_type == OrderType.MARKET:
            order = self._execute_market_order(symbol, qty, side, time_in_force)
        else:
            assert limit_price is not None  # guarded above
            order = self._queue_limit_order(
                symbol, qty, side, limit_price, time_in_force,
            )

        if idempotency_key is not None:
            self._idempotency_store(
                key=idempotency_key,
                order_id=order.id,
                side=side,
                symbol=symbol,
                qty=qty,
                order_type=order_type,
                limit_price=limit_price,
                time_in_force=time_in_force,
            )

        return order

    # ── Idempotency helpers ────────────────────────────────────────────

    def _idempotency_replay(
        self,
        key: str,
        side: OrderSide,
        symbol: str,
        qty: float,
        order_type: OrderType,
        limit_price: float | None,
        time_in_force: str,
    ) -> Order | None:
        """Look up ``key`` for this account; replay or raise on conflict."""
        with self.persistence.read() as conn:
            entry = _idempotency.lookup(conn, self.account_id, key)
        if entry is None:
            return None

        rh = _idempotency.hash_request(
            side=side.value, symbol=symbol, qty=qty,
            order_type=order_type.value, limit_price=limit_price,
            time_in_force=time_in_force,
        )
        if entry.request_hash != rh:
            raise IdempotencyConflict(
                f"Idempotency key {key!r} was previously used with "
                f"different parameters. Use a fresh key for new requests."
            )

        order = self.get_order(entry.order_id)
        if order is None:
            logger.warning(
                "Idempotency key %s pointed at missing order %s; replaying as new",
                key, entry.order_id,
            )
            return None
        logger.debug("Idempotency replay: key=%s -> order=%s", key, order.id)
        return order

    def _idempotency_store(
        self,
        key: str,
        order_id: str,
        side: OrderSide,
        symbol: str,
        qty: float,
        order_type: OrderType,
        limit_price: float | None,
        time_in_force: str,
    ) -> None:
        rh = _idempotency.hash_request(
            side=side.value, symbol=symbol, qty=qty,
            order_type=order_type.value, limit_price=limit_price,
            time_in_force=time_in_force,
        )
        with self.persistence.transaction() as conn:
            _idempotency.store(
                conn, self.account_id, key, rh, order_id,
                self._now_iso(),
            )

    def cleanup_idempotency_keys(self, hours: int = 24) -> int:
        """Delete idempotency rows older than ``hours``. Returns count.

        Run from a daily cron / startup hook to keep the table bounded.
        Backend convention is 24-48h.
        """
        from datetime import timedelta
        with self.persistence.transaction() as conn:
            return _idempotency.cleanup_expired(
                conn, ttl=timedelta(hours=hours),
            )

    # ── Risk helpers ───────────────────────────────────────────────────

    def _get_fill_quote(self, symbol: str) -> Quote:
        """Fetch a quote for a fill and apply ``enforce_fresh_prices``.

        The fill path runs through this single helper so the staleness
        rule is consistent across market and limit orders.
        """
        quote = self.price_feed.get_quote(symbol)
        if self.enforce_fresh_prices and quote.is_stale:
            raise StalePriceRejected(
                f"Refusing to fill {symbol} at stale cached price "
                f"₹{quote.price:.2f} (fetched {quote.fetched_at.isoformat()}). "
                f"Disable enforce_fresh_prices=False to allow stale fills."
            )
        return quote

    def _safe_last_price_for_risk(self, symbol: str) -> float:
        """Best-effort price for risk-cap math. Falls back to 0.0 if the
        feed is fully unavailable — that just disables the notional caps
        for this submission, which is the safe fail mode (the actual
        execution path will still raise PriceUnavailableError before
        any state changes)."""
        try:
            return self.price_feed.get_price(symbol)
        except Exception as e:  # noqa: BLE001 — risk pre-check is best-effort
            logger.debug("Risk pre-check: price unavailable for %s: %s", symbol, e)
            return 0.0

    def _risk_check(
        self,
        side: OrderSide,
        symbol: str,
        qty: float,
        price_for_check: float,
    ) -> None:
        """Build a RiskContext for the symbol and run the engine."""
        existing_qty = 0.0
        existing_avg = 0.0
        with self.persistence.read() as conn:
            row = conn.execute(
                "SELECT qty, avg_cost FROM positions "
                "WHERE account_id = ? AND symbol = ?",
                (self.account_id, symbol),
            ).fetchone()
            if row is not None:
                existing_qty = row["qty"]
                existing_avg = row["avg_cost"]
            equity_row = conn.execute(
                "SELECT cash FROM account WHERE account_id = ?",
                (self.account_id,),
            ).fetchone()
            cash = equity_row["cash"] if equity_row else 0.0
        equity = cash + existing_qty * existing_avg

        ctx = RiskContext(
            side=side,
            symbol=symbol,
            qty=qty,
            price=price_for_check,
            existing_qty=existing_qty,
            existing_avg_cost=existing_avg,
            equity=equity,
        )
        try:
            self.risk_engine.check(ctx)
        except Exception as e:
            with self.persistence.transaction() as conn:
                self._emit_event(
                    conn,
                    event_type="order_rejected",
                    payload={
                        "symbol": symbol,
                        "side": side.value,
                        "qty": qty,
                        "reason": type(e).__name__,
                        "detail": str(e),
                    },
                )
            self._drain_pending_events()
            raise

    def _execute_market_order(
        self,
        symbol: str,
        qty: float,
        side: OrderSide,
        time_in_force: str,
    ) -> Order:
        """Fill immediately at slippage-adjusted market price."""
        quote = self._get_fill_quote(symbol)
        last_price = quote.price
        price = apply_slippage(
            self.slippage_config,
            side=side,
            order_type=OrderType.MARKET,
            last_price=last_price,
            symbol=symbol,
        )
        order_id = uuid.uuid4().hex[:12]
        now = self._now_iso()
        fee_engine = self._fee_engine_for(now)
        fees = fee_engine.calculate(side, qty, price, self.default_exchange)

        with self.persistence.transaction() as conn:
            position_existed_before = (
                self._symbol_position_qty(conn, symbol) > 0
            )
            if side == OrderSide.BUY:
                self._apply_buy(
                    conn, symbol, qty, price, fees.total, now,
                    order_id=order_id,
                )
                realized_pl = 0.0
            else:
                realized_pl = self._apply_sell(
                    conn, symbol, qty, price, fees.total, now,
                    order_id=order_id,
                )

            self._record_order(
                conn,
                order_id=order_id,
                symbol=symbol,
                side=side,
                qty=qty,
                order_type=OrderType.MARKET,
                status=OrderStatus.FILLED,
                filled_qty=qty,
                filled_avg_price=price,
                limit_price=None,
                fees_paid=fees.total,
                realized_pl=realized_pl,
                time_in_force=time_in_force,
                created_at=now,
                filled_at=now,
            )
            self._record_trade(
                conn,
                order_id=order_id,
                symbol=symbol,
                side=side,
                qty=qty,
                price=price,
                fees=fees.total,
                realized_pl=realized_pl,
                executed_at=now,
            )

            self._emit_event(
                conn,
                event_type="order_submitted",
                order_id=order_id,
                payload={
                    "symbol": symbol, "side": side.value, "qty": qty,
                    "order_type": OrderType.MARKET.value,
                },
            )
            self._emit_event(
                conn,
                event_type="order_filled",
                order_id=order_id,
                payload={
                    "symbol": symbol, "side": side.value, "qty": qty,
                    "fill_price": price, "fees_paid": fees.total,
                },
            )
            qty_after = self._symbol_position_qty(conn, symbol)
            order_for_events = Order(
                id=order_id, symbol=symbol, exchange=self.default_exchange,
                side=side, qty=qty, order_type=OrderType.MARKET,
                status=OrderStatus.FILLED, filled_qty=qty,
                filled_avg_price=price, limit_price=None,
                fees_paid=fees.total, realized_pl=realized_pl,
            )
            self._emit_position_events(
                conn,
                order=order_for_events,
                qty_before=position_existed_before,
                qty_after=qty_after,
            )

        self._drain_pending_events()

        if side == OrderSide.SELL:
            logger.info(
                "FILL %s %s %s @ ₹%.2f (fees ₹%.2f, realized P&L ₹%.2f)",
                side.value.upper(), qty, symbol, price,
                fees.total, realized_pl,
            )
        else:
            logger.info(
                "FILL %s %s %s @ ₹%.2f (fees ₹%.2f)",
                side.value.upper(), qty, symbol, price, fees.total,
            )

        order = self.get_order(order_id)
        assert order is not None, "order disappeared after commit"
        return order

    def _apply_buy(
        self,
        conn: sqlite3.Connection,
        symbol: str,
        qty: float,
        price: float,
        fees: float,
        now: str,
        order_id: str | None = None,
    ) -> None:
        """Apply a buy: deduct cash, update or create the position.

        ``avg_cost`` includes fees: the new per-share cost basis is
        ``(old_qty*old_avg + qty*price + fees) / new_qty``. This means
        ``qty * avg_cost`` always equals total cash spent on the
        position, and a later sell's realized P&L line — computed as
        ``(price - avg_cost) * qty - sell_fees`` — naturally accounts for
        *both* sides of fees over the round-trip.

        Ledger: writes two cash-movement rows (buy_principal, buy_fees)
        so ``sum(movements) == account.cash`` stays an exact invariant.
        """
        principal = qty * price
        cost = principal + fees
        cash = conn.execute(
            "SELECT cash FROM account WHERE account_id = ?",
            (self.account_id,),
        ).fetchone()["cash"]

        if cost > cash:
            raise InsufficientFundsError(
                f"Need ₹{cost:,.2f} (incl fees ₹{fees:.2f}), have ₹{cash:,.2f}"
            )

        conn.execute(
            "UPDATE account SET cash = cash - ? WHERE account_id = ?",
            (cost, self.account_id),
        )

        _ledger.record(
            conn,
            account_id=self.account_id,
            amount=-principal,
            reason="buy_principal",
            recorded_at_iso=now,
            order_id=order_id,
            symbol=symbol,
        )
        if fees != 0:
            _ledger.record(
                conn,
                account_id=self.account_id,
                amount=-fees,
                reason="buy_fees",
                recorded_at_iso=now,
                order_id=order_id,
                symbol=symbol,
            )

        existing = conn.execute(
            "SELECT qty, avg_cost FROM positions "
            "WHERE account_id = ? AND symbol = ?",
            (self.account_id, symbol),
        ).fetchone()

        if existing:
            old_qty = existing["qty"]
            old_avg = existing["avg_cost"]
            new_qty = old_qty + qty
            new_avg = ((old_avg * old_qty) + (price * qty) + fees) / new_qty
            conn.execute(
                "UPDATE positions SET qty = ?, avg_cost = ? "
                "WHERE account_id = ? AND symbol = ?",
                (new_qty, new_avg, self.account_id, symbol),
            )
        else:
            avg_cost = (price * qty + fees) / qty
            conn.execute(
                "INSERT INTO positions "
                "(account_id, symbol, exchange, qty, avg_cost, entry_date) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    self.account_id, symbol, self.default_exchange.value,
                    qty, avg_cost, now,
                ),
            )

    def _apply_sell(
        self,
        conn: sqlite3.Connection,
        symbol: str,
        qty: float,
        price: float,
        fees: float,
        now: str,
        order_id: str | None = None,
    ) -> float:
        """Apply a sell: credit cash, update or close position, return realized P&L.

        Realized P&L = ``(price - avg_cost) * qty - sell_fees``. Because
        ``avg_cost`` already includes prorated buy-side fees, this is the
        true round-trip P&L net of all fees.

        Ledger: writes two cash-movement rows (sell_principal positive,
        sell_fees negative) so ``sum(movements) == account.cash`` stays
        exact across round-trips.
        """
        existing = conn.execute(
            "SELECT qty, avg_cost FROM positions "
            "WHERE account_id = ? AND symbol = ?",
            (self.account_id, symbol),
        ).fetchone()

        if not existing or existing["qty"] < qty:
            held = existing["qty"] if existing else 0
            raise InsufficientSharesError(
                f"Want to sell {qty} of {symbol}, hold {held}"
            )

        old_qty = existing["qty"]
        avg_cost = existing["avg_cost"]
        principal = qty * price
        proceeds = principal - fees
        realized_pl = (price - avg_cost) * qty - fees

        conn.execute(
            "UPDATE account SET cash = cash + ?, "
            "realized_pl_total = realized_pl_total + ? "
            "WHERE account_id = ?",
            (proceeds, realized_pl, self.account_id),
        )

        _ledger.record(
            conn,
            account_id=self.account_id,
            amount=principal,
            reason="sell_principal",
            recorded_at_iso=now,
            order_id=order_id,
            symbol=symbol,
        )
        if fees != 0:
            _ledger.record(
                conn,
                account_id=self.account_id,
                amount=-fees,
                reason="sell_fees",
                recorded_at_iso=now,
                order_id=order_id,
                symbol=symbol,
            )

        new_qty = old_qty - qty
        if new_qty <= _QTY_EPSILON:
            conn.execute(
                "DELETE FROM positions "
                "WHERE account_id = ? AND symbol = ?",
                (self.account_id, symbol),
            )
        else:
            conn.execute(
                "UPDATE positions SET qty = ? "
                "WHERE account_id = ? AND symbol = ?",
                (new_qty, self.account_id, symbol),
            )

        return realized_pl

    def _queue_limit_order(
        self,
        symbol: str,
        qty: float,
        side: OrderSide,
        limit_price: float,
        time_in_force: str,
    ) -> Order:
        order_id = uuid.uuid4().hex[:12]
        now = self._now_iso()

        with self.persistence.transaction() as conn:
            self._record_order(
                conn,
                order_id=order_id,
                symbol=symbol,
                side=side,
                qty=qty,
                order_type=OrderType.LIMIT,
                status=OrderStatus.PENDING,
                filled_qty=0.0,
                filled_avg_price=None,
                limit_price=limit_price,
                fees_paid=0.0,
                realized_pl=0.0,
                time_in_force=time_in_force,
                created_at=now,
                filled_at=None,
            )
            self._emit_event(
                conn,
                event_type="order_submitted",
                order_id=order_id,
                payload={
                    "symbol": symbol, "side": side.value, "qty": qty,
                    "order_type": OrderType.LIMIT.value,
                    "limit_price": limit_price,
                },
            )

        self._drain_pending_events()

        logger.info(
            "QUEUE LIMIT %s %s %s @ ₹%.2f",
            side.value.upper(), qty, symbol, limit_price,
        )
        order = self.get_order(order_id)
        assert order is not None
        return order

    def _execute_limit_fill(
        self,
        order: Order,
        fill_price: float,
        fill_qty: float | None = None,
    ) -> None:
        """Called by ``LimitOrderWatcher`` when market crosses limit price.

        Race-safety: between selecting the order and applying the fill,
        another thread might have cancelled or expired it. We claim the
        order first (``UPDATE ... WHERE status='pending' OR status='partially_filled'``),
        and only if ``rowcount == 1`` do we apply the cash/position changes.
        ``OrderNoLongerPending`` is raised on a lost race so the watcher
        can skip and move on.

        Slippage on limit fills is opt-in (``SlippageConfig.apply_to_limits``).
        Default behavior fills at the supplied ``fill_price`` (i.e. the
        limit price the watcher determined had been crossed).

        Stale-price reject: when ``enforce_fresh_prices=True`` and the
        watcher's price came from the long-lived stale cache, this raises
        ``StalePriceRejected`` and the order stays PENDING for the next
        tick. The watcher passes its own price; we don't re-quote here.

        Partial fills: ``fill_qty`` (if provided and < remaining qty)
        marks the order as ``PARTIALLY_FILLED`` and updates ``filled_qty``
        rather than transitioning to ``FILLED``. Subsequent ticks fill
        the rest.
        """
        adjusted_price = apply_slippage(
            self.slippage_config,
            side=order.side,
            order_type=OrderType.LIMIT,
            last_price=fill_price,
            limit_price=order.limit_price,
            symbol=order.symbol,
        )
        now = self._now_iso()
        fee_engine = self._fee_engine_for(now)

        remaining = order.qty - order.filled_qty
        if fill_qty is None or fill_qty >= remaining:
            slice_qty = remaining
            terminal = True
        else:
            slice_qty = fill_qty
            terminal = False

        if slice_qty <= 0:
            return

        fees = fee_engine.calculate(
            order.side, slice_qty, adjusted_price, order.exchange,
        )

        new_filled_qty = order.filled_qty + slice_qty
        prior_total = order.filled_qty * (order.filled_avg_price or 0.0)
        new_avg_fill = (prior_total + slice_qty * adjusted_price) / new_filled_qty
        new_status = (
            OrderStatus.FILLED if terminal else OrderStatus.PARTIALLY_FILLED
        )
        cumulative_fees = order.fees_paid + fees.total

        with self.persistence.transaction() as conn:
            cur = conn.execute(
                "UPDATE orders SET status = ?, filled_qty = ?, "
                "filled_avg_price = ?, fees_paid = ?, realized_pl = ?, "
                "filled_at = ? "
                "WHERE id = ? AND account_id = ? AND status IN (?, ?)",
                (
                    new_status.value,
                    new_filled_qty,
                    new_avg_fill,
                    cumulative_fees,
                    0.0,  # placeholder; rewritten below for sells
                    now if terminal else None,
                    order.id,
                    self.account_id,
                    OrderStatus.PENDING.value,
                    OrderStatus.PARTIALLY_FILLED.value,
                ),
            )
            if cur.rowcount == 0:
                raise OrderNoLongerPending(
                    f"Order {order.id} is no longer pending; skip"
                )

            position_existed_before = (
                self._symbol_position_qty(conn, order.symbol) > 0
            )

            if order.side == OrderSide.BUY:
                self._apply_buy(
                    conn, order.symbol, slice_qty, adjusted_price,
                    fees.total, now, order_id=order.id,
                )
                slice_realized_pl = 0.0
            else:
                slice_realized_pl = self._apply_sell(
                    conn, order.symbol, slice_qty, adjusted_price,
                    fees.total, now, order_id=order.id,
                )

            cumulative_realized_pl = order.realized_pl + slice_realized_pl
            if cumulative_realized_pl != 0.0:
                conn.execute(
                    "UPDATE orders SET realized_pl = ? "
                    "WHERE id = ? AND account_id = ?",
                    (cumulative_realized_pl, order.id, self.account_id),
                )

            self._record_trade(
                conn,
                order_id=order.id,
                symbol=order.symbol,
                side=order.side,
                qty=slice_qty,
                price=adjusted_price,
                fees=fees.total,
                realized_pl=slice_realized_pl,
                executed_at=now,
            )

            position_qty_after = self._symbol_position_qty(conn, order.symbol)
            self._emit_position_events(
                conn,
                order=order,
                qty_before=position_existed_before,
                qty_after=position_qty_after,
            )
            if terminal:
                self._emit_event(
                    conn,
                    event_type="order_filled",
                    order_id=order.id,
                    payload={
                        "symbol": order.symbol,
                        "side": order.side.value,
                        "qty": new_filled_qty,
                        "fill_price": adjusted_price,
                        "fees_paid": cumulative_fees,
                    },
                )
            else:
                self._emit_event(
                    conn,
                    event_type="order_partially_filled",
                    order_id=order.id,
                    payload={
                        "symbol": order.symbol,
                        "side": order.side.value,
                        "slice_qty": slice_qty,
                        "filled_qty": new_filled_qty,
                        "remaining_qty": order.qty - new_filled_qty,
                        "slice_price": adjusted_price,
                    },
                )

        self._drain_pending_events()

        if terminal:
            logger.info(
                "LIMIT FILL %s %s %s @ ₹%.2f (limit was ₹%.2f)",
                order.side.value.upper(), order.qty, order.symbol,
                adjusted_price, order.limit_price,
            )
        else:
            logger.info(
                "LIMIT PARTIAL %s %s/%s %s @ ₹%.2f (limit was ₹%.2f)",
                order.side.value.upper(),
                slice_qty, order.qty - new_filled_qty + slice_qty,
                order.symbol, adjusted_price, order.limit_price,
            )

    # ── Read API ────────────────────────────────────────────────────────

    def get_positions(self) -> list[Position]:
        with self.persistence.read() as conn:
            rows = conn.execute(
                "SELECT symbol, exchange, qty, avg_cost, entry_date "
                "FROM positions WHERE account_id = ?",
                (self.account_id,),
            ).fetchall()

        positions: list[Position] = []
        for row in rows:
            stale = False
            try:
                price = self.price_feed.get_price(row["symbol"])
            except Exception as e:  # noqa: BLE001 — network-volatile
                # PriceFeed already exhausts yfinance → jugaad → cache.
                # If we landed here, even the long-lived cache was empty.
                # Fall back to avg_cost so the row renders, and flag stale
                # so callers don't mistake "shows 0% P&L" for "real 0% P&L".
                logger.warning(
                    "Position %s: price unavailable, using avg_cost. %s",
                    row["symbol"], e,
                )
                price = row["avg_cost"]
                stale = True

            mv = price * row["qty"]
            cb = row["avg_cost"] * row["qty"]
            positions.append(
                Position(
                    symbol=row["symbol"],
                    exchange=Exchange(row["exchange"]),
                    qty=row["qty"],
                    avg_cost=row["avg_cost"],
                    current_price=price,
                    market_value=mv,
                    cost_basis=cb,
                    unrealized_pl=mv - cb,
                    unrealized_pl_percent=(
                        ((mv - cb) / cb * 100) if cb > 0 else 0.0
                    ),
                    entry_date=datetime.fromisoformat(row["entry_date"]),
                    current_price_stale=stale,
                )
            )
        return positions

    def get_position(self, symbol: str) -> Position | None:
        """Direct O(1) lookup against the (account_id, symbol) primary key."""
        with self.persistence.read() as conn:
            row = conn.execute(
                "SELECT symbol, exchange, qty, avg_cost, entry_date "
                "FROM positions WHERE account_id = ? AND symbol = ?",
                (self.account_id, symbol),
            ).fetchone()
        if row is None:
            return None

        stale = False
        try:
            price = self.price_feed.get_price(row["symbol"])
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Position %s: price unavailable, using avg_cost. %s",
                row["symbol"], e,
            )
            price = row["avg_cost"]
            stale = True

        mv = price * row["qty"]
        cb = row["avg_cost"] * row["qty"]
        return Position(
            symbol=row["symbol"],
            exchange=Exchange(row["exchange"]),
            qty=row["qty"],
            avg_cost=row["avg_cost"],
            current_price=price,
            market_value=mv,
            cost_basis=cb,
            unrealized_pl=mv - cb,
            unrealized_pl_percent=(
                ((mv - cb) / cb * 100) if cb > 0 else 0.0
            ),
            entry_date=datetime.fromisoformat(row["entry_date"]),
            current_price_stale=stale,
        )

    def get_account(self) -> Account:
        """Account summary.

        Note on consistency: cash/realized_pl_total and pending-buy
        notional are read from the same connection inside one read
        context to keep the snapshot tight. Mark-to-market on positions
        still calls out to the price feed (network-volatile), so the
        ``equity`` and ``unrealized_pl_total`` fields can drift slightly
        if a fill lands mid-call. Acceptable for paper-trading reads.
        """
        with self.persistence.read() as conn:
            acct_row = conn.execute(
                "SELECT cash, realized_pl_total FROM account "
                "WHERE account_id = ?",
                (self.account_id,),
            ).fetchone()
            pending_buys = conn.execute(
                "SELECT COALESCE(SUM(qty * limit_price), 0) AS notional "
                "FROM orders WHERE account_id = ? AND status = ? "
                "AND side = ? AND order_type = ?",
                (
                    self.account_id, OrderStatus.PENDING.value,
                    OrderSide.BUY.value, OrderType.LIMIT.value,
                ),
            ).fetchone()["notional"]

        cash = acct_row["cash"]
        realized = acct_row["realized_pl_total"]
        positions = self.get_positions()
        portfolio_value = sum(p.market_value for p in positions)
        unrealized = sum(p.unrealized_pl for p in positions)

        return Account(
            account_id=self.account_id,
            equity=cash + portfolio_value,
            cash=cash,
            portfolio_value=portfolio_value,
            buying_power=max(0.0, cash - pending_buys),
            realized_pl_total=realized,
            unrealized_pl_total=unrealized,
            currency="INR",
        )

    def get_orders(
        self,
        status: OrderStatus | None = None,
        limit: int = 100,
    ) -> list[Order]:
        with self.persistence.read() as conn:
            if status is not None:
                rows = conn.execute(
                    "SELECT * FROM orders WHERE account_id = ? AND status = ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (self.account_id, status.value, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM orders WHERE account_id = ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (self.account_id, limit),
                ).fetchall()

        return [self._row_to_order(r) for r in rows]

    def get_order(self, order_id: str) -> Order | None:
        with self.persistence.read() as conn:
            row = conn.execute(
                "SELECT * FROM orders WHERE id = ? AND account_id = ?",
                (order_id, self.account_id),
            ).fetchone()
        return self._row_to_order(row) if row else None

    # ── Order management ───────────────────────────────────────────────

    def cancel_order(self, order_id: str) -> bool:
        """Cancel a pending or partially-filled order. Race-safe."""
        with self.persistence.transaction() as conn:
            cur = conn.execute(
                "UPDATE orders SET status = ?, cancelled_at = ? "
                "WHERE id = ? AND account_id = ? AND status IN (?, ?)",
                (
                    OrderStatus.CANCELLED.value,
                    self._now_iso(),
                    order_id,
                    self.account_id,
                    OrderStatus.PENDING.value,
                    OrderStatus.PARTIALLY_FILLED.value,
                ),
            )
            cancelled = cur.rowcount == 1
            if cancelled:
                self._emit_event(
                    conn,
                    event_type="order_cancelled",
                    order_id=order_id,
                )
        if cancelled:
            self._drain_pending_events()
            logger.info("CANCEL order %s", order_id)
        return cancelled

    def cancel_all_orders(self) -> int:
        pending = self.get_orders(status=OrderStatus.PENDING)
        return sum(1 for o in pending if self.cancel_order(o.id))

    def expire_stale_day_orders(self) -> int:
        """Mark all PENDING DAY-tif orders as EXPIRED in one transaction.

        Call this from a session-close hook (e.g. a cron at 15:30 IST,
        or just before the next session open). Returns the count expired.

        Race-safe: only flips rows currently in PENDING.
        """
        expired_ids: list[str] = []
        with self.persistence.transaction() as conn:
            rows = conn.execute(
                "SELECT id FROM orders "
                "WHERE account_id = ? AND status = ? "
                "AND time_in_force = 'DAY'",
                (self.account_id, OrderStatus.PENDING.value),
            ).fetchall()
            expired_ids = [r["id"] for r in rows]
            cur = conn.execute(
                "UPDATE orders SET status = ?, expired_at = ? "
                "WHERE account_id = ? AND status = ? "
                "AND time_in_force = 'DAY'",
                (
                    OrderStatus.EXPIRED.value,
                    self._now_iso(),
                    self.account_id,
                    OrderStatus.PENDING.value,
                ),
            )
            n = cur.rowcount
            for oid in expired_ids:
                self._emit_event(
                    conn, event_type="order_expired", order_id=oid,
                )
        self._drain_pending_events()
        if n:
            logger.info("EXPIRE %d DAY order(s) on account %s",
                        n, self.account_id)
        return n

    def reset(self, initial_capital: float | None = None) -> None:
        """Reset account to initial state. Equivalent to IBKR paper reset.

        Trades have ON DELETE CASCADE on both ``order_id`` and
        ``account_id``, so deleting orders sweeps trades automatically.
        Cash movements are wiped (account-scoped CASCADE), then a fresh
        ``initial_capital`` row is inserted to keep the ledger
        invariant: ``sum(movements) == account.cash``.
        """
        with self.persistence.transaction() as conn:
            conn.execute(
                "DELETE FROM orders WHERE account_id = ?",
                (self.account_id,),
            )
            conn.execute(
                "DELETE FROM trades WHERE account_id = ?",
                (self.account_id,),
            )
            conn.execute(
                "DELETE FROM positions WHERE account_id = ?",
                (self.account_id,),
            )
            conn.execute(
                "DELETE FROM cash_movements WHERE account_id = ?",
                (self.account_id,),
            )

            now = self._now_iso()
            if initial_capital is not None:
                conn.execute(
                    "UPDATE account SET cash = ?, realized_pl_total = 0 "
                    "WHERE account_id = ?",
                    (initial_capital, self.account_id),
                )
                _ledger.record(
                    conn,
                    account_id=self.account_id,
                    amount=float(initial_capital),
                    reason="initial_capital",
                    recorded_at_iso=now,
                    notes="Account reset",
                )
            else:
                cash_row = conn.execute(
                    "SELECT cash FROM account WHERE account_id = ?",
                    (self.account_id,),
                ).fetchone()
                conn.execute(
                    "UPDATE account SET realized_pl_total = 0 "
                    "WHERE account_id = ?",
                    (self.account_id,),
                )
                _ledger.record(
                    conn,
                    account_id=self.account_id,
                    amount=float(cash_row["cash"]),
                    reason="initial_capital",
                    recorded_at_iso=now,
                    notes="Account reset (cash preserved)",
                )
            self._emit_event(
                conn,
                event_type="account_reset",
                payload={"initial_capital": initial_capital},
            )
        self._drain_pending_events()
        logger.info("RESET account %s", self.account_id)

    # ── Internal helpers ────────────────────────────────────────────────

    def _record_order(
        self,
        conn: sqlite3.Connection,
        order_id: str,
        symbol: str,
        side: OrderSide,
        qty: float,
        order_type: OrderType,
        status: OrderStatus,
        filled_qty: float,
        filled_avg_price: float | None,
        limit_price: float | None,
        fees_paid: float,
        realized_pl: float,
        time_in_force: str,
        created_at: str,
        filled_at: str | None = None,
    ) -> None:
        conn.execute(
            "INSERT INTO orders "
            "(id, account_id, symbol, exchange, side, qty, order_type, "
            "status, filled_qty, filled_avg_price, limit_price, fees_paid, "
            "realized_pl, time_in_force, created_at, filled_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                order_id,
                self.account_id,
                symbol,
                self.default_exchange.value,
                side.value,
                qty,
                order_type.value,
                status.value,
                filled_qty,
                filled_avg_price,
                limit_price,
                fees_paid,
                realized_pl,
                time_in_force,
                created_at,
                filled_at,
            ),
        )

    def _record_trade(
        self,
        conn: sqlite3.Connection,
        order_id: str,
        symbol: str,
        side: OrderSide,
        qty: float,
        price: float,
        fees: float,
        realized_pl: float,
        executed_at: str,
    ) -> None:
        conn.execute(
            "INSERT INTO trades "
            "(id, order_id, account_id, symbol, side, qty, price, fees, "
            "realized_pl, executed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                uuid.uuid4().hex[:12],
                order_id,
                self.account_id,
                symbol,
                side.value,
                qty,
                price,
                fees,
                realized_pl,
                executed_at,
            ),
        )

    def _row_to_order(self, row: sqlite3.Row) -> Order:
        return Order(
            id=row["id"],
            symbol=row["symbol"],
            exchange=Exchange(row["exchange"]),
            side=OrderSide(row["side"]),
            qty=row["qty"],
            order_type=OrderType(row["order_type"]),
            status=OrderStatus(row["status"]),
            filled_qty=row["filled_qty"] or 0.0,
            filled_avg_price=row["filled_avg_price"],
            limit_price=row["limit_price"],
            fees_paid=row["fees_paid"] or 0.0,
            realized_pl=row["realized_pl"] or 0.0,
            time_in_force=row["time_in_force"] or "DAY",
            created_at=datetime.fromisoformat(row["created_at"]),
            filled_at=(
                datetime.fromisoformat(row["filled_at"])
                if row["filled_at"] else None
            ),
            cancelled_at=(
                datetime.fromisoformat(row["cancelled_at"])
                if row["cancelled_at"] else None
            ),
            expired_at=(
                datetime.fromisoformat(row["expired_at"])
                if row["expired_at"] else None
            ),
            rejection_reason=row["rejection_reason"],
        )

    # ── Tier-2: corporate actions ──────────────────────────────────────

    def apply_split(
        self,
        symbol: str,
        ratio_num: int,
        ratio_den: int = 1,
        ex_date: str | None = None,
        notes: str | None = None,
    ) -> str:
        """Apply a stock split / bonus issue to the broker's holding.

        Parameters
        ----------
        symbol:
            The split symbol.
        ratio_num, ratio_den:
            New shares per old. A 2:1 split is ``ratio_num=2``,
            ``ratio_den=1`` (qty doubles, avg_cost halves). A 1:1 bonus
            is the same as 2:1 split (one new share per share held).
            A 1:5 reverse split is ``ratio_num=1, ratio_den=5``.
        ex_date:
            ISO date string. Defaults to today (IST).
        notes:
            Optional free-text annotation stored on the action row.

        Returns the action id. Idempotency is *not* enforced — calling
        twice applies the split twice. Wrap in your own dedup if needed.
        """
        from fractions import Fraction

        if ratio_num <= 0 or ratio_den <= 0:
            raise ValueError("ratio components must be positive integers")
        ratio = Fraction(ratio_num, ratio_den)
        ex_date = ex_date or self._clock.now().date().isoformat()
        now = self._now_iso()

        with self.persistence.transaction() as conn:
            action_id = _corporate_actions.record_split(
                conn,
                symbol=symbol,
                exchange=self.default_exchange.value,
                ratio=ratio,
                ex_date=ex_date,
                applied_at_iso=now,
                notes=notes,
            )
            existing = conn.execute(
                "SELECT qty, avg_cost FROM positions "
                "WHERE account_id = ? AND symbol = ?",
                (self.account_id, symbol),
            ).fetchone()
            if existing is None:
                logger.info(
                    "SPLIT %s %d:%d recorded; no holding to adjust",
                    symbol, ratio_num, ratio_den,
                )
                return action_id

            old_qty = existing["qty"]
            old_avg = existing["avg_cost"]
            new_qty = old_qty * (ratio_num / ratio_den)
            new_avg = old_avg * (ratio_den / ratio_num)
            conn.execute(
                "UPDATE positions SET qty = ?, avg_cost = ? "
                "WHERE account_id = ? AND symbol = ?",
                (new_qty, new_avg, self.account_id, symbol),
            )
            self._emit_event(
                conn,
                event_type="corporate_action",
                payload={
                    "type": "split",
                    "symbol": symbol,
                    "ratio_num": ratio_num,
                    "ratio_den": ratio_den,
                    "ex_date": ex_date,
                    "old_qty": old_qty,
                    "new_qty": new_qty,
                },
            )

        self._drain_pending_events()
        logger.info(
            "SPLIT %s %d:%d applied to %s: %g → %g shares (avg ₹%.4f → ₹%.4f)",
            symbol, ratio_num, ratio_den, self.account_id,
            old_qty, new_qty, old_avg, new_avg,
        )
        return action_id

    def apply_dividend(
        self,
        symbol: str,
        amount_per_share: float,
        ex_date: str | None = None,
        notes: str | None = None,
    ) -> str:
        """Apply a cash dividend to the broker's holding.

        Credits ``amount_per_share * qty_held`` to the account's cash
        on the ex-date. The credit lands in the ledger as a
        ``dividend`` row.

        Tax-aware behavior is *not* modeled: in reality, Indian dividend
        income is taxable to the recipient (and TDS may apply for some
        holders), but that's beyond the scope of a paper simulator.

        Returns the action id. Calling twice on the same dividend
        double-credits — wrap in your own dedup if needed.
        """
        if amount_per_share <= 0:
            raise ValueError("amount_per_share must be positive")
        ex_date = ex_date or self._clock.now().date().isoformat()
        now = self._now_iso()

        with self.persistence.transaction() as conn:
            action_id = _corporate_actions.record_dividend(
                conn,
                symbol=symbol,
                exchange=self.default_exchange.value,
                amount_per_share=amount_per_share,
                ex_date=ex_date,
                applied_at_iso=now,
                notes=notes,
            )
            row = conn.execute(
                "SELECT qty FROM positions "
                "WHERE account_id = ? AND symbol = ?",
                (self.account_id, symbol),
            ).fetchone()
            if row is None or row["qty"] <= 0:
                logger.info(
                    "DIVIDEND %s ₹%.4f/sh recorded; no holding to credit",
                    symbol, amount_per_share,
                )
                return action_id

            credit = row["qty"] * amount_per_share
            conn.execute(
                "UPDATE account SET cash = cash + ? WHERE account_id = ?",
                (credit, self.account_id),
            )
            _ledger.record(
                conn,
                account_id=self.account_id,
                amount=credit,
                reason="dividend",
                recorded_at_iso=now,
                symbol=symbol,
                notes=f"Dividend ₹{amount_per_share}/sh × {row['qty']:g}",
            )
            self._emit_event(
                conn,
                event_type="corporate_action",
                payload={
                    "type": "dividend",
                    "symbol": symbol,
                    "per_share": amount_per_share,
                    "qty": row["qty"],
                    "credit": credit,
                    "ex_date": ex_date,
                },
            )

        self._drain_pending_events()
        logger.info(
            "DIVIDEND %s ₹%.4f/sh credited ₹%.2f to %s",
            symbol, amount_per_share, credit, self.account_id,
        )
        return action_id

    # ── Tier-2: ledger access ──────────────────────────────────────────

    def get_cash_movements(self, limit: int = 200) -> list[_ledger.CashMovement]:
        """Recent cash-ledger rows for this account, newest first."""
        with self.persistence.read() as conn:
            return _ledger.list_for_account(conn, self.account_id, limit=limit)

    def get_position_basis_breakdown(self, symbol: str) -> dict | None:
        """Return the open position's cost basis broken into principal vs fees.

        Useful for reconciling against a broker contract note: ``avg_cost``
        bakes in prorated buy-side fees, but the ledger has the raw
        components separately. We only count buy-side movements that
        haven't been reversed by a subsequent sell.

        Returns ``None`` if no open position exists for ``symbol``.

        Returns a dict with::

            {
                "qty": float,              # current open qty
                "principal": float,        # qty * avg_cost - fees_in_basis
                "fees_in_basis": float,    # prorated buy fees still embedded
                "total_basis": float,      # qty * avg_cost (matches Position.cost_basis)
                "ledger_buy_principal": float,   # gross buy principal from ledger
                "ledger_buy_fees": float,        # gross buy fees from ledger
                "ledger_sell_principal": float,  # gross sell principal from ledger
                "ledger_sell_fees": float,       # gross sell fees from ledger
            }

        Mechanics: ``total_basis = qty * avg_cost`` is the definitive
        figure (avg_cost was maintained against this invariant). We back
        out ``fees_in_basis`` as the share of total ledger fees that's
        still embedded in the open qty — proportional to the unsold
        portion. Sells that closed positions wash everything to zero.
        """
        pos = self.get_position(symbol)
        if pos is None:
            return None

        with self.persistence.read() as conn:
            rows = conn.execute(
                "SELECT reason, COALESCE(SUM(amount), 0) AS total "
                "FROM cash_movements "
                "WHERE account_id = ? AND symbol = ? "
                "GROUP BY reason",
                (self.account_id, symbol),
            ).fetchall()

        by_reason = {r["reason"]: float(r["total"]) for r in rows}
        buy_principal = abs(by_reason.get("buy_principal", 0.0))
        buy_fees = abs(by_reason.get("buy_fees", 0.0))
        sell_principal = abs(by_reason.get("sell_principal", 0.0))
        sell_fees = abs(by_reason.get("sell_fees", 0.0))

        total_basis = pos.qty * pos.avg_cost
        # Buy-side fees still embedded in the open qty: proportional to
        # what remains. If we bought 10 with ₹X fees and sold 3, only
        # 7/10 of those fees are still in the basis.
        # Use the running ledger buy_principal as the denominator (gross).
        if buy_principal > 0:
            open_share = (total_basis / max(buy_principal + buy_fees, 1e-9))
            fees_in_basis = min(buy_fees * open_share, total_basis)
        else:
            fees_in_basis = 0.0
        principal_in_basis = total_basis - fees_in_basis

        return {
            "qty": pos.qty,
            "principal": principal_in_basis,
            "fees_in_basis": fees_in_basis,
            "total_basis": total_basis,
            "ledger_buy_principal": buy_principal,
            "ledger_buy_fees": buy_fees,
            "ledger_sell_principal": sell_principal,
            "ledger_sell_fees": sell_fees,
        }

    def verify_cash_invariant(self, tolerance: float = 0.01) -> bool:
        """Assert ``account.cash == sum(cash_movements.amount)``.

        Returns True if the invariant holds within ``tolerance`` (₹0.01
        absolute by default — paise rounding only).

        Run this from tests, audits, or a periodic health check. A False
        result means there's a code path mutating ``account.cash``
        without writing a matching ledger row, which is a bug.

        On drift (returning False) we also log a structured WARN with
        the magnitude and the most recent ledger rows so the failure
        is debuggable in the wild without needing to re-run with debug
        flags.
        """
        with self.persistence.read() as conn:
            cash = conn.execute(
                "SELECT cash FROM account WHERE account_id = ?",
                (self.account_id,),
            ).fetchone()["cash"]
            ledger_total = _ledger.sum_for_account(conn, self.account_id)

        drift = cash - ledger_total
        if abs(drift) <= tolerance:
            return True

        recent = self.get_cash_movements(limit=5)
        logger.warning(
            "Cash invariant broken on account=%s: cash=%.4f, "
            "sum(movements)=%.4f, drift=%.4f. Recent movements: %s",
            self.account_id, cash, ledger_total, drift,
            [(m.recorded_at.isoformat(), m.reason, m.amount) for m in recent],
        )
        return False

    # ── Tier-3: event log + session phase ──────────────────────────────

    def get_events(
        self,
        limit: int = 200,
        event_types: tuple[str, ...] | None = None,
    ) -> list[_events.Event]:
        """Recent events for this account, newest first.

        ``event_types`` filters by types when provided, e.g.
        ``event_types=("order_filled", "order_partially_filled")``.
        """
        with self.persistence.read() as conn:
            return _events.list_for_account(
                conn, self.account_id, limit=limit, event_types=event_types,
            )

    def current_session_phase(self) -> SessionPhase:
        """The active NSE session phase at the broker's clock."""
        return self.calendar.current_phase(self._clock.now())

    # ── Utilities ───────────────────────────────────────────────────────

    def __repr__(self) -> str:
        return (
            f"IndiaPaperBroker(account_id={self.account_id!r}, "
            f"exchange={self.default_exchange.value}, "
            f"db_path={self.persistence.db_path!r})"
        )

    @property
    def db_path(self) -> str:
        """Convenience accessor used by examples and the CLI."""
        return self.persistence.db_path
