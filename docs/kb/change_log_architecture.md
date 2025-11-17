# Framework Change Log

**Last updated:** 2025-11-17

This document tracks **framework-level changes** over time. This is NOT a git log; it's a human-readable summary of big conceptual changes to the bot's architecture, strategy, risk management, optimizer, and operational model.

---

## 2025-11-17: Documentation Structure Overhaul

**Type:** Infrastructure

**Changes:**
- Created comprehensive documentation structure (`docs/` hierarchy)
- Added Knowledge Base (KB) with framework overview and change log
- Created KB update tool (`tools/update_kb.py`) for auto-generating module maps and config references
- Added "Start Here" reading guide (`docs/start_here.md`)
- Moved existing documentation to structured locations

**Rationale:**
- Improve maintainability and discoverability
- Make it easier for new developers/operators to understand the system
- Enable semi-automated KB updates when framework changes

**Impact:**
- All documentation now under `docs/` with clear hierarchy
- Auto-generated docs (module maps, config refs) can be regenerated easily
- Clear reading paths for different user types (developers, operators)

---

## 2025-11-17: Discord Notifications System

**Type:** Infrastructure / Monitoring

**Changes:**
- Added Discord webhook notification system (`src/notifications/`)
- Integrated notifications into full-cycle optimizer (sends results after each run)
- Added daily performance report module (`src/reports/daily_report.py`)
- Support for dual webhook sources (env var + config file)

