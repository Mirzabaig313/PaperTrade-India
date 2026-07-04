"""Abstract broker interface.

Callers talk to brokers exclusively through ``BrokerInterface``, which
keeps different broker backends (this NSE/BSE paper broker, an
Alpaca-style US adapter, a live Zerodha/IBKR adapter, ...) swappable
without touching caller code.

Notes on the contract:

- All methods are synchronous. Order placement returns immediately with
  the resulting ``Order``. For paper market orders, the returned order is
  already ``FILLED``. For limit orders, status is ``PENDING`` until the
  market crosses the limit price.

- Failure modes are exceptions (not return codes): ``InsufficientFundsError``,
  ``InsufficientSharesError``, ``MarketClosedError``, ``InvalidOrderError``,
  ``PriceUnavailableError``. Callers should expect these and handle them.

- ``cancel_order`` returns ``True`` when the order moved from ``PENDING``
  to ``CANCELLED``, ``False`` otherwise (already filled, already cancelled,
  or unknown ID).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from .domain.models import Account, Order, OrderStatus, OrderType, Position


class BrokerInterface(ABC):
    """Common interface for all broker backends (paper, live, adapters)."""

    # ── Order placement ────────────────────────────────────────────────

    @abstractmethod
    def buy(
        self,
        symbol: str,
        qty: float,
        order_type: OrderType = OrderType.MARKET,
        limit_price: float | None = None,
        time_in_force: str = "DAY",
    ) -> Order:
        """Submit a buy order."""

    @abstractmethod
    def sell(
        self,
        symbol: str,
        qty: float,
        order_type: OrderType = OrderType.MARKET,
        limit_price: float | None = None,
        time_in_force: str = "DAY",
    ) -> Order:
        """Submit a sell order."""

    # ── Read API ───────────────────────────────────────────────────────

    @abstractmethod
    def get_positions(self) -> list[Position]:
        """All open positions with current price and P&L."""

    @abstractmethod
    def get_position(self, symbol: str) -> Position | None:
        """Single position lookup by symbol, or ``None`` if not held."""

    @abstractmethod
    def get_account(self) -> Account:
        """Account summary: cash, equity, buying power."""

    @abstractmethod
    def get_orders(
        self,
        status: OrderStatus | None = None,
        limit: int = 100,
    ) -> list[Order]:
        """Order history, optionally filtered by status. Most recent first."""

    @abstractmethod
    def get_order(self, order_id: str) -> Order | None:
        """Single order lookup by ID, or ``None`` if not found."""

    # ── Order management ───────────────────────────────────────────────

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """Cancel a pending order. Returns ``True`` if the cancel succeeded."""

    @abstractmethod
    def cancel_all_orders(self) -> int:
        """Cancel all pending orders. Returns the count cancelled."""
