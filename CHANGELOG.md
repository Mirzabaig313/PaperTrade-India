# Changelog

All notable changes to **papertrade-india** are documented here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Pre-1.0 releases may include breaking changes in MINOR bumps; each one will
be called out here.

## [Unreleased]

### Architecture refactor (internal, no public API changes)

The 3,363-line ``broker.py`` god-class has been broken up into cohesive
subsystems. Phases 1, 2, 3, 4a, and 5 of the
[architecture refactor plan](docs/architecture_refactor.md) have landed.

#### New package layout

- ``domain/`` — pure value types and stateless rules
  - ``domain/models.py`` (← ``models.py``)
  - ``domain/exceptions.py`` (← ``exceptions.py``)
  - ``domain/rules/risk.py`` (← ``risk.py``)
  - ``domain/rules/tick_lot_band.py`` (the validation half of
    ``microstructure.py``)
- ``execution/`` — fill mechanics
  - ``execution/slippage.py``, ``execution/fees.py``,
    ``execution/settlement.py``, ``execution/simulation.py``
    (← root ``slippage.py`` etc.)
  - ``execution/book.py`` (the order-book half of ``microstructure.py``)
- ``orders/`` — order lifecycle
  - ``orders/partial_fills.py`` (← ``partial_fills.py``)
  - ``orders/preopen.py`` (← ``preopen.py``)
  - ``orders/state.py`` (NEW — ``_record_order``, ``_record_trade``,
    ``_row_to_order`` extracted from ``IndiaPaperBroker``)
- ``reads/`` — read-only views over broker state (NEW)
  - ``reads/positions.py``, ``reads/account.py``, ``reads/orders.py``
- ``corporate_actions/`` — splits, bonuses, rights, dividends (NEW
  package replacing ``corporate_actions.py``)
- ``infrastructure/`` — persistence, observability, clock, market hours,
  symbols, ledger, events, idempotency (← root modules of the same
  names)
- ``workers/limit_orders.py`` (← ``limit_orders.py``)
- ``_context.py`` — ``BrokerContext`` dataclass that carries shared
  collaborators to subsystem functions

#### Behavior

- All 490 tests pass without modification.
- ``IndiaPaperBroker``'s public surface is unchanged: ``buy``, ``sell``,
  ``get_positions``, ``get_account``, ``apply_split`` and friends keep
  the same signatures and return shapes.
- ``broker.py`` line count: 3,363 → 2,717 (-19%). Order flow
  (``_submit_order``, ``_execute_market_order``, bracket/stop/limit
  bookkeeping, ~2,300 lines) is the Phase 4b/4c/4d work that remains.

#### Deprecated import paths

The old ``papertrade_india.<module>`` paths still work for now but emit
a ``DeprecationWarning`` pointing at the new home. Update imports to
the new locations; the shim modules will be removed in v0.3.

| Old path | New path |
|:---|:---|
| ``papertrade_india.models`` | ``papertrade_india.domain.models`` |
| ``papertrade_india.exceptions`` | ``papertrade_india.domain.exceptions`` |
| ``papertrade_india.risk`` | ``papertrade_india.domain.rules.risk`` |
| ``papertrade_india.microstructure`` (validators) | ``papertrade_india.domain.rules.tick_lot_band`` |
| ``papertrade_india.microstructure`` (book sim) | ``papertrade_india.execution.book`` |
| ``papertrade_india.slippage`` | ``papertrade_india.execution.slippage`` |
| ``papertrade_india.fees`` | ``papertrade_india.execution.fees`` |
| ``papertrade_india.settlement`` | ``papertrade_india.execution.settlement`` |
| ``papertrade_india.simulation`` | ``papertrade_india.execution.simulation`` |
| ``papertrade_india.partial_fills`` | ``papertrade_india.orders.partial_fills`` |
| ``papertrade_india.preopen`` | ``papertrade_india.orders.preopen`` |
| ``papertrade_india.limit_orders`` | ``papertrade_india.workers.limit_orders`` |
| ``papertrade_india.persistence`` | ``papertrade_india.infrastructure.persistence`` |
| ``papertrade_india.migrations`` | ``papertrade_india.infrastructure.migrations`` |
| ``papertrade_india.idempotency`` | ``papertrade_india.infrastructure.idempotency`` |
| ``papertrade_india.ledger`` | ``papertrade_india.infrastructure.ledger`` |
| ``papertrade_india.events`` | ``papertrade_india.infrastructure.events`` |
| ``papertrade_india.observability`` | ``papertrade_india.infrastructure.observability`` |
| ``papertrade_india.clock`` | ``papertrade_india.infrastructure.clock`` |
| ``papertrade_india.market_hours`` | ``papertrade_india.infrastructure.market_hours`` |
| ``papertrade_india.symbols`` | ``papertrade_india.infrastructure.symbols`` |

