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
- Provider system (new): :class:`MarketDataProvider`, :class:`MarketQuote`, :class:`OHLCV`,
  :class:`StooqProvider`, :class:`NSEBhavcopyProvider`, :class:`CompositeProvider`,
  :class:`CircuitBreakerProvider`, :class:`ProviderRegistry`, :data:`default_registry`
- Limit-order watcher: :class:`LimitOrderWatcher`
- Exceptions: :class:`IndiaPaperBrokerError` and subclasses
"""

from .broker import IndiaPaperBroker
from .clock import Clock, ReplayClock, WallClock
from .corporate_actions import CorporateAction
from .events import Event
from .exceptions import (
    AccountNotFoundError,
    IdempotencyConflict,
    IndiaPaperBrokerError,
    InsufficientFundsError,
    InsufficientSharesError,
    InvalidOrderError,
    KillSwitchActive,
    LotSizeViolation,
    MarketClosedError,
    OrderNoLongerPending,
    PriceBandViolation,
    PriceUnavailableError,
    RandomBrokerRejection,
    RiskViolation,
    SettlementError,
    StalePriceRejected,
    SymbolDelisted,
    SymbolNotFound,
    TickSizeViolation,
)
from .fees import FeeBreakdown, FeeConfig, FeeSchedule, IndianFeeEngine
from .interface import BrokerInterface
from .ledger import CashMovement
from .limit_orders import LimitOrderWatcher
from .market_hours import IST, NSECalendar, SessionPhase
from .microstructure import (
    MicrostructureConfig,
    OrderBook,
    OrderBookConfig,
    OrderBookSimulator,
    round_to_tick,
)
from .models import (
    Account,
    Exchange,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    ProductType,
    Trade,
)
from .observability import BrokerEvent, EventBus, stdlib_log_subscriber
from .partial_fills import PartialFillConfig
from .price_feed import (
    CachedLastKnownProvider,
    JugaadDataProvider,
    PriceFeed,
    PriceProvider,
    Quote,
    YFinanceProvider,
)
from .providers import (
    OHLCV,
    CircuitBreakerProvider,
    CompositeProvider,
    MarketDataProvider,
    MarketQuote,
    MedianAggregation,
    NSEBhavcopyProvider,
    ProviderCapability,
    ProviderError,
    ProviderHealth,
    ProviderInfo,
    ProviderRegistry,
    StooqProvider,
    default_registry,
)
from .quickstart import quickstart
from .risk import RiskConfig, RiskContext, RiskEngine
from .settlement import (
    PendingSettlement,
    SettlementConfig,
    SettlementEngine,
    SettlementMode,
)
from .simulation import (
    LatencyConfig,
    LatencySimulator,
    RejectionConfig,
    RejectionSimulator,
    RejectScenario,
)
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
    "ProductType",
    "SessionPhase",
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
    # New provider system
    "MarketDataProvider",
    "MarketQuote",
    "OHLCV",
    "ProviderCapability",
    "ProviderError",
    "ProviderInfo",
    "ProviderHealth",
    "CircuitBreakerProvider",
    "CompositeProvider",
    "MedianAggregation",
    "ProviderRegistry",
    "default_registry",
    "StooqProvider",
    "NSEBhavcopyProvider",
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
    # Tier-3: partial fills / events / observability
    "PartialFillConfig",
    "Event",
    "BrokerEvent",
    "EventBus",
    "stdlib_log_subscriber",
    # Tier-A: clocks / quickstart
    "Clock",
    "WallClock",
    "ReplayClock",
    "quickstart",
    # Tier-4: realism extensions
    "MicrostructureConfig",
    "OrderBookConfig",
    "OrderBookSimulator",
    "OrderBook",
    "round_to_tick",
    "SettlementConfig",
    "SettlementEngine",
    "SettlementMode",
    "PendingSettlement",
    "LatencyConfig",
    "LatencySimulator",
    "RejectionConfig",
    "RejectionSimulator",
    "RejectScenario",
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
    "TickSizeViolation",
    "LotSizeViolation",
    "PriceBandViolation",
    "SettlementError",
    "RandomBrokerRejection",
]

__version__ = "0.1.0.dev0"
