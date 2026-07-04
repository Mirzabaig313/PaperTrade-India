# Contributing

Thanks for taking the time. This package is small enough that you can
read it end-to-end in an afternoon — please do.

## Local setup

```bash
git clone https://github.com/Mirzabaig313/PaperTrade-India
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev,jugaad,cli,mcp]'
```

## Running the suite

```bash
# Unit + integration tests (no network, deterministic)
pytest

# Lint
ruff check src tests examples

# Type-check (advisory; not enforced in CI yet)
mypy src

# E2E tests against real yfinance (opt-in, network)
RUN_E2E=1 pytest -m e2e
```

The default suite is hermetic (no network) and runs in a few seconds. If
you add a test that needs the network or pushes the wall-clock up
noticeably, it probably belongs in `tests/e2e/` (opt-in) instead.

## Coding style

- Python 3.10+ idioms (`X | None`, native generics, `match` where it
  helps). The Ruff config in `pyproject.toml` enforces this.
- Frozen dataclasses for the public API; mutability is internal to the
  persistence layer.
- Treat docstrings as part of the public contract: when you change
  behavior, update the docstring in the same commit.
- Comments explain *why*, not *what*. The code says what.
- No globals, no module-level singletons. Everything injectable through
  the broker constructor.

Specific anti-patterns to avoid:

- Naked `except:` or broad `except Exception:` without a `# noqa: BLE001`
  and a one-line reason. Defensive catches are fine; silent ones aren't.
- Reaching across module boundaries to grab private state. Add a method
  to the owner instead.
- Mocking SQLite or yfinance in tests. Use `Testcontainers`-equivalent
  stubs (the `_StubProvider` pattern, real on-disk SQLite under
  `tmp_path`) so tests exercise the real driver paths.

## Adding a fee component

If your broker charges something the engine doesn't model:

1. Add the field to `FeeConfig` with a sensible default of 0 (so existing
   users see no behavior change).
2. Compute it in `IndianFeeEngine.calculate`.
3. Add it to `FeeBreakdown` and `__str__`.
4. Update `docs/FEES.md` with the formula.
5. Add a unit test pinning a hand-checked example.

## Refreshing the holiday calendar

Trading holidays are fetched live from the exchange-published API
(`UpstoxHolidayProvider`) and cached, so most users never touch this.
The bundled `src/papertrade_india/data/nse_holidays_*.json` files are the
offline fallback. To refresh a fallback year:

1. Extract the equity-segment holidays into a CSV with a single column
   `date` (`YYYY-MM-DD`).
2. Run `python scripts/update_nse_holidays.py 2027 path/to/2027.csv`.
3. The script writes `src/papertrade_india/data/nse_holidays_2027.json`.
4. Open a PR with the JSON diff.

## Adding a new broker adapter

Implement `BrokerInterface` in a new module. Look at `IndiaPaperBroker`
for the shape. Drop-in adapters (Alpaca, IBKR, Zerodha Kite, etc.) live
in their own packages — `papertrade-india` itself stays focused on
NSE/BSE simulation.

## Pull requests

- One concern per PR.
- Include a test that fails on `main` and passes on your branch.
- If you change behavior, update `CHANGELOG.md` under `## [Unreleased]`.
- Keep PR descriptions short: what, why, and any tradeoffs.

## Reporting issues

The best bug reports include:

- A 5-line snippet that reproduces the issue.
- The exception trace (if any), end-to-end.
- What you expected vs. what happened.
- Your `papertrade-india` version (`pip show papertrade-india`) and
  Python version.

For "fees don't match my broker" reports, include a screenshot or
screenshot-of-PDF of the contract note line-by-line. We map each line to
a `FeeBreakdown` field and figure out where the schedule diverges.
