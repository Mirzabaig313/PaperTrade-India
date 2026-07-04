# papertrade-india — Quickstart & Usage Guide

Hands-on examples for every feature. For the project overview, feature
list, and fee model, see the [README](README.md).

Install:

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
# Autonomous mode: refuse to fill on cached stale prices.
# An outage that exhausts the live providers raises StalePriceRejected
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

### Realism extensions

Every realism feature is **on by default** so a fresh broker behaves
like a real Indian retail account: T+1 cash settlement, mark-to-bid
P&L, tick/lot/band rules, latency, occasional rejections, synthetic
order book. Override any of them by passing the matching config
object — set `enabled=False` or pass the legacy mode to opt out:

```python
from datetime import time
from papertrade_india import (
    IndiaPaperBroker, MicrostructureConfig, OrderBookConfig,
    SettlementConfig, SettlementMode, LatencyConfig, RejectionConfig,
    RejectScenario,
)

# Default broker — all realism on:
broker = IndiaPaperBroker(initial_capital=1_000_000)

# Or override individual layers:
broker = IndiaPaperBroker(
    # tick/lot/band — all enforced by default. Disable any flag to skip.
    microstructure_config=MicrostructureConfig(
        enforce_tick_size=True,
        enforce_lot_size=True,
        enforce_price_band=True,
        default_tick_size=0.05,    # NSE cash equity
        default_band_pct=0.20,     # ±20% if symbol master has no override
    ),
    # T+1 settlement is the default; switch to T+0 for legacy backtests
    settlement_config=SettlementConfig(
        mode=SettlementMode.T_PLUS_1,    # T+1 by default
        auto_square_off_at=time(15, 15),
    ),
    # synthetic L2 book + queue position + Almgren impact (on by default)
    order_book_config=OrderBookConfig(
        enabled=True,
        levels=10,
        depth_pct_of_adv=0.005,    # 0.5% of ADV at the touch
        almgren_coeff_bps=50.0,    # 50 bps for 100% of ADV
    ),
    # latency + random rejections (on by default with sane parameters)
    latency_config=LatencyConfig(submit_ms_mean=80, submit_ms_p99=400),
    rejection_config=RejectionConfig(
        rate=0.001,
        scenarios=[RejectScenario.NETWORK, RejectScenario.FREEZE_QTY],
    ),
    # mark unrealized P&L off the bid (real-broker convention) — on by default
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
for a working walkthrough of all the realism features.

### Data-provider system

A formal `MarketDataProvider` ABC delivers a rich `MarketQuote`
(last/bid/ask/OHLC/volume/source/freshness), not just a float. You can
stack circuit breakers and median aggregation to get fills closer to
"real":

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

There's also a `resilient_feed()` helper that wires a first-wins
fallback chain with per-provider circuit breakers in one line:

```python
from papertrade_india import resilient_feed
from papertrade_india.price_feed import JugaadDataProvider, YFinanceProvider
from papertrade_india.providers import UpstoxInstrumentMaster, UpstoxProvider

feed = resilient_feed([
    UpstoxProvider(resolve=UpstoxInstrumentMaster().resolve),  # live, needs a token
    YFinanceProvider("NS"),
    JugaadDataProvider(),
])
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
| `UpstoxProvider` | Upstox (broker feed) | ✅ `UPSTOX_ACCESS_TOKEN` | ✅ | NSE + BSE, real bid/ask + 5-level depth |
| `DhanProvider` | Dhan (broker feed) | ✅ `DHAN_CLIENT_ID` / `DHAN_ACCESS_TOKEN` | ✅ | NSE + BSE, needs a security-id map |

Discover what's installed on your machine via the registry:

```python
from papertrade_india import default_registry

for name, info in default_registry.available().items():
    print(name, "—", info.description)
```

Every provider is wrappable in `CircuitBreakerProvider` and aggregatable
via `CompositeProvider` (default `first_wins`, or `MedianAggregation`).
Old code that uses the legacy `PriceProvider` Protocol shape (objects
with just `get_price`) continues to work — the chain mixes both styles.
