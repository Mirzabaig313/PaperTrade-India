# Cookbook

Task-oriented recipes for `papertrade-india`. Every recipe is short,
runnable, and explains the reasoning behind the choices — not just the
syntax.

If you're looking for *what the package does*, read the
[README](../README.md). If you're looking for *how it works internally*,
read [`docs/ARCHITECTURE.md`](ARCHITECTURE.md). This file is for "I
need to do X — show me a working snippet."

---

## 1. Start here in 30 seconds

```python
from papertrade_india import quickstart

broker = quickstart()
order = broker.buy("RELIANCE", 1)
print(order.filled_avg_price, order.fees_paid)
print(broker.get_account())
```

`quickstart()` returns a broker with safe defaults: ₹1M starting cash,
realistic fees, 5 bp slippage, strict symbol master pre-loaded with the
NSE-30 sample, stale-price hard-reject ON. Override any field
explicitly.

---

## 2. Verify simulator P&L against a real contract note

You ran a few trades in the simulator and want to compare the fee
breakdown line-by-line against your broker's PDF.

```python
from papertrade_india import IndianFeeEngine, OrderSide, Exchange

# Use the same FeeConfig your broker uses. The presets cover the common ones:
from papertrade_india.presets import ZERODHA_DELIVERY

engine = IndianFeeEngine(ZERODHA_DELIVERY)
fb = engine.calculate(
    side=OrderSide.BUY, qty=10, price=2500.0, exchange=Exchange.NSE,
)
print(fb)
# Brokerage: ₹0.00, STT: ₹25.00, Exchange: ₹0.81, GST: ₹0.15,
# SEBI: ₹0.03, Stamp: ₹3.75, DP: ₹0.00, Total: ₹29.74
```

For a position breakdown that decomposes `avg_cost` into principal +
prorated fees:

```python
bd = broker.get_position_basis_breakdown("RELIANCE")
print(bd["principal"], bd["fees_in_basis"], bd["total_basis"])
```

If your numbers diverge from the broker's PDF, the issue is almost
always one of:

1. Wrong `FeeConfig` (intraday vs delivery, full-service vs discount).
2. Statutory rate change you haven't picked up — see recipe #5.
3. Broker-specific tier discount.

---

## 3. Drive a deterministic backtest with `ReplayClock`

You want a backtest that doesn't depend on wall-clock time.

```python
from datetime import datetime, timedelta
from papertrade_india import (
    IST, IndiaPaperBroker, PriceFeed, ReplayClock, SlippageConfig,
)

class StubProvider:
    def __init__(self): self.prices = {}
    def set(self, sym, p): self.prices[sym] = p
    def get_price(self, sym): return self.prices.get(sym)

stub = StubProvider()
feed = PriceFeed(providers=[stub], short_cache_ttl_seconds=0)
clock = ReplayClock(datetime(2026, 5, 18, 9, 15, tzinfo=IST))

broker = IndiaPaperBroker(
    initial_capital=500_000,
    db_path="bt.db",
    price_feed=feed,
    clock=clock,
    slippage_config=SlippageConfig(bps=5),
)

# Drive a 5-day strategy by advancing the clock + price between each call.
for day, close in enumerate([2400, 2450, 2480, 2520, 2550]):
    next_day = clock.now() + timedelta(days=1)
    while not broker.calendar.is_trading_day(next_day.date()):
        next_day += timedelta(days=1)
    clock.set(next_day.replace(hour=10, minute=0))
    stub.set("RELIANCE", close)
    broker.buy("RELIANCE", 1) if day == 0 else None
```

Why a `ReplayClock`?

- Order timestamps reflect the simulated time.
- Market-hour and session-phase checks use the simulated time.
- `LimitOrderWatcher.tick()` runs synchronously without sleeping.
- Idempotency-key TTLs respect simulated time.

See `examples/07_backtest_replay.py` for the full version.

---

## 4. Use the broker as a Jupyter cell

Jupyter cells often re-run, which would normally double-buy on each
re-execution. Use idempotency keys keyed by cell-run identity:

```python
import uuid

# In an early cell, capture a stable key for THIS notebook session.
SESSION_KEY = str(uuid.uuid4())

# In a later cell, use it. Re-running the cell is safe — replays return
# the original order rather than placing a new one.
order = broker.buy("RELIANCE", 1, idempotency_key=f"{SESSION_KEY}-buy-1")
print(order.filled_avg_price)
```

