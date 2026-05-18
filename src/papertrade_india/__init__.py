"""
papertrade_india
================

Production-grade simulated broker for NSE/BSE paper trading.

A drop-in replacement for ``TradingService`` (Alpaca): same method
signatures, same dataclass-shaped return values, no agent-side changes
when switching markets.

Public API
----------
- :class:`IndiaPaperBroker` — the broker
- :class:`BrokerInterface` — ABC that this broker and Alpaca-style adapters implement
- Models: :class:`Position`, :class:`Order`, :class:`Account`, :class:`Trade`
- Enums: :class:`OrderType`, :class:`OrderSide`, :class:`OrderStatus`, :class:`Exchange`
- Fee config: :class:`FeeConfig`, :class:`IndianFeeEngine`, :class:`FeeBreakdown`
- Calendar: :class:`NSECalendar`
- Price feed: :class:`PriceFeed`, :class:`YFinanceProvider`, :class:`JugaadDataProvider`
- Limit-order watcher: :class:`LimitOrderWatcher`
- Exceptions: :class:`IndiaPaperBrokerError` and subclasses
"""

from .broker import IndiaPaperBroker
from .corporate_actions import CorporateAction
from .exceptions import (
    AccountNotFoundError,
    IdempotencyConflict,
    IndiaPaperBrokerError,
    InsufficientFundsError,
    InsufficientSharesError,
    InvalidOrderError,
    KillSwitchActive,
    MarketClosedError,
    OrderNoLongerPending,
    PriceUnavailableError,
    RiskViolation,
    StalePriceRejected,
    SymbolDelisted,
    SymbolNotFound,
)
from .fees import FeeBreakdown, FeeConfig, FeeSchedule, IndianFeeEngine
from .interface import BrokerInterface
from .ledger import CashMovement
from .limit_orders import LimitOrderWatcher
from .market_hours import IST, NSECalendar
from .models import (
    Account,
    Exchange,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    Trade,
)
from .price_feed import (
    CachedLastKnownProvider,
    JugaadDataProvider,
    PriceFeed,
    PriceProvider,
    Quote,
    YFinanceProvider,
)
from .risk import RiskConfig, RiskContext, RiskEngine
from .slippage import SlippageConfig, apply_slippage
from .symbols import SymbolEntry, SymbolMaster

__all__ = [
    # Core
    "IndiaPaperBroker",
    "BrokerInterface",
    # Models
    "Account",
    "Order",
    "Position",
    "Trade",
    # Enums
    "Exchange",
    "OrderSide",
    "OrderStatus",
    "OrderType",
    # Fees
    "FeeBreakdown",
    "FeeConfig",
    "FeeSchedule",
    "IndianFeeEngine",
    # Calendar / time
    "IST",
    "NSECalendar",
    # Price feed
    "CachedLastKnownProvider",
    "JugaadDataProvider",
    "PriceFeed",
    "PriceProvider",
    "Quote",
    "YFinanceProvider",
    # Background workers
    "LimitOrderWatcher",
    # Tier-1: slippage / risk / symbols
    "SlippageConfig",
    "apply_slippage",
    "RiskConfig",
    "RiskContext",
    "RiskEngine",
    "SymbolMaster",
    "SymbolEntry",
    # Tier-2: ledger / corporate actions
    "CashMovement",
    "CorporateAction",
    # Exceptions
    "IndiaPaperBrokerError",
    "InsufficientFundsError",
    "InsufficientSharesError",
    "InvalidOrderError",
    "MarketClosedError",
    "OrderNoLongerPending",
    "AccountNotFoundError",
    "PriceUnavailableError",
    "StalePriceRejected",
    "RiskViolation",
    "KillSwitchActive",
    "IdempotencyConflict",
    "SymbolNotFound",
    "SymbolDelisted",
]

__version__ = "0.1.0.dev0"
