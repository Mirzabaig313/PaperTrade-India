# papertrade-india

Production-grade simulated broker for **NSE** and **BSE** paper trading.

There's a real gap to fill: no Indian broker offers a programmatic paper
trading API. This package solves it — an Alpaca-style trading interface so
you can swap markets without changing your code.

> **Status:** alpha — pre-1.0. The public API is stable enough to use, but
> minor releases may include breaking changes until 1.0.

## Goals

- Realistic NSE/BSE paper trading for **cash equity, delivery, long-only**.
- An Alpaca-style trading interface.
- Configurable enough to model your specific broker's fee schedule.
- Auditable: realized P&L matches a contract note within ~₹0.05 across
  the supported scenarios.
- Safe enough to plug behind an autonomous trading system (idempotency,
  kill switch, position caps, symbol whitelist, rejection of delisted
  symbols).

## Non-goals

The simulator deliberately does **not** model:

- Margin, leverage, or short selling
- Options or F&O
- A real central-limit-order-book matching engine (fills use a synthetic
  book built from top-of-book quotes; true queue priority isn't reproducible
  from retail data)
- Mergers, spin-offs, rights issues (only splits + cash dividends)
- A real-time tick stream (prices are on-demand snapshots)

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

## Features

- Real NSE/BSE prices via a **pluggable data-provider system** — a formal
  `MarketDataProvider` ABC with bid/ask/OHLCV/volume support, capability
  flags, per-provider circuit breakers, median aggregation across sources,
  and a name registry. Built-in providers: `yfinance`, `jugaad-data`,
  `stooq`, `nse-bhavcopy` (official NSE EOD), `nsepython`, `alphavantage`,
  `twelvedata`, `finnhub`, plus live broker feeds `upstox` and `dhan`
  (real bid/ask + market depth). See
  [examples/08_data_providers.py](examples/08_data_providers.py).
- **Live NSE/BSE holiday calendar** fetched from the exchange-published API
  (cached, with an offline fallback).
- **Realism extensions** — opt-in features that close the gap between
  "paper trading" and "broker simulator":
  - **Tick / lot / band rules** — limit prices snap to the symbol's tick
    (₹0.05 for most NSE scrips); orders rejected if unaligned or outside
    the daily price band.
  - **STOP_MARKET / STOP_LIMIT / BRACKET** orders with full OCO semantics.
  - **T+1 settlement** with deliverable-qty enforcement and same-day
    intraday round-trips through `ProductType.INTRADAY`.
  - **Auto-square-off** of intraday positions at 15:15 IST.
  - **Mark-to-bid** valuation — unrealized P&L at the actual exit price.
  - **Synthetic L2 order book** (using real provider depth when available)
    with queue-position tracking and Almgren-style market impact.
  - **Latency + random rejection** simulation for testing flaky upstreams.
- Realistic Indian fees: brokerage, **STT**, exchange charges, **GST**,
  SEBI charges, **stamp duty**, **DP charges**. Configurable per-broker,
  with date-versioned schedules.
- Thread-safe SQLite persistence with WAL mode and atomic transactions.
- Session-phase awareness (PRE_OPEN / REGULAR / POST_CLOSE / CLOSED).
- Limit-order support with a background watcher loop and configurable
  partial fills.
- Multi-account support — one DB, many independent accounts.
- **Slippage model**, **risk controls** (kill switch, whitelist, notional
  caps), **idempotency keys**, **broker presets** (Zerodha, Upstox, Groww,
  Angel One, ICICIdirect), **symbol master** with delisting, **immutable
  cash ledger**, **corporate actions** (splits + dividends), and an
  **event log + observability bus**.
- Optional CLI for inspecting state.
- Optional MCP server example so any LLM agent can use the broker as a tool.

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
order = broker.buy("RELIANCE", 10)
print(order.filled_avg_price, order.fees_paid)
```

Full getting-started walkthrough and usage examples for every feature are
in **[QUICKSTART.md](QUICKSTART.md)**. Runnable scripts live in
[`examples/`](examples/).

## How fees are modelled

Defaults match a typical discount-broker delivery account:

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
| Synthetic order book (not a real matching engine) | Large-order fills are modelled, not exact | Uses real provider depth when available; accurate for retail sizes |
| Snapshot prices, not a tick stream | Intraday fills price off the last snapshot | Negligible at daily/swing cadence |
| Corporate actions applied manually | Splits/dividends via explicit calls | `apply_split` / `apply_dividend` |
| No margin / leverage / short selling | Cash, long-only account | Intentional |
| No options or F&O | Equity only | Out of scope |

## License

[MIT](LICENSE) — use it however you want.

## Disclaimer

This is a **simulated** broker. It does not place real trades. Always verify
calculations against your actual broker's contract notes before relying on
the simulator's outputs for tax, compliance, or investment decisions.
