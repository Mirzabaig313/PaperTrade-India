# Changelog

All notable changes to **papertrade-india** are documented here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Pre-1.0 releases may include breaking changes in MINOR bumps; each one will
be called out here.

## [Unreleased]

### Added
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
