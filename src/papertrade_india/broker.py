"""IndiaPaperBroker — thin orchestrator over cohesive subsystems.

Drop-in replacement for an Alpaca-style ``TradingService``: same method
signatures, same dataclass-shaped return values. Plug into a broker router
keyed on ``market`` and the agent's call sites don't change.

Architecture
------------
This class is a façade. Every method delegates to a subsystem module:

- Order placement → :mod:`orders.submit`
- Market fills → :mod:`orders.market`
- Limit fills → :mod:`orders.limit`
- Stop / bracket / AMO → :mod:`orders.stop`, :mod:`orders.bracket`, :mod:`orders.amo`
- Pre-open auction → :mod:`orders.preopen`
- Order state (cancel, expire, square-off) → :mod:`orders.state`
- Read-only views → :mod:`reads.positions`, :mod:`reads.account`, :mod:`reads.orders`
- Corporate actions → :mod:`corporate_actions.splits` etc.
- Shared collaborators → :class:`_context.BrokerContext`

See ``docs/architecture_refactor.md`` for the full design rationale.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime

from ._context import BrokerContext
from .corporate_actions import bonus as _ca_bonus
from .corporate_actions import dividends as _ca_dividends
from .corporate_actions import rights as _ca_rights
from .corporate_actions import splits as _ca_splits
from .domain.exceptions import AccountNotFoundError
from .domain.models import (
    Account,
    Exchange,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    ProductType,
)
from .domain.rules.risk import RiskConfig, RiskEngine
from .domain.rules.tick_lot_band import MicrostructureConfig
from .execution.book import OrderBookConfig, OrderBookSimulator
from .execution.fees import FeeConfig, FeeSchedule, IndianFeeEngine
from .execution.settlement import SettlementConfig, SettlementEngine
from .execution.simulation import (
    LatencyConfig,
    LatencySimulator,
    RejectionConfig,
    RejectionSimulator,
)
from .execution.slippage import SlippageConfig
from .infrastructure import events as _events
from .infrastructure import ledger as _ledger
from .infrastructure.clock import Clock, WallClock
from .infrastructure.market_hours import NSECalendar, SessionPhase
from .infrastructure.observability import BrokerEvent, EventBus
from .infrastructure.persistence import PathLike, Persistence
from .infrastructure.symbols import SymbolMaster
from .infrastructure.watchlist import WatchlistStore
from .interface import BrokerInterface
from .orders import amo as _amo
from .orders import state as _orders_state
from .orders.partial_fills import PartialFillConfig
from .orders.preopen import AuctionMatch
from .orders.submit import submit_order as _submit_order_fn
from .price_feed import PriceFeed
from .reads import account as _reads_account
from .reads import orders as _reads_orders
from .reads import positions as _reads_positions

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
        enforce_fresh_prices: bool = True,
        partial_fill_config: PartialFillConfig | None = None,
        event_bus: EventBus | None = None,
        clock: Clock | None = None,
        microstructure_config: MicrostructureConfig | None = None,
        order_book_config: OrderBookConfig | None = None,
        settlement_config: SettlementConfig | None = None,
        latency_config: LatencyConfig | None = None,
        rejection_config: RejectionConfig | None = None,
        mark_to_bid: bool = True,
        enforce_real_time: bool = False,
    ) -> None:
        """Construct a broker bound to ``account_id`` in ``db_path``.

        If the account row doesn't exist:
          - ``strict_open=False`` (default): create it with ``initial_capital``.
          - ``strict_open=True``: raise ``AccountNotFoundError``.

        See the class docstring and ``docs/architecture_refactor.md`` for
        the full parameter reference.
        """
        self.account_id = account_id
        self.default_exchange = exchange
        self.enforce_market_hours = enforce_market_hours
        self.enforce_fresh_prices = enforce_fresh_prices
        self.enforce_real_time = enforce_real_time

        self.persistence = Persistence(db_path)
        self._watchlist = WatchlistStore(self.persistence)
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

        self.slippage_config = slippage_config or SlippageConfig()
        self.risk_engine = RiskEngine(risk_config or RiskConfig())
        self.symbol_master = symbol_master or SymbolMaster(strict=False)

        self.partial_fill_config = partial_fill_config or PartialFillConfig(
            enabled=True, max_pct_per_tick=0.25, min_fill_qty=1,
        )
        self.events: EventBus = event_bus or EventBus()
        self._pending_events: list[BrokerEvent] = []
        self._clock: Clock = clock or WallClock()

        self.microstructure_config = microstructure_config or MicrostructureConfig()
        self._book_sim = OrderBookSimulator(order_book_config or OrderBookConfig())
        self.settlement = SettlementEngine(settlement_config or SettlementConfig())
        self._latency_sim = LatencySimulator(latency_config or LatencyConfig())
        self._reject_sim = RejectionSimulator(rejection_config or RejectionConfig())
        self.mark_to_bid = bool(mark_to_bid)

        # Shared context handed to every subsystem function.
        self._ctx = BrokerContext(
            account_id=self.account_id,
            default_exchange=self.default_exchange,
            persistence=self.persistence,
            price_feed=self.price_feed,
            calendar=self.calendar,
            fee_schedule=self.fee_schedule,
            slippage_config=self.slippage_config,
            risk_engine=self.risk_engine,
            symbol_master=self.symbol_master,
            microstructure_config=self.microstructure_config,
            book_sim=self._book_sim,
            settlement=self.settlement,
            latency_sim=self._latency_sim,
            reject_sim=self._reject_sim,
            partial_fill_config=self.partial_fill_config,
            events=self.events,
            clock=self._clock,
            enforce_market_hours=self.enforce_market_hours,
            enforce_fresh_prices=self.enforce_fresh_prices,
            mark_to_bid=self.mark_to_bid,
            enforce_real_time=self.enforce_real_time,
            pending_events=self._pending_events,
        )

        self._ensure_account_exists(initial_capital, strict_open=strict_open)

    # ── Clock / fee helpers (kept for backwards-compat callers) ───────

    def _fee_engine_for(self, when_iso: str) -> IndianFeeEngine:
        d = datetime.fromisoformat(when_iso).date()
        return IndianFeeEngine(self.fee_schedule.config_on(d))

    @property
    def fee_engine(self) -> IndianFeeEngine:
        """Backwards-compat: fee engine for today (IST)."""
        return IndianFeeEngine(self.fee_schedule.config_on(self._clock.now().date()))

    def _now_iso(self) -> str:
        return self._clock.now().isoformat()

    @property
    def clock(self) -> Clock:
        return self._clock

    # ── Event helpers (kept for reset() and backwards-compat) ─────────

    def _emit_event(
        self,
        conn: sqlite3.Connection,
        event_type: str,
        order_id: str | None = None,
        payload: dict | None = None,
    ) -> None:
        self._ctx.emit(conn, event_type=event_type,
                       order_id=order_id, payload=payload)

    def _drain_pending_events(self) -> None:
        self._ctx.drain_pending_events()

    # ── Account lifecycle ──────────────────────────────────────────────

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
        stop_price: float | None = None,
        target_price: float | None = None,
        product_type: ProductType = ProductType.DELIVERY,
    ) -> Order:
        return _submit_order_fn(
            self._ctx, symbol, qty, OrderSide.BUY, order_type, limit_price,
            time_in_force, idempotency_key=idempotency_key,
            stop_price=stop_price, target_price=target_price,
            product_type=product_type,
        )

    def sell(
        self,
        symbol: str,
        qty: float,
        order_type: OrderType = OrderType.MARKET,
        limit_price: float | None = None,
        time_in_force: str = "DAY",
        idempotency_key: str | None = None,
        stop_price: float | None = None,
        target_price: float | None = None,
        product_type: ProductType = ProductType.DELIVERY,
    ) -> Order:
        return _submit_order_fn(
            self._ctx, symbol, qty, OrderSide.SELL, order_type, limit_price,
            time_in_force, idempotency_key=idempotency_key,
            stop_price=stop_price, target_price=target_price,
            product_type=product_type,
        )

    # ── Public API: reads ──────────────────────────────────────────────

    def get_positions(self) -> list[Position]:
        return _reads_positions.list_all(self)

    def get_position(self, symbol: str) -> Position | None:
        return _reads_positions.get(self, symbol)

    def _mark_price(self, symbol: str) -> tuple[float, str]:
        return _reads_positions.mark_price(self, symbol)

    def get_account(self) -> Account:
        return _reads_account.summary(self)

    def get_orders(
        self,
        status: OrderStatus | None = None,
        limit: int = 100,
    ) -> list[Order]:
        return _reads_orders.list_all(self, status=status, limit=limit)

    def get_order(self, order_id: str) -> Order | None:
        return _reads_orders.get(self, order_id)

    # ── Public API: watchlist (UI favorites, SQLite-backed) ────────────
    def get_watchlist(self) -> list[str]:
        """Return the saved watchlist symbols in user order."""
        return self._watchlist.list_symbols()

    def set_watchlist(self, symbols: list[str]) -> list[str]:
        """Replace the watchlist with ``symbols``; returns the stored list."""
        return self._watchlist.set_symbols(symbols)

    def add_to_watchlist(self, symbol: str) -> None:
        self._watchlist.add(symbol)

    def remove_from_watchlist(self, symbol: str) -> None:
        self._watchlist.remove(symbol)

    # ── Public API: order management ──────────────────────────────────

    def cancel_order(self, order_id: str) -> bool:
        return _orders_state.cancel(self._ctx, order_id)

    def cancel_all_orders(self) -> int:
        return _orders_state.cancel_all(self._ctx)

    def expire_stale_day_orders(self) -> int:
        return _orders_state.expire_day_orders(self._ctx)

    def get_queue_position(
        self,
        symbol: str,
        side: OrderSide,
        price: float,
    ) -> int | None:
        """Public accessor for queued shares-ahead at a price level."""
        if not self._book_sim.config.enabled:
            return None
        from .orders.book_helpers import symbol_microstructure  # noqa: PLC0415
        tick, _, _ = symbol_microstructure(self._ctx, symbol)
        return self._book_sim.queue_position(symbol, side, price, tick)

    # ── Public API: workers' hooks ─────────────────────────────────────

    def fire_amo_orders(self) -> int:
        return _amo.fire_pending(self._ctx)

    def run_pre_open_auction(self) -> AuctionMatch:
        from .orders.preopen import run as _run_auction  # noqa: PLC0415
        return _run_auction(self._ctx)

    def settle_due(self) -> int:
        with self.persistence.transaction() as conn:
            return self.settlement.settle_due(
                conn,
                account_id=self.account_id,
                as_of=self._clock.now().date(),
            )

    def square_off_intraday(self) -> int:
        return _orders_state.square_off_intraday(self._ctx)

    # ── Public API: corporate actions ─────────────────────────────────

    def apply_split(
        self,
        symbol: str,
        ratio_num: int,
        ratio_den: int = 1,
        ex_date: str | None = None,
        notes: str | None = None,
    ) -> str:
        return _ca_splits.apply(self._ctx, symbol, ratio_num, ratio_den,
                                ex_date=ex_date, notes=notes)

    def apply_bonus(
        self,
        symbol: str,
        ratio_num: int,
        ratio_den: int = 1,
        ex_date: str | None = None,
        notes: str | None = None,
    ) -> str:
        return _ca_bonus.apply(self._ctx, symbol, ratio_num, ratio_den,
                               ex_date=ex_date, notes=notes)

    def apply_rights(
        self,
        symbol: str,
        ratio_num: int,
        ratio_den: int,
        subscription_price: float,
        subscribe: bool = False,
        ex_date: str | None = None,
        notes: str | None = None,
    ) -> str:
        return _ca_rights.apply(self._ctx, symbol, ratio_num, ratio_den,
                                subscription_price, subscribe=subscribe,
                                ex_date=ex_date, notes=notes)

    def apply_dividend(
        self,
        symbol: str,
        amount_per_share: float,
        ex_date: str | None = None,
        notes: str | None = None,
    ) -> str:
        return _ca_dividends.apply(self._ctx, symbol, amount_per_share,
                                   ex_date=ex_date, notes=notes)

    # ── Public API: ledger / events / session ─────────────────────────

    def get_cash_movements(self, limit: int = 200) -> list[_ledger.CashMovement]:
        return _reads_account.list_cash_movements(self, limit=limit)

    def get_position_basis_breakdown(self, symbol: str) -> dict | None:
        return _reads_positions.basis_breakdown(self, symbol)

    def verify_cash_invariant(self, tolerance: float = 0.01) -> bool:
        return _reads_account.verify_cash_invariant(self, tolerance=tolerance)

    def get_events(
        self,
        limit: int = 200,
        event_types: tuple[str, ...] | None = None,
    ) -> list[_events.Event]:
        return _reads_account.list_events(self, limit=limit,
                                          event_types=event_types)

    def current_session_phase(self) -> SessionPhase:
        return _reads_account.current_session_phase(self)

    # ── Public API: idempotency cleanup ───────────────────────────────

    def cleanup_idempotency_keys(self, hours: int = 24) -> int:
        """Delete idempotency rows older than ``hours``. Returns count."""
        from datetime import timedelta  # noqa: PLC0415

        from .infrastructure import idempotency as _idempotency  # noqa: PLC0415
        with self.persistence.transaction() as conn:
            return _idempotency.cleanup_expired(conn, ttl=timedelta(hours=hours))

    # ── Public API: account reset ─────────────────────────────────────

    def reset(self, initial_capital: float | None = None) -> None:
        """Reset account to initial state (paper-trading reset)."""
        with self.persistence.transaction() as conn:
            conn.execute("DELETE FROM orders WHERE account_id = ?",
                         (self.account_id,))
            conn.execute("DELETE FROM trades WHERE account_id = ?",
                         (self.account_id,))
            conn.execute("DELETE FROM positions WHERE account_id = ?",
                         (self.account_id,))
            conn.execute("DELETE FROM cash_movements WHERE account_id = ?",
                         (self.account_id,))
            now = self._now_iso()
            if initial_capital is not None:
                conn.execute(
                    "UPDATE account SET cash = ?, realized_pl_total = 0 "
                    "WHERE account_id = ?",
                    (initial_capital, self.account_id),
                )
                _ledger.record(conn, account_id=self.account_id,
                               amount=float(initial_capital),
                               reason="initial_capital", recorded_at_iso=now,
                               notes="Account reset")
            else:
                cash_row = conn.execute(
                    "SELECT cash FROM account WHERE account_id = ?",
                    (self.account_id,),
                ).fetchone()
                conn.execute(
                    "UPDATE account SET realized_pl_total = 0 WHERE account_id = ?",
                    (self.account_id,),
                )
                _ledger.record(conn, account_id=self.account_id,
                               amount=float(cash_row["cash"]),
                               reason="initial_capital", recorded_at_iso=now,
                               notes="Account reset (cash preserved)")
            self._emit_event(conn, event_type="account_reset",
                             payload={"initial_capital": initial_capital})
        self._drain_pending_events()
        logger.info("RESET account %s", self.account_id)

    # ── Backwards-compat shims (remove in v0.3) ───────────────────────

    def _row_to_order(self, row: sqlite3.Row) -> Order:
        """Shim: delegates to orders.state.row_to_order."""
        return _orders_state.row_to_order(row)

    def _execute_limit_fill(
        self,
        order: Order,
        fill_price: float,
        fill_qty: float | None = None,
    ) -> None:
        """Shim: delegates to orders.limit.fill (used by LimitOrderWatcher)."""
        from .orders.limit import fill as _limit_fill  # noqa: PLC0415
        _limit_fill(self._ctx, order, fill_price, fill_qty=fill_qty)

    def _trigger_stop_order(self, order: Order, last_price: float) -> None:
        """Shim: delegates to orders.stop.trigger (used by LimitOrderWatcher)."""
        from .orders.stop import trigger as _stop_trigger  # noqa: PLC0415
        _stop_trigger(self._ctx, order, last_price)

    def _fill_pending_market_parent(self, parent_id: str) -> None:
        """Shim: delegates to orders.market.fill_pending_market."""
        from .orders.market import fill_pending_market  # noqa: PLC0415
        fill_pending_market(self._ctx, parent_id)

    # ── Utilities ──────────────────────────────────────────────────────

    def __repr__(self) -> str:
        return (
            f"IndiaPaperBroker(account_id={self.account_id!r}, "
            f"exchange={self.default_exchange.value}, "
            f"db_path={self.persistence.db_path!r})"
        )

    @property
    def db_path(self) -> str:
        return self.persistence.db_path
