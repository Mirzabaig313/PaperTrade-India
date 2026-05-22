# papertrade-india

Production-grade simulated broker for **NSE** and **BSE** paper trading.

There's a real gap to fill: no Indian broker offers a programmatic paper
trading API. This package solves it. Drop-in replacement for an Alpaca-style
trading service so you can swap markets without changing your agent code.

> **Status:** alpha — pre-1.0. The public API is stable enough to use, but
> minor releases may include breaking changes until 1.0.

## Goals

- Realistic NSE/BSE paper trading for **cash equity, delivery, long-only**.
- Drop-in replacement for an Alpaca-style trading service.
- Configurable enough to model your specific broker's fee schedule.
- Auditable: realized P&L matches a contract note within ~₹0.05 across
  the supported scenarios.
- Safe enough to plug behind an autonomous AI agent (idempotency, kill
  switch, position caps, symbol whitelist, rejection of delisted symbols).

## Non-goals

The simulator deliberately does **not** model:

- Margin, leverage, or short selling
- Options or F&O
- Real order-book / bid-ask depth (partial fills are configurable, not real)
- Pre-open auction matching algorithm (the phase is recognized, fills are not simulated)
- Mergers, spin-offs, rights issues (only splits + cash dividends in v0.2)
- Real-time tick data — yfinance is 15-minute delayed

These are documented limitations, not bugs. Add them only when a real use
case forces it.

## Why

| Platform | Paper trading? | API access? |
|:---|:---:|:---:|
| Zerodha Kite | ❌ | ❌ |
| Upstox | ⚠️ mock only | ⚠️ mock only |
| Angel One SmartAPI | ❌ | ❌ |
| Dhan / Fyers / Shoonya | ❌ | ❌ |
| **papertrade-india** | ✅ | ✅ |

See [docs/India_Paper_Trading_Design.md](../docs/India_Paper_Trading_Design.md)
for the full landscape review.

## Features

- Real NSE/BSE prices via `yfinance`, with `jugaad-data` fallback and a
  cached last-known fallback. Three layers of degradation.
- **Pluggable data-provider system** — formal `MarketDataProvider` ABC
  with bid/ask/OHLCV/volume support, capability flags, per-provider
  circuit breakers, median aggregation across multiple sources, and a
  name registry. Built-in providers: `yfinance`, `jugaad-data`, `stooq`,
  `nse-bhavcopy` (official NSE EOD), `nsepython`, `alphavantage`,
  `twelvedata`, `finnhub`. See [examples/08_data_providers.py](examples/08_data_providers.py).
- **Realism extensions (v0.2)** — opt-in features that close the gap
  between "paper trading" and "broker simulator":
  - **Tick / lot / band rules** — limit prices snap to the symbol's
    tick (₹0.05 for most NSE scrips); orders rejected if not aligned
    or if outside the daily price band.
  - **STOP_MARKET / STOP_LIMIT / BRACKET** orders with full OCO
    semantics (target fill cancels stop and vice versa).
  - **T+1 settlement** with deliverable-qty enforcement (you can't
    sell what you bought today on a delivery account) and same-day
    intraday round-trips through `ProductType.INTRADAY`.
  - **Auto-square-off** of intraday positions at 15:15 IST.
  - **Mark-to-bid** valuation — unrealized P&L computed at the
    actual exit price, not last.
  - **Synthetic L2 order book** with queue position tracking and
    Almgren-style market impact (`OrderBookConfig.enabled=True`).
  - **Latency + random rejection** simulation for testing how your
    agent handles a flaky upstream.
  - See [examples/09_realism_features.py](examples/09_realism_features.py)
    for a tour.
- Realistic Indian fees: brokerage, **STT**, exchange charges, **GST**,
  SEBI charges, **stamp duty**, **DP charges**. Configurable per-broker.
- Thread-safe SQLite persistence with WAL mode and atomic transactions.
- NSE holiday calendar and session-phase awareness (PRE_OPEN /
  REGULAR / POST_CLOSE / CLOSED).
- Limit-order support with a background watcher loop and configurable
  partial fills.
- Multi-account support — run multiple agents against one DB.
- **Slippage model** — configurable basis-point impact, default + per-symbol overrides.
- **Risk controls** — kill switch, symbol whitelist, per-order and
  per-position notional caps, equity-fraction caps.