**Rationale:**
- Keep operator informed of optimizer results and daily performance
- Enable quick response to issues (alerts on failures)
- Non-blocking (failures don't crash bot or optimizer)

**Impact:**
- Operators receive Discord notifications for optimizer results and daily reports
- Better observability (optimizer decisions, daily PnL, risk metrics)

---

## 2025-11-XX: Full-Cycle Optimizer Implementation

**Type:** Optimizer

**Changes:**
- Implemented walk-forward optimization (WFO) with purged segments
- Integrated Bayesian optimization via Optuna (TPE sampler)
- Added Monte Carlo stress testing for tail risk assessment
- Implemented config versioning and rollback capability
- Added safe deployment with guardrails (improvement thresholds, tail risk checks)

**Rationale:**
- Reduce overfitting (WFO tests parameters on unseen data)
- Efficient parameter search (BO explores promising regions faster than grid search)
- Assess tail risk before deployment (MC stress tests catch catastrophic scenarios)
- Enable rollback if live performance degrades (versioned configs)

**Impact:**
- More robust parameter optimization (validated on out-of-sample data)
- Faster optimization (BO reduces number of backtests needed)
- Safer deployments (tail risk checks prevent catastrophic configs)
- Better recovery (rollback enables quick revert to previous config)

**Files:**
- `src/optimizer/full_cycle.py` - Main orchestrator
- `src/optimizer/walk_forward.py` - WFO implementation
- `src/optimizer/bo_runner.py` - Bayesian optimization
- `src/optimizer/monte_carlo.py` - MC stress testing
- `src/optimizer/config_manager.py` - Config versioning, deployment, rollback

---

## 2025-09-04: Portfolio-Level Risk Scaling (v1.8.0)

**Type:** Strategy / Risk

**Changes:**
- Added portfolio-level scaler: VolTarget × DD Stepdown × Fractional-Kelly
- Re-cap weights AFTER multipliers (per-asset and per-symbol notional)
- Router hardening: whitespace/case tolerant + explicit logging of config mode/raw

**Rationale:**
- Maintain consistent risk level across market regimes (volatility targeting)
- Adapt to drawdown scenarios (step down risk during losses)
- Scale positions by conviction (Kelly-style scaling based on historical performance)

**Impact:**
- More stable returns (volatility targeting maintains consistent risk)
- Better drawdown recovery (step down risk during losses)
- Higher conviction positions get larger sizes (Kelly scaling)

**Files:**
- `src/sizing.py` - Portfolio vol targeting, DD stepdown, Kelly scaling
- `src/live.py` - Router hardening

---

## 2025-08-21: Initial Production Release (v1.1.0)

**Type:** Initial Release

**Changes:**
- Cross-sectional momentum (XSMOM) strategy
- Inverse-volatility sizing
- Liquidity-aware caps
- Cost-aware backtests
- Strong risk controls (kill-switch, daily loss limits)
- Modularized for maintainability
- Deploys via systemd

**Rationale:**
- Production-ready trading bot with robust risk management
- Modular architecture for maintainability
- Automated deployment for 24/7 operation

**Impact:**
- First production release of xsmom-bot
- Automated trading on Bybit USDT-perp futures
- 24/7 unattended operation via systemd

---

## Future Changes (Planned)

### State File Atomic Writes

**Type:** Infrastructure / Reliability

**Status:** Planned

**Changes:**
- Implement atomic writes for state file (temp file + rename)
- Prevent state file corruption on crash mid-write

**Rationale:**
- Current JSON writes are not atomic (risk of corruption on crash)
- Atomic writes prevent corruption (previous state preserved if write fails)

**Impact:**
- Crash-safe state persistence
- No risk of state file corruption

**Files:**
- `src/utils.py` - Already implemented (`write_json_atomic()`)

---

### Health Monitoring

**Type:** Infrastructure / Monitoring

**Status:** Planned

**Changes:**
- Add heartbeat system (writes timestamp to file every cycle)
- Alert if no trades for > 4 hours (or configurable)
- External system can check heartbeat freshness

**Rationale:**
- Bot could silently stop trading (no trades for hours)
- Health checks enable quick detection of issues

**Impact:**
- Better observability (detect silent failures)
- Quicker response to issues (alerts on no trades)

**Files:**
- `src/utils.py` - Already implemented (`write_heartbeat()`, `read_heartbeat()`)

---

### Live.py Refactoring

**Type:** Architecture / Maintainability

**Status:** Planned

**Changes:**
- Split `live.py` (~2200 lines) into smaller modules:
  - `live_execution.py` - Order placement, reconciliation
  - `live_loop.py` - Main loop orchestration
  - `live_risk.py` - Kill-switch, daily loss tracking

**Rationale:**
- Current `live.py` is too large (hard to maintain, test, debug)
- Split into smaller modules improves maintainability

**Impact:**
- Better code organization
- Easier to test and debug
- Improved maintainability

**Files:**
- `src/live.py` - To be split

---

## Adding New Entries

When making **framework-level changes** (architecture, strategy, risk, optimizer, infrastructure), add an entry here:

1. **Date**: Use `YYYY-MM-DD` format
2. **Type**: `Strategy`, `Risk`, `Optimizer`, `Infrastructure`, `Monitoring`, etc.
3. **Changes**: What changed (be specific)
4. **Rationale**: Why the change was made
5. **Impact**: What effect the change has
6. **Files**: Key files affected (if any)

**Example:**
```markdown
## 2025-12-01: New Risk Control

**Type:** Risk

**Changes:**
- Added max portfolio-wide drawdown limit (separate from daily loss limit)
- Config parameter: `risk.max_portfolio_drawdown_pct`

**Rationale:**
- Current system only has daily loss limit (no portfolio-wide DD limit)
- Need portfolio-level risk control for longer-term drawdown scenarios

**Impact:**
- Better risk management (catches longer-term drawdowns)
- Stops trading if portfolio-wide DD exceeds threshold

**Files:**
- `src/risk.py` - Added `max_portfolio_drawdown_pct` check
- `src/config.py` - Added config parameter
```

---

## Related Documentation

- **[`framework_overview.md`](framework_overview.md)** - Complete framework map
- **[`../architecture/high_level_architecture.md`](../architecture/high_level_architecture.md)** - System overview
- **[`../meta/changelog.md`](../meta/changelog.md)** - Release-level changelog

---

**Motto: MAKE MONEY** — track framework changes to understand system evolution. 📈

