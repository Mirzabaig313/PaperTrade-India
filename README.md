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
- Partial fills on limit orders
- Order-book / auction / pre-open behavior
- Corporate actions (splits, dividends, bonuses) — manual `reset()` for now
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
- Realistic Indian fees: brokerage, **STT**, exchange charges, **GST**,
  SEBI charges, **stamp duty**, **DP charges**. Configurable per-broker.
- Thread-safe SQLite persistence with WAL mode and atomic transactions.
- NSE holiday calendar and trading-hours enforcement.
- Limit-order support with a background watcher loop.
- Multi-account support — run multiple agents against one DB.
- **Slippage model** — configurable basis-point impact on market fills.
- **Risk controls** — kill switch, symbol whitelist, per-order and
  per-position notional caps, equity-fraction caps.
- **Idempotency keys** — replay-safe order submission.
- **Broker presets** — named `FeeConfig` for Zerodha, Upstox, Groww,
  Angel One, ICICIdirect (delivery + intraday variants).
- **Symbol master** — track tradeable symbols, reject delisted symbols.
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