- **Idempotency keys** — replay-safe order submission.
- **Broker presets** — named `FeeConfig` for Zerodha, Upstox, Groww,
  Angel One, ICICIdirect (delivery + intraday variants).
- **Symbol master** — track tradeable symbols, reject delisted symbols.
- **Immutable cash ledger** — every cash mutation is an append-only
  row; ``verify_cash_invariant()`` asserts cash == sum(movements).
- **Corporate actions** — `apply_split` and `apply_dividend` for stock
  splits, bonus issues, and cash dividends.
- **Date-versioned fee schedules** — pick the right `FeeConfig` based
  on trade date when statutory rates change mid-year.
- **Stale-price hard-reject** — autonomous-mode flag to refuse fills
  when the price came from the long-lived cache rather than a live feed.
- **Event log + observability bus** — every domain event persisted to
  SQLite AND fanned out to in-process subscribers (wire to OTel/Prom/etc).
- **Configurable partial fills** — per-tick fill cap on limit orders,
  for stress-testing strategies that assume infinite liquidity.
- Optional CLI for inspecting state.
- Optional MCP server example so any LLM agent (Claude Desktop, Cursor,
  custom agents) can use the broker as a tool.

## Install

```bash
pip install papertrade-india               # core
pip install 'papertrade-india[jugaad]'     # + NSE-direct fallback
pip install 'papertrade-india[cli]'        # + Typer/Rich CLI
pip install 'papertrade-india[mcp]'        # + MCP server deps
pip install 'papertrade-india[dev]'        # tests + lint
```

Requires Python 3.10+.

## Quickstart

```python
from papertrade_india import IndiaPaperBroker

broker = IndiaPaperBroker(initial_capital=1_000_000)

# Buy at market price (must be inside NSE trading hours)
order = broker.buy("RELIANCE", 10)
print(order.filled_avg_price, order.fees_paid)

# Inspect state
for p in broker.get_positions():
    print(p.symbol, p.qty, p.unrealized_pl)

acct = broker.get_account()
print(f"Equity: ₹{acct.equity:,.2f}, cash: ₹{acct.cash:,.2f}")

# Sell to realize P&L
broker.sell("RELIANCE", 10)
```

### Limit orders

```python
from papertrade_india import IndiaPaperBroker, LimitOrderWatcher, OrderType

broker = IndiaPaperBroker()
watcher = LimitOrderWatcher(broker, interval_seconds=5)
watcher.start()  # background thread

broker.buy("HDFCBANK", 1, order_type=OrderType.LIMIT, limit_price=1500)
# ...later...
watcher.stop()
watcher.join()
```

### Multiple accounts

```python
trader = IndiaPaperBroker(account_id="trader_a", db_path="agents.db")
follower = IndiaPaperBroker(account_id="trader_b", db_path="agents.db")

trader.buy("INFY", 50)
follower.buy("INFY", 25)  # independent
```

### CLI

```bash
papertrade-india account --account default
papertrade-india positions
papertrade-india orders --status pending
papertrade-india cancel-all
papertrade-india reset --capital 1000000
```

### As an MCP server

See [`examples/05_mcp_server.py`](examples/05_mcp_server.py) for a complete
example. Drop the broker behind a FastMCP server so any LLM agent can buy,
sell, and inspect positions through tool calls.

## Configuration

```python
from papertrade_india import IndiaPaperBroker, FeeConfig, Exchange

broker = IndiaPaperBroker(
    initial_capital=500_000,
    db_path="my_agent.db",
    account_id="alpha",
    exchange=Exchange.NSE,
    fee_config=FeeConfig(
        # Override the defaults to match your specific broker.
        brokerage_flat=20.0,    # ₹20 flat per intraday order
        stt_pct_buy=0.001,
        stt_pct_sell=0.001,
    ),
    enforce_market_hours=True,  # Reject MARKET orders outside NSE hours
)
```

### Slippage

```python
from papertrade_india import IndiaPaperBroker, SlippageConfig

broker = IndiaPaperBroker(
    slippage_config=SlippageConfig(bps=5),  # 0.05% slippage on market fills
)
# BUY pays 5 bps above last; SELL receives 5 bps below.
# Default is 0 bps (legacy behavior). Tune to match your strategy's fills.
```