The umbrella ``papertrade_india`` re-exports stay unchanged: ``from
papertrade_india import IndiaPaperBroker, Position, Order, ...``
continues to work.

### Added (Tier-B/C: 1.0 readiness)

#### Versioned schema migrations (Tier-C, 1.0 blocker)
- New `migrations.py` module with a `@migration(N)` decorator,
  `MIGRATIONS` registry, and `run_migrations(conn)` runner.
- A single `schema_version` table tracks the applied version per DB.
  Brand-new DBs see no row and start at v0; legacy 0.1.x DBs (tables
  present, no row) are detected and stamped at v1 without re-running.
- Each migration runs in its own `BEGIN IMMEDIATE` transaction so a
  failure leaves the previous state intact.
- Forward-incompatibility guard: if a DB reports a higher version than
  the package knows, `run_migrations` raises `RuntimeError` rather than
  risking corruption.
- `papertrade-india migrate --db PATH` CLI command for pre-deploy /
  recovery use.
- Decorator validates positive versions and rejects duplicates loudly
  so a future double-PR collision can't silently overwrite.
- The pre-existing `Persistence.SCHEMA` and `EXTENSION_SCHEMAS`
  symbols are now thin re-exports that point at the migrations module
  for backwards compatibility.

This is the 1.0 blocker called out in the suggestion review: any future
schema change can now ship as a v2/v3/... migration and existing 0.1
users get the new columns automatically on first connect after upgrade.

#### Cookbook (Tier-B)
- New `docs/COOKBOOK.md` with 15 task-oriented recipes covering
  contract-note reconciliation, replay backtests, Jupyter usage,
  mid-year fee changes, autonomous-agent safety, OTel/Prometheus
  wiring, multi-account, and migrations.

### Tests
- 12 new tests covering fresh DB at head, legacy detection,
  forward-incompatibility, decorator validation, and the migrate CLI.
- Total: **317 passing**, 3 opt-in E2E skipped.

### Pushed back on (with reasons)
- **Tier-C `account.cash` integer paise** — the cash-invariant fuzz
  test (200 random ops, exact match within ₹0.01) has held since Tier 2
  with float math. No evidence of drift biting in practice. Switching
  to integer paise touches ~15 files and forces re-validation of every
  fee calculation, for a problem nobody has yet. Document as "if/when"
  in the limitations section.
- **Tier-C `PriceFeed.subscribe()` for streaming** — speculative.
  No streaming data source to test against. The `PriceProvider`
  Protocol already accepts any `get_price` impl; a future streaming
  provider can wrap a stream into pull-based polling without breaking
  the API.

### Added (Tier-A: completing the package)

#### Backtest replay mode
- New `clock.py` module with `Clock` Protocol, `WallClock` (default),
  and `ReplayClock(start_at)` for deterministic backtests.
- `IndiaPaperBroker(clock=...)` accepts any `Clock`. Every `datetime.now(IST)`
  call inside the broker now routes through the clock, including order
  timestamps, ledger `recorded_at`, event `recorded_at`, market-hour
  checks, session-phase checks, and DAY-tif expiry timestamps.
- `broker.clock` property exposes the clock; the limit-order watcher
  uses it for its own market-hour checks.
- `ReplayClock.advance(timedelta)` and `.set(datetime)` move time
  forward; backwards moves raise `ValueError` so the simulator's
  audit log stays monotonic.
