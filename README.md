<div align="center">

# PaperTrade-India

**A production-grade simulated broker for NSE & BSE paper trading.**

Realistic Indian-market order execution — fees, T+1 settlement, tick/lot/band
rules, and live prices — behind a clean, Alpaca-style Python API.

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Status](https://img.shields.io/badge/status-alpha-orange)
![Tests](https://img.shields.io/badge/tests-passing-brightgreen)

[Quickstart](QUICKSTART.md) · [Examples](examples/) · [Report a bug](../../issues)

</div>

---

## Overview

No Indian broker offers a programmatic paper-trading API. **PaperTrade-India**
fills that gap: a simulated NSE/BSE broker that models the parts of real
trading that actually move your P&L — statutory fees, T+1 settlement,
price bands, slippage, and market impact — while exposing the same
method surface as an Alpaca-style trading client, so you can build and
test strategies without touching real capital or rewriting your code.

```python
from papertrade_india import IndiaPaperBroker

broker = IndiaPaperBroker(initial_capital=1_000_000)
order = broker.buy("RELIANCE", 10)
print(order.filled_avg_price, order.fees_paid)
```

> **Status:** alpha (pre-1.0). The public API is stable enough to build on,
> but minor releases may introduce breaking changes until 1.0.

## Why

| Platform | Paper trading | API access |
|:---|:---:|:---:|
| Zerodha Kite | ❌ | ❌ |
| Upstox | ⚠️ mock only | ⚠️ mock only |
| Angel One SmartAPI | ❌ | ❌ |
| Dhan / Fyers / Shoonya | ❌ | ❌ |
| **papertrade-india** | ✅ | ✅ |

## Features

**Execution realism** — all on by default, so a fresh broker behaves like a real retail account:

- **Indian fee model** — brokerage, STT, exchange charges, GST, SEBI
  charges, stamp duty, and DP charges, configurable per broker and
  date-versioned for mid-year statutory changes.
- **T+1 settlement** with deliverable-quantity enforcement, plus same-day
  intraday round-trips via `ProductType.INTRADAY` and 15:15 auto-square-off.
- **Tick / lot / price-band rules** — orders snap to the symbol's tick and
  are rejected outside the daily band.
- **Order types** — market, limit, `STOP_MARKET`, `STOP_LIMIT`, and
  `BRACKET` with full OCO semantics.
- **Synthetic L2 order book** — uses real provider depth when available,
  with queue-position tracking and Almgren-style market impact.
- **Slippage**, **latency**, and **random-rejection** simulation for
  stress-testing strategy robustness.
- **Mark-to-bid** valuation for realistic unrealized P&L.

**Data** — a pluggable `MarketDataProvider` layer:

- Built-in providers: `yfinance`, `jugaad-data`, `stooq`, `nse-bhavcopy`
  (official EOD), `nsepython`, `alphavantage`, `twelvedata`, `finnhub`,
  plus live broker feeds `upstox` and `dhan` (real bid/ask + market depth).
- Per-provider **circuit breakers**, **median aggregation** across sources,
  a first-wins `resilient_feed()` helper, and a name registry.
- **Live NSE/BSE holiday calendar** from the exchange-published API, cached
  with an offline fallback.

**Engineering & safety**

- Thread-safe SQLite persistence (WAL, atomic transactions, versioned
  migrations).
- Session-phase awareness (PRE_OPEN / REGULAR / POST_CLOSE / CLOSED).
- Risk controls: kill switch, symbol whitelist, per-order and per-position
  notional caps.
- Idempotency keys, an immutable cash ledger with an invariant check,
  broker fee presets (Zerodha, Upstox, Groww, Angel One, ICICIdirect),
  a symbol master with delisting, corporate actions (splits + dividends),
  and a persisted event log with an in-process observability bus.
- Multi-account support, an optional CLI, and an optional MCP server so
  LLM agents can trade through tool calls.

## Installation

```bash
pip install papertrade-india               # core
pip install 'papertrade-india[jugaad]'     # + NSE-direct fallback data
pip install 'papertrade-india[cli]'        # + Typer/Rich CLI
pip install 'papertrade-india[mcp]'        # + MCP server
pip install 'papertrade-india[dev]'        # + tests and linting
```

Requires Python 3.10+.

## Quickstart

```python
from papertrade_india import IndiaPaperBroker

broker = IndiaPaperBroker(initial_capital=1_000_000)

# Market buy (inside NSE trading hours)
order = broker.buy("RELIANCE", 10)
print(order.filled_avg_price, order.fees_paid)

# Inspect state
for position in broker.get_positions():
    print(position.symbol, position.qty, position.unrealized_pl)

account = broker.get_account()
print(f"Equity: ₹{account.equity:,.2f}  Cash: ₹{account.cash:,.2f}")

# Realize P&L
broker.sell("RELIANCE", 10)
```

The full getting-started walkthrough and a worked example for **every**
feature — limit orders, multiple accounts, risk controls, live data feeds,
realism configuration, and more — live in **[QUICKSTART.md](QUICKSTART.md)**.
Runnable scripts are in [`examples/`](examples/).

## Configuration

Most providers and live broker feeds need an API key or token. Everything
is optional — with no keys set, the broker uses the free
`yfinance → jugaad-data` fallback chain and works out of the box. There
are three ways to supply credentials:

**1. Pass them directly to the provider** (most explicit — no env needed):

```python
from papertrade_india import IndiaPaperBroker, resilient_feed
from papertrade_india.providers import UpstoxProvider, UpstoxInstrumentMaster

feed = resilient_feed([
    UpstoxProvider(access_token="your-token", resolve=UpstoxInstrumentMaster().resolve),
])
broker = IndiaPaperBroker(price_feed=feed)
```

Every provider accepts its key this way: `FinnhubProvider(api_key=...)`,
`DhanProvider(client_id=..., access_token=...)`,
`KiteProvider(api_key=..., access_token=...)`, and so on.

**2. Set an environment variable** — every provider falls back to it:

```bash
export UPSTOX_ACCESS_TOKEN="your-token"
python your_script.py
```

The variable names are listed in [`.env.example`](.env.example) (e.g.
`UPSTOX_ACCESS_TOKEN`, `FINNHUB_API_KEY`, `ALPHA_VANTAGE_API_KEY`,
`DHAN_CLIENT_ID` / `DHAN_ACCESS_TOKEN`).

**3. Use a `.env` file** — the library doesn't read `.env` automatically,
so load it yourself with [python-dotenv](https://pypi.org/project/python-dotenv/):

```python
from dotenv import load_dotenv
load_dotenv()   # copy .env.example to .env and fill in your keys
```

## Fee model

Defaults match a typical discount-broker delivery account:

| Component | Default rate | Applies to |
|:---|:---|:---|
| Brokerage | ₹0 | Both sides (delivery) |
| STT | 0.1% | Both sides |
| Exchange charge | 0.00322% (NSE) / 0.00375% (BSE) | Both sides |
| GST | 18% on (brokerage + exchange) | Both sides |
| SEBI charges | ₹10 per crore | Both sides |
| Stamp duty | 0.015% | Buy only |
| DP charge | ₹13.5 | Sell only |

Override any field via `FeeConfig`, or use a named preset, to model
intraday or full-service brokers.

## Scope & limitations

The simulator is intentionally focused on **cash equity, delivery,
long-only** trading. It is a faithful behavioral simulator, not an exchange
replica:

| Limitation | Notes |
|:---|:---|
| Synthetic order book, not a real matching engine | Uses real provider depth when available; accurate for retail order sizes. True queue priority isn't reproducible from retail data. |
| Snapshot prices, not a tick stream | Fills price off the latest snapshot — negligible at daily/swing cadence. |
| Corporate actions applied manually | Via `apply_split` / `apply_dividend`. |
| No margin, leverage, or short selling | Cash, long-only by design. |
| No options or F&O | Equity only. |

## Contributing

Issues and pull requests are welcome. Install the dev extras and run the
test suite before submitting:

```bash
pip install -e '.[dev]'
pytest
ruff check src tests
```

## License

[MIT](LICENSE) — use it however you want.

## Disclaimer

This is a **simulated** broker; it does not place real trades. It is not
investment advice. Always verify calculations against your actual broker's
contract notes before relying on the simulator's outputs for tax,
compliance, or investment decisions.