### Risk controls

```python
from papertrade_india import IndiaPaperBroker, RiskConfig

broker = IndiaPaperBroker(
    risk_config=RiskConfig(
        kill_switch=False,                       # Or PAPERTRADE_INDIA_KILL_SWITCH=1
        symbol_whitelist=frozenset({"RELIANCE", "INFY"}),
        max_order_notional=100_000.0,            # ₹1L per order
        max_position_notional=500_000.0,         # ₹5L per position
        max_position_pct_of_equity=0.20,         # 20% of equity per position
    ),
)
```

### Idempotency

```python
# Re-submitting the same key with the same params returns the original order
# (no duplicate fill). Different params under the same key raise
# IdempotencyConflict.
order1 = broker.buy("RELIANCE", 1, idempotency_key="trade-2026-05-18-001")
order2 = broker.buy("RELIANCE", 1, idempotency_key="trade-2026-05-18-001")
assert order1.id == order2.id  # same order, no double-buy
```

### Broker presets

```python
from papertrade_india import IndiaPaperBroker
from papertrade_india.presets import ZERODHA_INTRADAY, UPSTOX_DELIVERY

broker = IndiaPaperBroker(fee_config=ZERODHA_INTRADAY)
# Available: zerodha-delivery, zerodha-intraday, upstox-delivery,
# upstox-intraday, groww-delivery, angel-one-delivery,
# angel-one-intraday, icicidirect-delivery
```

### Symbol master

```python
from pathlib import Path
from papertrade_india import IndiaPaperBroker, SymbolMaster, Exchange

broker = IndiaPaperBroker(
    symbol_master=SymbolMaster(strict=True),  # require registration
)

# Load the bundled NSE-30 sample, or your own CSV
sample = Path(__file__).parent / "src/papertrade_india/data/nse_universe_sample.csv"
with broker.persistence.transaction() as conn:
    broker.symbol_master.load_csv(conn, sample, Exchange.NSE)

# Mark a symbol delisted (rejected even in lenient mode)
with broker.persistence.transaction() as conn:
    broker.symbol_master.delist(conn, "OLDCO", Exchange.NSE)
```

### Cash ledger

Every cash mutation is recorded as an append-only row. Verify the
invariant `account.cash == sum(cash_movements)` any time:

```python
broker.buy("RELIANCE", 5)
broker.sell("RELIANCE", 5)

assert broker.verify_cash_invariant()  # always True

for m in broker.get_cash_movements(limit=10):
    print(m.recorded_at, m.reason, m.amount, m.order_id)
```

### Corporate actions

```python
# 2:1 split (qty doubles, avg_cost halves; total basis preserved)
broker.apply_split("RELIANCE", ratio_num=2, ratio_den=1)

# 1:5 reverse split
broker.apply_split("PENNYSTOCK", ratio_num=1, ratio_den=5)

# Cash dividend ₹12.50/share
broker.apply_dividend("ITC", amount_per_share=12.50, notes="Q4 FY26")
```

### Date-versioned fee schedule

```python
from datetime import date
from papertrade_india import FeeConfig, FeeSchedule, IndiaPaperBroker

schedule = FeeSchedule(
    default=FeeConfig(stt_pct_buy=0.0010),
    effective_from={
        date(2026, 4, 1): FeeConfig(stt_pct_buy=0.00125),  # hypothetical hike
    },
)
broker = IndiaPaperBroker(fee_config=schedule)
# Orders before 2026-04-01 use 0.10% STT; orders on/after use 0.125%.
```

### Stale-price hard-reject

```python
# Autonomous-agent mode: refuse to fill on cached stale prices.
# An outage that exhausts yfinance + jugaad raises StalePriceRejected
# instead of executing on potentially-hours-old data.
broker = IndiaPaperBroker(enforce_fresh_prices=True)
```

### Per-symbol slippage

```python
from papertrade_india import IndiaPaperBroker, SlippageConfig

broker = IndiaPaperBroker(
    slippage_config=SlippageConfig(
        bps=5,                                  # default for liquids
        per_symbol_bps={"PENNYSTOCK": 50.0},    # 0.50% on illiquid
    ),
)
```

### Configurable partial fills