- New example `examples/07_backtest_replay.py` runs a 10-day momentum
  backtest with no wall-clock waiting.

#### Quickstart factory
- New `quickstart()` factory returns a safe-by-default broker
  pre-configured with the Zerodha-delivery preset, 5 bp slippage,
  strict symbol master with the bundled NSE-30 sample loaded, and
  stale-price hard-reject ON. Saves first-time users from the
  multi-parameter cliff.

#### Position basis breakdown
- New `broker.get_position_basis_breakdown(symbol)` returns
  `{qty, principal, fees_in_basis, total_basis, ledger_buy_principal,
  ledger_buy_fees, ledger_sell_principal, ledger_sell_fees}`. Lets
  users reconcile a position's `avg_cost` against a real broker's
  contract note.

#### Status CLI command
- New `papertrade-india status` consolidates account + positions +
  ledger tail + events tail + invariant check into one Rich panel.
  Same exit-3-on-drift contract as `verify-invariant` so it can also
  serve as a cron health check.

#### Watcher idempotency cleanup
- `LimitOrderWatcher(idempotency_cleanup_every=N, idempotency_ttl_hours=H)`
  runs `broker.cleanup_idempotency_keys(hours=H)` every Nth tick.
  Off by default; opt-in for users who don't want to wire their own
  cron.

#### Invariant logging on drift
- `verify_cash_invariant()` now emits a structured WARN with the drift
  amount, the account id, and the most recent 5 ledger rows when it
  returns False. Triage-friendly out of the box.

#### Alpaca contract test
- New `tests/integration/test_alpaca_contract.py` pins the "drop-in
  replacement for Alpaca's TradingService" claim with structural
  signature checks. Method names, required params, optional params,
  and Order/Account/Position field shapes are all verified.

### Tests
- 32 new tests across clock, replay-broker, quickstart, basis
  breakdown, status CLI, watcher cleanup, and Alpaca contract.
- Total: **305 passing**, 3 opt-in E2E skipped.

### Added (Tier-3 polish — closing the loop)
- `EventBus.subscribe(fn, event_types=...)`: per-subscription filter so
  callbacks only fire for matching event types. Empty set = drop all
  (useful as a deliberate no-op for testing).
- `EventBus.replay_from_broker(broker, since=..., event_types=...)`:
  re-dispatches persisted events to current subscribers in chronological
  order. Catches up subscribers added mid-run.
- `BrokerEvent.recorded_at`: timestamp on every delivered event so
  subscribers can do their own time-based logic without re-querying.
- New CLI commands: `ledger`, `events` (with `--type` filter),
  `verify-invariant` (exit 3 on drift), `phase`.
- `MarketClosedError` now names the current `SessionPhase` so an agent
  can distinguish "wait 7 minutes for REGULAR" from "wait until tomorrow".
- MCP server example exposes Tier-3 surfaces:
  `get_session_phase`, `get_cash_ledger`, `get_recent_events`,
  `verify_cash_invariant`.
- New example `examples/06_tier3_observability.py` demonstrates the
  bus + filter + replay end-to-end.

### Tests
- 14 new tests covering filter sets, replay, recorded_at on delivered
  events, phase-aware errors, and CLI smoke tests for all new commands.
- Total: **246 passing**, 3 opt-in E2E skipped.

### Added (Tier-3 realism)

#### Configurable partial fills
- New `PartialFillConfig` (`max_per_tick`, `max_pct_per_tick`,
  `min_fill_qty`, `enabled`). The watcher now slices limit fills per
  tick instead of all-or-nothing.
- New `OrderStatus.PARTIALLY_FILLED` is now actually used (was reserved).
- `Order.filled_avg_price` becomes volume-weighted across slices.
- `cancel_order()` works on PARTIALLY_FILLED orders too — already-filled
  qty stays in the position; the rest is dropped.
- Honest framing: this is a configurable per-tick fill cap, not real
  bid/ask depth. We have no order book.

#### Session phases
- New `SessionPhase` enum: `CLOSED`, `PRE_OPEN`, `REGULAR`, `POST_CLOSE`.
- New `NSECalendar.current_phase(dt=None)` returns the active phase.
- `is_market_open()` now means "is the REGULAR continuous session
  active" — kept the same semantics as before so legacy code still works.
