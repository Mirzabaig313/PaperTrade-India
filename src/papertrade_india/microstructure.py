"""Deprecated re-export shim — module split during the architecture refactor.

The original ``microstructure`` module mixed two responsibilities:

- pure validation rules (tick / lot / band) → :mod:`papertrade_india.domain.rules.tick_lot_band`
- the synthetic order-book simulator → :mod:`papertrade_india.execution.book`

This shim keeps the legacy import path working but emits a
:class:`DeprecationWarning`. Update imports to the new locations; this
shim will be removed in v0.3.
"""

from __future__ import annotations

import warnings as _warnings

_warnings.warn(
    "papertrade_india.microstructure is deprecated; "
    "import validation rules from papertrade_india.domain.rules.tick_lot_band "
    "and the order-book simulator from papertrade_india.execution.book.",
    DeprecationWarning,
    stacklevel=2,
)

from papertrade_india.domain.rules.tick_lot_band import (  # noqa: E402, F401
    MicrostructureConfig,
    is_aligned_to_tick,
    round_to_tick,
    validate_band,
    validate_lot,
    validate_tick,
)
from papertrade_india.execution.book import (  # noqa: E402, F401
    BookLevel,
    FillSlice,
    MarketFill,
    OrderBook,
    OrderBookConfig,
    OrderBookSimulator,
)

del _warnings

__all__ = [
    "MicrostructureConfig",
    "round_to_tick",
    "is_aligned_to_tick",
    "validate_tick",
    "validate_lot",
    "validate_band",
    "OrderBookConfig",
    "BookLevel",
    "OrderBook",
    "FillSlice",
    "MarketFill",
    "OrderBookSimulator",
]