```python
from papertrade_india import IndiaPaperBroker, PartialFillConfig

broker = IndiaPaperBroker(
    partial_fill_config=PartialFillConfig(
        enabled=True,
        max_per_tick=100,        # absolute cap per watcher tick
        max_pct_per_tick=0.25,   # or 25% of remaining qty
        min_fill_qty=1,          # don't fill slivers smaller than this
    ),
)
# A 1000-share limit order now fills across ~10 ticks instead of one.
# This isn't real bid/ask depth — it's a configurable knob for stress-
# testing strategies that assume infinite liquidity.
```

### Session phases

```python
from papertrade_india import SessionPhase

# Four phases per trading day:
#   PRE_OPEN     09:00–09:08
#   REGULAR      09:15–15:30  (the only phase where market orders fill)
#   POST_CLOSE   15:40–16:00
#   CLOSED       everything else, weekends, holidays
phase = broker.current_session_phase()
if phase == SessionPhase.REGULAR:
    broker.buy("RELIANCE", 1)
```

### Event log + observability

```python
# Persisted event log — recoverable across restarts, queryable in SQL.
events = broker.get_events(event_types=("order_filled",))
for e in events:
    print(e.recorded_at, e.event_type, e.payload)

# In-process callback bus — wire to OpenTelemetry, Prometheus, logs, ...
def to_metrics(event):
    if event.event_type == "order_filled":
        my_counter.inc()

broker.events.subscribe(to_metrics, name="prom-shipper")
```

### Realism extensions (new in v0.2)

By default, the broker behaves exactly like v0.1.x: instant fills, no
tick/lot enforcement, T+0 cash, mark-off-last. Each realism feature is
an opt-in config object:

```python
from datetime import time
from papertrade_india import (
    IndiaPaperBroker, MicrostructureConfig, OrderBookConfig,
    SettlementConfig, SettlementMode, LatencyConfig, RejectionConfig,
    RejectScenario,
)

broker = IndiaPaperBroker(
    # 1–3: tick/lot/band — fully on by default. Disable any flag to skip.
    microstructure_config=MicrostructureConfig(
        enforce_tick_size=True,
        enforce_lot_size=True,
        enforce_price_band=True,
        default_tick_size=0.05,    # NSE cash equity
        default_band_pct=0.20,     # ±20% if symbol master has no override
    ),
    # 4: T+1 settlement + intraday auto-square-off
    settlement_config=SettlementConfig(
        mode=SettlementMode.T_PLUS_1,    # T+0 by default
        auto_square_off_at=time(15, 15),
    ),
    # 5: synthetic L2 book + queue position + Almgren impact
    order_book_config=OrderBookConfig(
        enabled=True,
        levels=10,
        depth_pct_of_adv=0.005,    # 0.5% of ADV at the touch
        almgren_coeff_bps=50.0,    # 50 bps for 100% of ADV
    ),
    # 6: latency + random rejections
    latency_config=LatencyConfig(submit_ms_mean=80, submit_ms_p99=400),
    rejection_config=RejectionConfig(
        rate=0.001,
        scenarios=[RejectScenario.NETWORK, RejectScenario.FREEZE_QTY],
    ),
    # 7: mark unrealized P&L off the bid (real-broker convention)
    mark_to_bid=True,
)
```

New order types:

```python
from papertrade_india import OrderType, ProductType

# Stop-loss after entering a position
broker.buy("RELIANCE", 10)
broker.sell(
    "RELIANCE", 10,
    order_type=OrderType.STOP_MARKET,
    stop_price=2400.00,
)

# Stop-Limit (avoids the "stop slipped through" failure mode)
broker.sell(
    "RELIANCE", 10,
    order_type=OrderType.STOP_LIMIT,
    stop_price=2400.00,
    limit_price=2390.00,
)

# Bracket: entry + SL + target as one logical unit (OCO semantics)
broker.buy(
    "RELIANCE", 10,
    order_type=OrderType.BRACKET,
    stop_price=2400.00,
    target_price=2600.00,
)

# Intraday product type — bypasses T+1, auto-squared at 15:15 IST
broker.buy("INFY", 5, product_type=ProductType.INTRADAY)
broker.sell("INFY", 5, product_type=ProductType.INTRADAY)  # same-day OK
```

Run [`examples/09_realism_features.py`](examples/09_realism_features.py)
for a working walkthrough of all seven features.