- New `IndiaPaperBroker.current_session_phase()` exposes the phase.

#### Persisted event log
- New `events` table: append-only, with typed `event_type`, optional
  `account_id`, `order_id`, JSON `payload`, and `recorded_at`.
- Events emitted: `order_submitted`, `order_filled`,
  `order_partially_filled`, `order_cancelled`, `order_expired`,
  `order_rejected`, `position_opened`, `position_closed`,
  `corporate_action`, `account_reset`.
- Events are persisted in the same transaction as the state change,
  so the log can never disagree with what was committed.
- New `IndiaPaperBroker.get_events(limit, event_types)` for queries.
- Honest framing: this is a parallel audit stream, NOT event-sourcing.
  Account/positions/orders are still authoritative.

#### Observability callback bus
- New `EventBus` for in-process pub/sub of `BrokerEvent` callbacks.
  Wire to OpenTelemetry, Prometheus, structured logs, or anything else.
- Subscribers fire AFTER the SQL transaction commits (so they never
  see uncommitted state).
- One bad subscriber doesn't poison the bus — failures are logged
  per-subscriber and the next one still runs.
- New `IndiaPaperBroker(event_bus=...)` parameter; `broker.events.subscribe(fn)`.
- New `stdlib_log_subscriber` convenience for development.

#### Per-symbol slippage
- `SlippageConfig` gains a `per_symbol_bps: dict[str, float]` field.
  Use to model illiquid micro-caps with higher slippage than mid-caps.
- Default `bps` still applies to symbols not in the override map.

### Schema additions
- `events` table (account-CASCADE; partially scoped — some events have no account_id).

### Tests
- 42 new tests covering session phases, partial-fill slicing,
  partial-fill cash invariant, event-log persistence, callback bus
  isolation, and per-symbol slippage on the broker.
- Total: **232 passing**, 3 opt-in E2E skipped.

### Added (Tier-2 realism)

#### Immutable cash ledger
- New `cash_movements` table: append-only row per cash mutation, with
  signed `amount`, typed `reason`, optional `order_id`/`symbol`/`notes`,
  and `recorded_at` timestamp.
- Reasons: `buy_principal`, `buy_fees`, `sell_principal`, `sell_fees`,
  `dividend`, `adjustment`, `initial_capital`.
- New `IndiaPaperBroker.get_cash_movements(limit=200)` returns recent
  rows for an account.
- New `IndiaPaperBroker.verify_cash_invariant()` asserts
  `account.cash == sum(cash_movements.amount)` within ₹0.01. Run from
  audits / health checks.
- Account creation seeds the ledger with an `initial_capital` row;
  `reset()` re-seeds so the invariant holds across resets.

#### Corporate actions
- New `IndiaPaperBroker.apply_split(symbol, ratio_num, ratio_den, ...)`
  multiplies qty and divides avg_cost by the ratio. Total cost basis
  preserved, no cash impact. Bonus issues are a degenerate split
  (1:1 bonus = 2:1 split).
- New `IndiaPaperBroker.apply_dividend(symbol, amount_per_share, ...)`
  credits `amount_per_share * qty_held` to cash, recorded in the ledger
  as a `dividend` row. Tax-aware behavior is not modeled.
- New `corporate_actions` table (audit log of every action applied).

#### Date-versioned fee schedule
- New `FeeSchedule(default, effective_from={date: FeeConfig})`. Broker
  picks the right config based on the order's trade date — useful when
  STT/GST/stamp duty change in budget.
- Bare `FeeConfig` still works (auto-wrapped in a single-entry schedule).

#### Stale-price hard-reject
- New `IndiaPaperBroker(enforce_fresh_prices=True)` rejects fills whose
  underlying price came from the long-lived stale-price cache.
- New `Quote` dataclass with `price`, `source`, `fetched_at`, `is_stale`.
- New `PriceFeed.get_quote(symbol)` returns a `Quote` (existing
  `get_price()` returns float, kept for backwards compatibility).
