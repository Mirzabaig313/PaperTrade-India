# Architecture

This is a small, focused package: one broker, one persistence layer, one
fee engine, one calendar, one price-feed coordinator, one optional
background worker. The interfaces between them are explicit so any one
piece can be replaced or extended without disturbing the rest.

## Module map

```
                 ┌────────────────────────────────┐
                 │       IndiaPaperBroker         │
                 │   (orchestrates buy/sell/      │
                 │    positions/account API)      │
                 └──┬───┬───┬──────┬──────┬───────┘
                    │   │   │      │      │
                    ▼   ▼   ▼      ▼      ▼
              ┌─────┐ ┌─────┐ ┌──────┐ ┌────────┐ ┌──────────┐
              │Pers-│ │Price│ │Mkt   │ │ Fees   │ │ Limit    │
              │istce│ │Feed │ │Hours │ │ Engine │ │ Watcher  │
              │     │ │     │ │+ Cal.│ │        │ │ (opt-in) │
              └─────┘ └─────┘ └──────┘ └────────┘ └──────────┘
```

Every collaborator is injected through the broker's constructor. Defaults
exist so casual use is one line; tests and advanced users override
everything.

## Component responsibilities

### `IndiaPaperBroker` (`broker.py`)

Public face of the package. Implements `BrokerInterface`. Coordinates
all other components; never reaches for global state.

Two state-changing operations: `buy()` and `sell()`. Both go through the
same `_submit_order()` validator, which:

1. Rejects invalid input (non-positive qty, missing limit price).
2. Enforces market hours (MARKET orders rejected when closed; LIMIT
   orders queue for next session, modeling AMO behavior).
3. Routes to either `_execute_market_order` or `_queue_limit_order`.

### `Persistence` (`persistence.py`)

Thread-safe SQLite wrapper around the four tables:
`account`, `positions`, `orders`, `trades`. Three properties matter:

- **WAL journal mode**: many readers + one writer without blocking.
- **Per-thread connections**: SQLite connections aren't safe to share
  across threads. Each thread gets its own; PRAGMAs are re-applied per
  connection because SQLite forgets them across connection boundaries.
- **Atomic transactions**: `transaction()` is a context manager that
  wraps multi-statement operations in `BEGIN IMMEDIATE` and rolls back
  on any exception. Buy and sell are multi-statement (cash UPDATE,
  positions UPSERT, order INSERT, trade INSERT) — they must be atomic.

Schema is enforced with CHECK constraints (`cash >= 0`, valid
`status`/`side`/`order_type` values) so even a future bug can't slip a
nonsensical row through.

### `IndianFeeEngine` (`fees.py`)

Pure function: `(side, qty, price, exchange) → FeeBreakdown`.

Encodes the seven Indian fee components (brokerage, STT, exchange charge,
GST, SEBI charges, stamp duty, DP charges) and rounds each to paise.
Defaults model a discount-broker delivery account; pass a custom
`FeeConfig` to model intraday or full-service brokers.

See `docs/FEES.md` for the formulas.

### `NSECalendar` (`market_hours.py`)

Loads holiday JSON files from `data/`, then answers four questions:
`is_holiday(d)`, `is_trading_day(d)`, `is_market_open(dt=None)`,
`next_open(dt=None)`. All times are in IST (`Asia/Kolkata`); naive
datetimes are interpreted as IST.

Holiday files are versioned per year (`nse_holidays_2026.json`,
`nse_holidays_2027.json`, ...). The community can refresh each year
independently via `scripts/update_nse_holidays.py`.

### `PriceFeed` (`price_feed.py`)

Three-layer fallback chain:

```
yfinance ─→ jugaad-data ─→ cached last-known
```

The first provider returning a non-`None` price wins. Successful fetches
update both a short-lived in-memory cache (5-second TTL by default,
absorbs rapid repeats inside a single `get_positions()` call) and a
persistent cache (1 hour TTL, used as last-resort fallback). Failure of
all three raises `PriceUnavailableError`.

When this happens for an *existing* position, `Position.current_price`
falls back to `avg_cost` and `Position.current_price_stale` is set to
`True` so callers don't mistake the resulting "0% P&L" for a real
break-even.