Want a shared SQLite file across notebooks but per-notebook accounts?
Use distinct `account_id`s on the same `db_path`.

---

## 5. Handle a mid-year statutory fee change

The Indian government tweaks STT or stamp duty in the union budget. To
keep your backtest's pre-budget orders correct and post-budget orders
correct:

```python
from datetime import date
from papertrade_india import FeeConfig, FeeSchedule, IndiaPaperBroker

schedule = FeeSchedule(
    default=FeeConfig(stt_pct_buy=0.0010),         # pre-history default
    effective_from={
        date(2026, 4, 1): FeeConfig(stt_pct_buy=0.00125),  # hypothetical hike
    },
)
broker = IndiaPaperBroker(fee_config=schedule)
```

The broker picks the right `FeeConfig` from the order's trade date — so
the same backtest, replayed after the rate change is published, produces
correct historical P&L.

---

## 6. Run an autonomous-agent deployment safely

Pre-trade risk controls + idempotency + stale-price hard-reject =
"the agent can't accidentally blow up the account."

```python
from papertrade_india import IndiaPaperBroker, RiskConfig

broker = IndiaPaperBroker(
    risk_config=RiskConfig(
        symbol_whitelist=frozenset({"RELIANCE", "INFY", "TCS"}),
        max_order_notional=100_000.0,             # ₹1L cap per order
        max_position_pct_of_equity=0.20,          # 20% per position
    ),
    enforce_fresh_prices=True,                    # fail-stop on stale data
)

# Operator kill-switch: trip via env var with no code redeploy.
# PAPERTRADE_INDIA_KILL_SWITCH=1
```

Always pass an idempotency key per logical decision so the agent can
retry without double-filling:

```python
order = broker.buy(
    "RELIANCE", 5,
    idempotency_key=f"agent-decision-{decision.id}",
)
```

---

## 7. Wire into OpenTelemetry / Prometheus

The package ships zero observability deps. Wire on your side via the
event bus:

```python
from prometheus_client import Counter

orders_filled = Counter(
    "papertrade_orders_filled_total",
    "Filled orders",
    ["symbol", "side"],
)

def to_prom(event):
    if event.event_type == "order_filled":
        orders_filled.labels(
            symbol=event.payload["symbol"],
            side=event.payload["side"],
        ).inc()

broker.events.subscribe(to_prom, name="prom-shipper")
```

OpenTelemetry is similar — wire a span emitter in the callback. The
event ships with `recorded_at`, so you don't need to call `now()` again.

A subscriber that crashes is logged and skipped; other subscribers
continue. The broker's correctness doesn't depend on your metrics
shipper being healthy.

To catch up a subscriber added mid-run:

```python
broker.events.replay_from_broker(broker)
```

---

## 8. Spot-audit the cash invariant

The package guarantees `account.cash == sum(cash_movements.amount)`
within ₹0.01 (paise rounding). Verify it any time:

```python
assert broker.verify_cash_invariant(), "ledger drift!"
```

A False return logs a structured WARN with the magnitude and the most
recent ledger rows. CI hook:

```bash
papertrade-india verify-invariant --account my-acct
echo $?  # 0 if OK, 3 if drift
```

Or get everything in one panel:

```bash
papertrade-india status --account my-acct
```

---

## 9. Reconcile against a contract note

Two angles. First, per-leg fee breakdown:

```python
fb = broker.fee_engine.calculate(
    side=OrderSide.SELL, qty=10, price=2600.0, exchange=Exchange.NSE,
)
# Line up each component against your PDF:
# Brokerage / STT / Exchange / GST / SEBI / Stamp / DP / Total
```

Second, position basis decomposition:

```python
bd = broker.get_position_basis_breakdown("RELIANCE")
# {
#   "qty": 10.0,
#   "principal": 25023.78,        # what you paid for shares
#   "fees_in_basis": 5.97,        # prorated buy-side fees still embedded
#   "total_basis": 25029.75,      # qty * avg_cost
#   "ledger_buy_principal": 25000.00,
#   "ledger_buy_fees": 5.97,
#   "ledger_sell_principal": 0.00,
#   "ledger_sell_fees": 0.00,
# }
```

The buy-fee figure should match the sum of fees on your buy contract
notes. If it doesn't, the FeeConfig is the suspect.

---

## 10. Apply a stock split or bonus issue