- Limit watcher pre-checks staleness so stale orders skip cleanly
  (stay PENDING for the next live tick) without unnecessary log noise.
- New `StalePriceRejected` exception.

### Schema additions
- `cash_movements` table (account-CASCADE, no order FK by design — see
  `ledger.py` docstring for the rationale).
- `corporate_actions` table (audit log).

### Tests
- 38 new tests covering ledger invariant under random ops, splits +
  dividends + cost-basis preservation, fee schedules with effective
  dates, and stale-price reject paths for both market and limit fills.
- Total: **190 passing**, 3 opt-in E2E skipped.

### Added (Tier-1 realism)
- **Slippage model** (`slippage.py`): configurable basis-point slippage
  on market fills. Default 0 bps (legacy behavior); pass
  `SlippageConfig(bps=5)` for realistic 5-bp impact. Limit fills are
  unaffected by default; opt-in with `apply_to_limits=True` (capped by
  the limit price).
- **Risk controls** (`risk.py`): pre-trade kill switch (config flag or
  `PAPERTRADE_INDIA_KILL_SWITCH=1` env var), symbol whitelist, per-order
  notional cap, per-position notional cap, per-position equity-fraction
  cap. Violations raise `RiskViolation` (or `KillSwitchActive`) with
  zero state mutation.
- **Idempotency keys** (`idempotency.py`): `buy()` / `sell()` accept
  an `idempotency_key`. Replays with same params return the prior
  order; mismatched params raise `IdempotencyConflict`. Keys scoped
  per-account, TTL-pruned via `cleanup_idempotency_keys()`.
- **Broker presets** (`presets.py`): named `FeeConfig` instances for
  Zerodha, Upstox, Groww, Angel One, and ICICIdirect (delivery + intraday
  variants where applicable). `get_preset(name)` for case-insensitive
  lookup.
- **Symbol master** (`symbols.py`): track tradeable NSE/BSE symbols
  with optional metadata (name, ISIN, lot size). Always rejects orders
  for delisted symbols; rejects unknown symbols in `strict=True` mode.
  Bundled `nse_universe_sample.csv` covers ~30 of the largest NSE
  companies.
- **Non-goals section** in README — visible at the top of the project
  rather than buried in §7 of the design doc.
- New exceptions: `RiskViolation`, `KillSwitchActive`,
  `IdempotencyConflict`, `SymbolNotFound`, `SymbolDelisted`.

### Changed
- `IndiaPaperBroker.__init__` accepts `slippage_config`, `risk_config`,
  and `symbol_master`. All default to "no-op" so existing callers see
  zero behavior change.
- `IndiaPaperBroker.buy()` / `sell()` accept an optional
  `idempotency_key` parameter.

### Schema additions
- `idempotency_keys` table (per-account, FK CASCADE on account & order).
- `symbols` table for the symbol master.

### Tests
- 67 new tests covering all five new modules. Total: 152 passing,
  3 opt-in E2E skipped.

### Added (test + tooling completeness)
- Property-based tests for the fee engine (Hypothesis): all components
  non-negative, total within paise of sum, BSE ≥ NSE per side, capped
  brokerage respects paise-rounded cap, etc.
- Stateful fuzz: ~1000 random buy/sell ops against a 5-symbol universe
  with prices drifting ±2% per op; asserts the equity invariant
  ``cash + portfolio_value == initial + realized + unrealized`` and
  every order ends in a valid terminal status.
- Round-trip Hypothesis test: any sequence of N buys followed by a
  matched-quantity sell zeroes the position cleanly.
- E2E test suite (`tests/e2e/`) opt-in via `RUN_E2E=1` for real-yfinance
  smoke. Default `pytest` runs skip these.
- Snapshot tests for bundled NSE holiday data (2026 + 2027) plus a
  weekend-detector that fails loudly if a refresh PR adds a Sat/Sun.
- `scripts/update_nse_holidays.py` — converts a hand-curated CSV into
  the bundled JSON, with validation (year matches, no weekends).
- GitHub Actions: `test.yml` (lint + pytest on Python 3.10/3.11/3.12)
  and `publish.yml` (PyPI Trusted Publishing on tag push).