### Data-provider system (new in v0.2) — a formal ABC that
delivers a rich `MarketQuote` (last/bid/ask/OHLC/volume/source/freshness),
not just a float. You can stack circuit breakers and median aggregation
to get fills closer to "real":

```python
from papertrade_india import (
    CircuitBreakerProvider, CompositeProvider, IndiaPaperBroker,
    MedianAggregation, NSEBhavcopyProvider, PriceFeed,
    StooqProvider, YFinanceProvider,
)

feed = PriceFeed(
    providers=[
        CompositeProvider(
            [
                CircuitBreakerProvider(YFinanceProvider("NS")),
                CircuitBreakerProvider(StooqProvider()),
                CircuitBreakerProvider(NSEBhavcopyProvider()),
            ],
            aggregation=MedianAggregation(max_disagreement_bps=200),
        ),
    ],
)

broker = IndiaPaperBroker(price_feed=feed)
```

Built-in providers (each implements `MarketDataProvider`):

| Provider | Source | API key | Real-time | Notes |
|:---|:---|:---:|:---:|:---|
| `YFinanceProvider` | Yahoo Finance | ❌ | ❌ (15-min delay) | NSE + BSE |
| `JugaadDataProvider` | NSE direct (scraper) | ❌ | ❌ | NSE only, scraper |
| `NSEPythonProvider` | NSE direct (scraper) | ❌ | ❌ | NSE only, alternate scraper |
| `StooqProvider` | stooq.com CSV | ❌ | ❌ (EOD) | NSE, free |
| `NSEBhavcopyProvider` | NSE official bhavcopy | ❌ | ❌ (EOD) | Authoritative EOD |
| `AlphaVantageProvider` | alphavantage.co | ✅ `ALPHA_VANTAGE_API_KEY` | ❌ | Free tier 5 req/min |
| `TwelveDataProvider` | twelvedata.com | ✅ `TWELVE_DATA_API_KEY` | ❌ | Free tier 800/day |
| `FinnhubProvider` | finnhub.io | ✅ `FINNHUB_API_KEY` | ✅ | Free tier 60/min |

Discover what's installed on your machine via the registry:

```python
from papertrade_india import default_registry

for name, info in default_registry.available().items():
    print(name, "—", info.description)
```

Every provider is wrappable in `CircuitBreakerProvider` (per the
project's resiliency rules) and aggregatable via `CompositeProvider`
with a strategy of your choice (default `first_wins`, or
`MedianAggregation` for harder-to-skew fills). Old code that uses the
legacy `PriceProvider` Protocol shape (objects with just `get_price`)
continues to work — the chain mixes both styles.

## How fees are modelled

Defaults match a typical discount-broker delivery account in 2026:

| Component | Rate (default) | Applies to |
|:---|:---|:---|
| Brokerage | ₹0 | Both sides (delivery) |
| STT | 0.1% | Both sides |
| Exchange charge | 0.00322% (NSE), 0.00375% (BSE) | Both sides |
| GST | 18% on (brokerage + exchange) | Both sides |
| SEBI charges | ₹10 per crore | Both sides |
| Stamp duty | 0.015% | Buy only |
| DP charge | ₹13.5 | Sell only |

Override any field via `FeeConfig` to model intraday or full-service brokers.

## Limitations

| Limitation | Impact | Mitigation |
|:---|:---|:---|
| No real slippage | Fills at last price | Acceptable for daily-cadence agents on liquid mid/large-caps |
| 15-min delayed prices (yfinance) | Entry/exit slightly off | Negligible for daily cadence |
| No corporate actions | Splits/dividends not auto-applied | Manual `reset()` between events |
| No margin / leverage | Cash account only | Intentional |
| No short selling | Long-only | Intentional |
| No partial fills on limit orders | All-or-nothing | Real exchanges allow partial fills; not modelled here |
| No options or F&O | Equity only | Out of scope |

See [docs/India_Paper_Trading_Design.md](../docs/India_Paper_Trading_Design.md)
§7 for the full list and roadmap.

## License

[MIT](LICENSE) — use it however you want.

## Disclaimer

This is a **simulated** broker. It does not place real trades. Always verify
calculations against your actual broker's contract notes before relying on
the simulator's outputs for tax, compliance, or investment decisions.
