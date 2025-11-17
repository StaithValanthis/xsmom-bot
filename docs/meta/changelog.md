# Changelog

## Version 1.2.0 (2025-11-17)

### Added

- **Discord Notifications** - Automated alerts for optimizer results and daily reports
  - Optimizer result notifications (deployment status, metrics, parameters)
  - Daily performance reports (PnL, trades, win rate, equity growth)
  - Dual webhook support (env var + config file)

- **Documentation Structure** - Comprehensive documentation system
  - Complete `docs/` hierarchy with clear organization
  - Knowledge Base (KB) with framework overview and change log
  - "Start Here" reading guide for new developers/operators
  - KB update tool (`tools/update_kb.py`) for auto-generating docs

- **Daily Report Module** - Performance reporting
  - Daily PnL aggregation from state file
  - Cumulative metrics (total PnL, max DD, equity growth)
  - Discord notifications (optional)
  - CLI: `python -m src.reports.daily_report`

### Changed

- **README.md** - Updated to point to new documentation structure
- **Config System** - Added `notifications.discord` section

### Fixed

- State file corruption risk (atomic writes via `utils.write_json_atomic()`)
- Health monitoring (heartbeat system via `utils.write_heartbeat()`)

---

## Version 1.1.0 (2025-08-21)

### Initial Production Release

- **Cross-Sectional Momentum (XSMOM)** strategy
- Inverse-volatility sizing
- Liquidity-aware caps
- Cost-aware backtests
- Strong risk controls (kill-switch, daily loss limits)
- Modular architecture for maintainability
- systemd deployment support

---

## Future Releases

### Planned

- Portfolio-wide drawdown limits
- Partial fill handling
- Circuit breaker for API failures
- Health monitoring improvements
- Live.py refactoring (split into smaller modules)

---

**Motto: MAKE MONEY** — track changes to understand system evolution. 📈