- `docs/ARCHITECTURE.md`, `docs/FEES.md`, `docs/CONTRIBUTING.md`.

### Fixed (caught by the new property tests)
- Bundled holiday data contained dates that fall on weekends
  (`2026-08-15`, several 2027 placeholders). Removed — weekends are
  already non-trading days, and `scripts/update_nse_holidays.py` now
  rejects them at validation time.

### Fixed (correctness)
- **Buy-side fees now flow into realized P&L.** `avg_cost` includes prorated
  buy fees (i.e. `qty * avg_cost == total cash spent acquiring the position`).
  Realized P&L on a sell — `(price - avg_cost) * qty - sell_fees` — therefore
  captures *both* sides of fees over a round-trip. Previously the buy-side
  fees affected cash but were absent from `realized_pl`, leading the agent
  to systematically over-state P&L by ~₹6–₹15 per round trip.
- **Limit-order cancel-vs-fill race.** `_execute_limit_fill` now claims the
  order via `UPDATE ... WHERE status='pending'` before applying cash and
  position changes; if the order moved out of PENDING (e.g. user cancelled),
  `OrderNoLongerPending` is raised and the whole transaction rolls back.
  Previously the watcher could clobber a CANCELLED row back to FILLED and
  open an unwanted position.
- `cancel_order` is similarly race-safe via `WHERE status='pending'`.

### Added
- `Position.current_price_stale: bool` — flips to `True` when the price
  feed couldn't produce a fresh quote and the broker fell back to
  `avg_cost`. Lets agents distinguish a real break-even from a stale
  valuation.
- `IndiaPaperBroker.expire_stale_day_orders()` — sweeps all PENDING DAY-tif
  limit orders to EXPIRED in one transaction. Call from a session-close hook.
- `IndiaPaperBroker(strict_open=True)` — refuse to auto-create a missing
  account; raise `AccountNotFoundError` instead. Used by CLI inspection
  commands.
- `papertrade-india expire-day-orders` and `create-account` CLI commands.
- `Order.expired_at` field and EXPIRED status in the DB schema CHECK list.
- New exceptions: `AccountNotFoundError`, `OrderNoLongerPending`.

### Changed
- `IndiaPaperBroker.get_position(symbol)` is now O(1) — direct primary-key
  lookup instead of scanning all positions.
- `get_account()` reads cash, realized_pl_total, and pending-buy notional
  inside one read context for a tighter snapshot.
- CLI inspection commands (`account`, `positions`, `orders`, `cancel-all`,
  `reset`, `expire-day-orders`) use `strict_open=True` and exit with code 2
  on a missing account instead of silently creating a fresh ₹1M account.
- Fill log message no longer prints `realized P&L ₹0.00` for buys.

### Schema
- `account.cash` gains `CHECK(cash >= 0)`.
- `orders.status` gains a CHECK constraint matching the `OrderStatus` enum.
- `orders.expired_at` column added.
- `trades.order_id` foreign key gains `ON DELETE CASCADE` (simplifies `reset()`).

### Initial implementation
- Initial implementation extracted from `hedge-fund-agent` per
  `docs/India_Paper_Trading_Design.md` v2.0.
- `IndiaPaperBroker` — drop-in replacement for Alpaca's `TradingService`
  with the same method shapes.
- `BrokerInterface` ABC for swappable brokers.
- Domain models: `Position`, `Order`, `Account`, `Trade`.
- `IndianFeeEngine` with realistic NSE/BSE fee schedule
  (brokerage, STT, exchange, GST, SEBI, stamp duty, DP charges).
- `NSECalendar` with bundled holiday data for 2026 and 2027.
- `PriceFeed` with three-layer fallback chain (yfinance →
  jugaad-data → cached last-known).
- Thread-safe SQLite persistence with WAL mode and atomic transactions.
- `LimitOrderWatcher` background loop for limit-order fills.
- Multi-account support via `account_id`.
- CLI for inspecting account state.
- MCP server example for direct LLM-agent integration.

[Unreleased]: https://github.com/your-org/papertrade-india/compare/HEAD...HEAD
