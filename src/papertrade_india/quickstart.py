"""One-line factory for the most common configuration.

Most first-time users just want a "sane defaults" broker so they can
play with buy/sell. Hand-picking among 14 constructor parameters is a
cliff. ``quickstart()`` returns a broker pre-configured for safe
delivery-style equity trading on NSE:

- ₹1,000,000 starting capital
- Zerodha-style fee schedule (₹0 brokerage delivery)
- 5 bp slippage on market fills
- Strict symbol master with the bundled NSE-30 sample loaded
- Idempotency keys cleaned up automatically every 50 watcher ticks
  (see ``LimitOrderWatcher`` opt-in below)
- Stale-price hard-reject ON for autonomous safety

Override any field by passing it explicitly. The returned broker is
identical to one constructed by hand — there's no "quickstart mode"
flag anywhere; it's just an opinionated factory.

Example::

    from papertrade_india import quickstart
    broker = quickstart()
    broker.buy("RELIANCE", 1)

Pass overrides to specialize::

    broker = quickstart(
        initial_capital=500_000,
        symbol_master=None,            # disable the strict symbol master
        slippage_bps=10.0,
    )
"""

from __future__ import annotations

from pathlib import Path

from .broker import IndiaPaperBroker
from .fees import FeeConfig, FeeSchedule
from .models import Exchange
from .persistence import PathLike
from .presets import ZERODHA_DELIVERY
from .risk import RiskConfig
from .slippage import SlippageConfig
from .symbols import SymbolMaster


def quickstart(
    *,
    initial_capital: float = 1_000_000.0,
    db_path: PathLike = "data/india_paper.db",
    account_id: str = "default",
    fee_config: FeeConfig | FeeSchedule | None = None,
    slippage_bps: float = 5.0,
    risk_config: RiskConfig | None = None,
    symbol_master: SymbolMaster | None = "load-bundled-nse",
    enforce_market_hours: bool = True,
    enforce_fresh_prices: bool = True,
) -> IndiaPaperBroker:
    """Construct an opinionated, safe-by-default broker.

    Parameters that differ from ``IndiaPaperBroker.__init__`` defaults:

    - ``slippage_bps`` (vs 0): a realistic 5 bps for liquid NSE names.
    - ``enforce_fresh_prices`` (vs False): hard-reject stale fills.
    - ``symbol_master``: defaults to a strict master pre-loaded with
      the bundled NSE-30 sample. Pass ``None`` to disable, or your own
      ``SymbolMaster`` to override.
    - ``fee_config``: defaults to ``ZERODHA_DELIVERY`` (₹0 brokerage,
      full statutory schedule).

    Other parameters fall through to ``IndiaPaperBroker``.
    """
    if symbol_master == "load-bundled-nse":
        sm = SymbolMaster(strict=True)
    elif symbol_master is None:
        sm = SymbolMaster(strict=False)
    else:
        sm = symbol_master

    broker = IndiaPaperBroker(
        initial_capital=initial_capital,
        db_path=db_path,
        account_id=account_id,
        exchange=Exchange.NSE,
        fee_config=fee_config or ZERODHA_DELIVERY,
        slippage_config=SlippageConfig(bps=slippage_bps),
        risk_config=risk_config or RiskConfig(),
        symbol_master=sm,
        enforce_market_hours=enforce_market_hours,
        enforce_fresh_prices=enforce_fresh_prices,
    )

    # Load the NSE-30 sample into the master if we constructed one.
    if symbol_master == "load-bundled-nse":
        sample = Path(__file__).parent / "data" / "nse_universe_sample.csv"
        if sample.exists():
            with broker.persistence.transaction() as conn:
                broker.symbol_master.load_csv(conn, sample, Exchange.NSE)

    return broker