Your holding gets a 2:1 split (or a 1:1 bonus, same thing):

```python
broker.apply_split("RELIANCE", ratio_num=2, ratio_den=1, notes="2:1 split")
```

Reverse split (1:5):

```python
broker.apply_split("PENNYSTOCK", ratio_num=1, ratio_den=5)
```

Cash dividend (₹12.50 per share, applied on ex-date):

```python
broker.apply_dividend("ITC", amount_per_share=12.50, notes="Q4 FY26")
```

Both record an audit row in `corporate_actions` and emit a
`corporate_action` event on the bus. Total cost basis is preserved on
splits; cash is credited on dividends.

`apply_split` and `apply_dividend` are NOT idempotent — calling twice
applies the action twice. Wrap in your own dedup if you replay events.

---

## 11. Test against your agent's risk rules

You want to confirm your agent never fires an order that exceeds ₹50K.

```python
from papertrade_india import IndiaPaperBroker, RiskConfig, RiskViolation

broker = IndiaPaperBroker(
    risk_config=RiskConfig(max_order_notional=50_000.0),
)

# Drive your agent against this broker. Any order that exceeds the cap
# raises RiskViolation BEFORE any state mutation. Catch it in the agent
# and let your test assert the rejection happened.
try:
    your_agent.run(broker)
except RiskViolation as e:
    print(f"agent correctly stopped: {e}")
```

The same pattern works for the symbol whitelist (`SymbolNotFound` /
`SymbolDelisted`), the kill switch (`KillSwitchActive`), or stale prices
(`StalePriceRejected`).

---

## 12. Migrate an existing v0.1.x DB to a newer release

If you upgraded the package and have an existing SQLite file from a
prior release, the broker auto-applies any pending migrations on the
first `Persistence.__init__`. No action needed.

For pre-deploy / CI:

```bash
papertrade-india migrate --db path/to/db.sqlite
```

Reports the version range applied, or "already at vN" if up-to-date.
Forward-incompatibility (a DB stamped at a higher version than the
package knows) raises a `RuntimeError` with a clear message — refusing
to operate is safer than corrupting data.

---

## 13. Replay the event log to a new subscriber

A subscriber added mid-run (e.g. after a metrics-shipper restart)
missed historical events. Backfill:

```python
broker.events.subscribe(my_metrics_shipper, name="prom")
broker.events.replay_from_broker(
    broker,
    event_types=("order_filled", "order_partially_filled"),
)
```

Replay respects each subscriber's own filter — others on the bus that
care about a different slice of events get their own slice.

For a time-bounded replay:

```python
from datetime import datetime, timedelta
broker.events.replay_from_broker(
    broker,
    since=datetime.now() - timedelta(hours=1),
)
```

---

## 14. Multi-account in one DB

Run multiple agents (or the same agent under multiple personas) against
one SQLite file:

```python
db = "agents.db"
alice = IndiaPaperBroker(account_id="alice", db_path=db)
bob = IndiaPaperBroker(account_id="bob", db_path=db)

alice.buy("RELIANCE", 5)
bob.buy("INFY", 10)

# Each account is fully isolated. No cross-account contamination —
# every account-scoped table is keyed on (account_id, ...) in the schema.
```

---

## 15. Limit-order watchers without busy-waiting

The `LimitOrderWatcher` background thread calls
`broker.price_feed.get_quote()` every `interval_seconds`. For backtests
or tests, drive ticks manually:

```python
from papertrade_india import LimitOrderWatcher

watcher = LimitOrderWatcher(broker, interval_seconds=999)
# In a loop, advance time + price, then tick:
for tick in synthetic_ticks:
    stub.set("RELIANCE", tick.price)
    clock.set(tick.timestamp)
    watcher.tick()
```

For production, opt into periodic idempotency-key cleanup so your
`idempotency_keys` table doesn't grow forever:

```python
watcher = LimitOrderWatcher(
    broker,
    interval_seconds=5,
    idempotency_cleanup_every=720,  # 720 ticks * 5s = once per hour
    idempotency_ttl_hours=24,
)
watcher.start()
```

---

## See also

- [README](../README.md) — overview, install, configuration knobs
- [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) — module map, threading model, data flow
- [`docs/FEES.md`](FEES.md) — fee formulas + worked examples
- [`docs/CONTRIBUTING.md`](CONTRIBUTING.md) — local setup, style, PR rules