### `LimitOrderWatcher` (`limit_orders.py`)

Optional `threading.Thread` that ticks every `interval_seconds`. Each
tick:

1. Skip if market is closed.
2. Fetch all PENDING limit orders.
3. Get current price for each unique symbol.
4. Fill BUY limits when `current_price <= limit_price`.
5. Fill SELL limits when `current_price >= limit_price`.

Fills go through `broker._execute_limit_fill`, which claims the order
race-safely (`UPDATE ... WHERE status='pending'`) before applying cash
and position changes. If the order moved out of PENDING between snapshot
and fill (e.g. user cancelled), `OrderNoLongerPending` is raised and the
whole fill rolls back.

The watcher's `tick()` method is public so tests can drive it without
starting a thread.

## Data flow

### Buy

```
buy()
 ├─ _submit_order(): validate input, check market hours
 └─ _execute_market_order():
     ├─ price_feed.get_price()                  # may raise
     ├─ fee_engine.calculate()                  # pure
     └─ persistence.transaction():
         ├─ _apply_buy(): UPDATE cash, UPSERT positions
         ├─ _record_order(): INSERT into orders (status=FILLED)
         └─ _record_trade(): INSERT into trades
```

### Sell

```
sell()
 ├─ _submit_order(): validate, check hours
 └─ _execute_market_order():
     ├─ price_feed.get_price()
     ├─ fee_engine.calculate()
     └─ persistence.transaction():
         ├─ _apply_sell(): UPDATE cash + realized_pl_total,
         │                 update or DELETE position
         ├─ _record_order()
         └─ _record_trade()
```

### Limit fill (background)

```
LimitOrderWatcher.tick()
 ├─ broker.get_orders(status=PENDING)            # snapshot
 ├─ for each unique symbol: price_feed.get_price()
 └─ for each crossing order:
     broker._execute_limit_fill(order, price):
         persistence.transaction():
             ├─ UPDATE orders SET status=FILLED  # claim, race-safe
             │   WHERE status='pending'          #
             ├─ if rowcount == 0:                #
             │   raise OrderNoLongerPending      # rolls back
             ├─ _apply_buy / _apply_sell
             ├─ UPDATE realized_pl on order
             └─ _record_trade
```

## Cost-basis convention

`Position.avg_cost` is the **per-share economic cost basis** including
prorated buy-side fees. So `qty * avg_cost == total cash spent acquiring
the position`. This makes realized P&L on a sell:

```
realized_pl = (price - avg_cost) * qty - sell_fees
```

which naturally captures *both* sides of fees over a round-trip.

Partial sells leave `avg_cost` of the remaining sleeve unchanged — we
don't re-prorate buy fees across leftover shares.

## Multi-account model

`account_id` is part of the primary key on every account-scoped table.
One SQLite file can host arbitrarily many independent accounts:

```python
alice = IndiaPaperBroker(account_id="alice", db_path="agents.db")
bob   = IndiaPaperBroker(account_id="bob",   db_path="agents.db")
# fully isolated; alice.buy("RELIANCE", 5) is invisible to bob
```

Per-account `cancel_order` / `get_order` / `_execute_limit_fill`
SELECTs all include `WHERE account_id = ?`, preventing cross-account
contamination.

## Threading model

- One broker instance is safe to share across threads.
- One persistence file is safe to share across multiple broker instances
  (multi-account or otherwise). WAL handles concurrency.
- `LimitOrderWatcher` runs as a daemon thread by default; it does not
  block process shutdown.
- Concurrency tests prove the cash invariant holds under 50 concurrent
  buys with mixed success/insufficient-funds outcomes.

## What's deliberately not modeled

(Documented in detail in the README, design doc, and `CHANGELOG.md`.)

- Slippage
- Real-time tick data (yfinance is 15-min delayed)
- Corporate actions (splits, dividends, bonuses)
- Margin / leverage / shorting
- Partial fills on limit orders
- Options / F&O

These are roadmap items, not bugs. Add them only if a real use case
surfaces.
