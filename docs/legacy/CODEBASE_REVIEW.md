# XSMOM-BOT Codebase Review & Analysis
**Date:** 2025-01-XX  
**Focus:** MAKE MONEY — Robust, Risk-Adjusted, Fully Automated Trading System

---

## 1. REPO MAP & WORKFLOW OVERVIEW

### Directory Structure

```
xsmom-bot/
├── src/                    # Core Python modules
│   ├── main.py            # Entry point (CLI: live/backtest modes)
│   ├── config.py          # Pydantic config schema (357 lines, well-structured)
│   ├── live.py            # Main live trading loop (~2200 lines, complex)
│   ├── backtester.py      # Cost-aware backtesting engine
│   ├── signals.py         # Signal generation (momentum, regime, ADX, meta-labeler)
│   ├── sizing.py          # Position sizing (inverse-vol, Kelly, caps)
│   ├── risk.py            # Risk management (kill-switch, drawdown tracking)
│   ├── exchange.py        # CCXT wrapper for Bybit (clean abstraction)
│   ├── optimizer*.py      # Multiple optimizer variants (grid, Bayes, purged WF)
│   ├── regime_router.py   # TSMOM/XSMOM regime switching logic
│   ├── carry.py           # Funding/basis carry sleeve (delta-neutral)
│   ├── anti_churn.py      # Trade throttling / cooldown logic
│   └── utils.py           # JSON I/O, logging setup
├── config/
│   ├── config.yaml.example    # Comprehensive example (454 lines)
│   └── optimizer.grid.yaml    # Grid search config
├── bin/
│   ├── run-optimizer.sh   # Optimizer orchestration script
│   └── pnl_ingest.py      # PnL ingestion for optimizer heuristics
├── systemd/               # Production deployment
│   ├── xsmom-bot.service  # Main trading bot service
│   ├── xsmom-optimizer.service  # Daily optimizer
│   └── xsmom-optimizer.timer    # Scheduled at 00:20 UTC
└── state/                 # Runtime state (positions, cooldowns, stats)
```

### Main Workflows

#### **1. Live Trading Loop** (`src/live.py::run_live()`)

**Flow:**
```
Startup → Exchange Connection → State Load → Position Reconcile → FastSLTPThread (background)
    ↓
Main Loop (every `rebalance_minute`):
  1. Fetch equity, positions, tickers
  2. Fetch OHLCV for universe (filtered by volume/price)
  3. Apply regime filters (EMA slope, majors trend)
  4. Compute signals (cross-sectional momentum with multi-lookback)
  5. Apply filters: ADX, symbol scoring, time-of-day whitelist, MTF confirmation
  6. Build targets via `sizing.build_targets()`:
     - Cross-sectional z-scores with `signal_power` exponentiation
     - Dynamic K selection (top K longs, bottom K shorts)
     - Inverse-volatility sizing
     - Market-neutral centering (optional)
     - Per-asset caps, notional caps, ADV caps
     - Portfolio vol-target scaling
     - Kelly-style conviction scaling (optional)
  7. Apply liquidity caps, symbol filters (bans/downweights)
  8. Combine with carry sleeve (if enabled)
  9. Reconcile orders (cancel stale, place new limit orders)
  10. Update state JSON (positions, stats, cooldowns)
  11. Check kill-switches (daily loss, drawdown)
```

**Key Functions:**
- `run_live()` — main orchestrator
- `FastSLTPThread.run()` — fast stop/TP checker (every 2s, 5m timeframe)
- `_reconcile_positions_on_start()` — crash recovery (reloads positions, reconstructs stop state)
- `_reconcile_open_orders()` — order maintenance (stale cleanup, reprice)
- `build_targets()` / `build_targets_auto()` — sizing engine

**FastSLTPThread Responsibilities:**
- ATR-based stops (initial + trailing)
- MA-ATR trailing stop (optional, newer)
- Breakeven moves
- Partial profit-taking ladders
- Regime flip exits
- No-progress exits (time-based + RR threshold)
- Catastrophic stops (3.5×ATR)

#### **2. Backtesting Engine** (`src/backtester.py::run_backtest()`)

**Flow:**
```
1. Fetch OHLCV for symbols
2. Warmup period (max(lookbacks) + vol_lookback + 5 bars)
3. For each bar:
   a. Compute targets via `sizing.build_targets()`
   b. Portfolio return = (targets × returns).sum()
   c. Turnover cost = fees + slippage (maker/taker mix)
   d. Funding cost (if enabled)
   e. Equity += pnl + costs + funding
4. Compute stats: total_return, annualized, Sharpe, Calmar, max_drawdown
```

**Features:**
- Cost-aware (maker/taker fees, slippage, funding)
- Turnover tracking (for optimizer objective)
- Supports prefetched bars (optimizer optimization)

#### **3. Optimizer / Parameter Search**

**Multiple Optimizers:**

1. **`src/optimizer_runner.py`** — Main production optimizer
   - Phase 1: Grid search on K, kappa, vol_lookback, leverage
   - Phase 2: Individual sweeps (signal_power, entry_z, vol_target, etc.)
   - Phase 2 Extra: PnL-informed blacklisting, time-of-day sweeps
   - Writes best config to disk (with backup)
   - Objective: Sharpe (or Calmar) with turnover penalty

2. **`src/optimizer.py`** — Legacy simple grid
   - Hardcoded parameter grid (regime_bps, K, leverage, entry_z, vol_target, diversify)
   - Ranks by Calmar → Sharpe → Annualized

3. **`src/optimizer_cli.py`** — Grid-based CLI
   - Uses `optimizer.grid.yaml` for staged parameter exploration
   - Supports param change budgets (limits per-run changes)
   - Optional service restart after write

4. **`src/optimizer_bayes.py`** — Bayesian optimization (minimal, appears experimental)

5. **`src/optimizer_purged_wf.py`** — Walk-forward with embargo (appears unused in production)

**Automation:**
- `systemd/xsmom-optimizer.timer` runs daily at 00:20 UTC
- `bin/run-optimizer.sh` orchestrates PnL ingestion + optimizer
- Writes improvements to `config/config.yaml` (backed up)
- **Does NOT auto-restart bot** (manual or separate automation needed)

#### **4. Scheduled Tasks**

- **Daily Optimizer** (systemd timer: 00:20 UTC)
- **Meta-Label Training** (systemd timer: `xsmom-meta-trainer.timer`, if enabled)

---

## 2. STRATEGY & LOGIC UNDERSTANDING

### Strategy Type: **Cross-Sectional Momentum (XSMOM) with Regime Switching**

**Core Signal:**
1. **Multi-lookback momentum** (default: [12, 24, 48, 96] hours, weights [0.4, 0.3, 0.2, 0.1])
2. **Cross-sectional z-scores**: Rank assets by weighted returns, normalize to z-score
3. **Nonlinear amplification**: `sign(z) * |z|^signal_power` (default: 1.35)
4. **Top-K selection**: Long top K, short bottom K (dynamic K via dispersion)
5. **Inverse-volatility sizing**: Weights ∝ 1/vol
6. **Market-neutral**: De-mean cross-section (optional)

**Entry/Exit Logic:**

- **Entry filters:**
  - Regime filter (EMA slope on 200h EMA, minimum bps/day)
  - ADX filter (minimum ADX, rising ADX, DI alignment)
  - Symbol scoring (win rate, profit factor, EMA-based bans)
  - Time-of-day whitelist (hourly PnL tracking, boost/downweight)
  - Multi-timeframe confirmation (4h alignment)
  - Breadth gate (minimum fraction of assets passing entry threshold)
  - Majors regime gate (BTC/ETH trend check)
  - Entry z-score minimum (dynamic based on correlation)

- **Exit logic:**
  - Initial stop: `entry_price ± (ATR_mult_sl × ATR)` (default: 2.0×ATR)
  - Trailing stop: MA-ATR style (EMA - k×ATR) or HH/LL style
  - Breakeven: After N×R profit
  - Partial profit ladders (e.g., 80% at 0.8R, 30% size)
  - Regime flip exit (confirm_bars = 1)
  - No-progress exit (after N minutes, if RR < threshold)
  - Max hold time (hours)
  - Catastrophic stop (3.5×ATR)

**Position Sizing:**

- **Base sizing**: Inverse-volatility (normalized to sum = gross_leverage)
- **Caps**:
  - Per-asset: `max_weight_per_asset` (default: 0.20)
  - Per-symbol notional: % of equity OR absolute USDT
  - ADV% cap (optional, via tickers)
- **Scaling**:
  - Portfolio vol-target (realized vol vs target, scale 0.5–2.0×)
  - Kelly-style conviction scaling (optional, base_frac × conviction_rank)
- **Filters**: Symbol bans, downweights, liquidity caps

**Risk Management:**

- **Daily loss limit**: `max_daily_loss_pct` (default: 5.0%, trailing from day high)
- **Soft kill**: Pauses trading, resumes after N minutes
- **Drawdown stepdown**: Adaptive risk scaling (not fully implemented)
- **Anti-churn**: Cooldowns after stops, streak pause after losses

**Additional Features:**

- **Carry sleeve** (`src/carry.py`):
  - Funding carry (delta-neutral via spot/dated futures)
  - Basis carry (dated futures vs spot)
  - Budget split: 20% carry, 80% momentum (configurable)
- **Regime router** (`src/regime_router.py`):
  - Switches between XSMOM and TSMOM based on correlation + majors trend
  - High correlation + trending → TSMOM
  - Low dispersion → TSMOM fallback
- **Meta-labeler** (`src/signals.py::_OnlineMetaLabeler`):
  - Online SGD logistic regression
  - Features: abs_z, sign, vol_look, breakout, funding_bps
  - Filters trades by predicted win probability
  - **NOTE: Defined but NOT wired into live.py main loop**

### Comparison to Best Practices

**✅ STRENGTHS:**
- Cost-aware backtesting (fees, slippage, funding)
- Proper cross-sectional normalization (z-scores)
- Inverse-volatility sizing (risk parity style)
- Multiple exit rules (stops, trailing, profit ladders)
- Position caps (per-asset, per-symbol, ADV%)
- Regime awareness (filters out choppy markets)
- Daily loss limits (kill-switch)
- Crash recovery (reconciles positions on startup)

**❌ GAPS / ISSUES:**

1. **Overfitting Risk**: Too many fine-tuned parameters (see Section 3)
2. **Missing Portfolio-Level Risk**:
   - No max portfolio-wide drawdown limit (only daily loss)
   - No correlation-based position limits (beyond simple diversify filter)
   - No VaR/CVaR limits
3. **Execution Slippage Model**: Assumes fixed bps (no dynamic based on order size / liquidity)
4. **Funding Cost**: Only applied in backtest, not tracked in live (no P&L attribution)
5. **Meta-Labeler**: Code exists but not integrated into live loop
6. **No Position-Level Attribution**: Can't easily answer "which symbols contributed most to PnL?"
7. **Limited Regime Switching**: Only TSMOM/XSMOM, no volatility regime or correlation regime switching
8. **Optimizer Not Fully Closed-Loop**:
   - Writes config but doesn't restart bot
   - No staged rollout (paper → small → full)
   - No rollback on live performance degradation
9. **State Persistence**: JSON-based (not transactional; risk of corruption on crash mid-write)

---

## 3. CONFIG & PARAMETERS (INCLUDING OVERFITTING RISK)

### Parameter Inventory

**STRATEGY CORE (Essential):**
- `signal_power` (1.35) — Nonlinear amplification of z-scores
- `lookbacks` ([12,24,48,96]) — Momentum lookback periods
- `lookback_weights` ([0.4,0.3,0.2,0.1]) — Weights for lookbacks
- `vol_lookback` (96) — For inverse-vol sizing
- `k_min`, `k_max` (2, 4) — Top-K selection bounds
- `gross_leverage` (1.5) — Portfolio gross exposure
- `max_weight_per_asset` (0.20) — Per-asset cap
- `market_neutral` (true) — De-mean cross-section

**RISK (Essential):**
- `atr_mult_sl` (2.0) — Stop loss multiplier
- `atr_len` (28) — ATR period
- `trailing_enabled` (false) — Enable trailing stops
- `trail_atr_mult` (0.0) — Trailing stop multiplier
- `max_daily_loss_pct` (5.0) — Daily loss limit
- `breakeven_after_r` (0.0) — Move stop to breakeven after N×R

**FILTERS (Conditional — High Overfitting Risk):**
- `regime_filter.enabled` (false) — EMA slope filter
- `regime_filter.ema_len` (200) — EMA period
- `regime_filter.slope_min_bps_per_day` (0.0) — Minimum slope threshold
- `adx_filter.enabled` (false) — ADX filter
- `adx_filter.min_adx` (20.0) — Minimum ADX
- `adx_filter.require_rising` (false) — Require rising ADX
- `adx_filter.use_di_alignment` (true) — DI+ > DI- for longs
- `adx_filter.min_di_separation` (0.0) — Minimum DI separation
- `adx_filter.di_hysteresis_bps` (0.0) — Hysteresis for DI flip
- `symbol_filter.score.*` — Many thresholds (win_rate, pf, pnl_block, ban_minutes, etc.)
- `time_of_day_whitelist.*` — Hourly tracking, thresholds, boost factors
- `confirmation.*` — Multi-timeframe confirmation bars, z_boost
- `majors_regime.*` — BTC/ETH trend gate
- `dynamic_entry_band.*` — Correlation-based entry threshold adjustment

**SIZING EXTRAS (Conditional):**
- `vol_target.*` — Portfolio vol targeting (target, lookback, min/max scale)
- `kelly.*` — Kelly-style conviction scaling
- `funding_tilt.*` — Funding rate tilt
- `funding_trim.*` — Funding rate trim (down-weight adverse funding)
- `diversify.*` — Correlation-based diversification

**EXECUTION (Tuning — Low Overfitting Risk):**
- `order_type` (limit)
- `post_only` (true)
- `price_offset_bps` (0.0)
- `spread_guard.*` — Max spread check
- `dynamic_offset.*` — Dynamic limit offset based on spread
- `microstructure.*` — Order book imbalance check
- `stale_orders.*` — Stale order cleanup

### Overfitting Risk Analysis

**🔴 HIGH RISK (Too Many Degrees of Freedom):**

1. **Symbol Scoring** (`symbol_filter.score.*`):
   - `min_sample_trades`, `ema_alpha`, `min_win_rate_pct`, `pf_downweight_threshold`, `downweight_factor`, `block_below_win_rate_pct`, `pf_block_threshold`, `pnl_block_threshold_usdt_per_trade`, `ban_minutes`, `grace_trades_after_unban`, `decay_days`, `pf_warn_threshold`
   - **Issue**: 12+ parameters controlling a single filter. Likely overfit to historical symbol performance.
   - **Recommendation**: Simplify to 3–4 parameters: min_trades, win_rate_threshold, pf_threshold, ban_hours.

2. **Time-of-Day Whitelist** (`time_of_day_whitelist.*`):
   - `use_ema`, `ema_alpha`, `min_trades_per_hour`, `min_hours_allowed`, `threshold_bps`, `fixed_hours`, `downweight_factor`, `boost_good_hours`, `boost_factor`, `fixed_good_hours`, `require_consecutive_good_hours`, `blackout_hours_utc`
   - **Issue**: Complex hourly tracking with multiple thresholds. High risk of overfitting to specific hours.
   - **Recommendation**: Remove or simplify to fixed blackout hours only.

3. **Dynamic Entry Band** (`dynamic_entry_band.*`):
   - Correlation-based entry threshold adjustment with 3 tiers (high/mid/low corr, each with threshold and zmin)
   - **Issue**: Adds complexity without clear economic rationale (why should entry threshold depend on correlation?).
   - **Recommendation**: Remove; use single `entry_zscore_min`.

4. **ADX Filter** (`adx_filter.*`):
   - 6 parameters (len, min_adx, require_rising, use_di_alignment, min_di_separation, di_hysteresis_bps)
   - **Issue**: Many fine-tuned thresholds. ADX is a lagging indicator; likely overfit.
   - **Recommendation**: Simplify to 2 parameters: `enabled`, `min_adx` (remove DI logic, hysteresis).

5. **Confirmation Gates** (`confirmation.*`, `confirmation_timeframe`, `confirmation_lookback`, `require_mtf_alignment`):
   - Multi-timeframe confirmation with z_boost, lookback_bars, alignment requirement
   - **Issue**: Adds latency; unclear if it adds edge vs. noise.
   - **Recommendation**: Test with/without in walk-forward; remove if no consistent benefit.

**🟡 MEDIUM RISK (Useful but Keep Simple):**

- **Vol Target**: Portfolio vol targeting is economically sound, but `target_daily_vol_bps`, `lookback_hours`, `min_scale`, `max_scale` can be simplified (fix lookback, reduce scale range).
- **Kelly Scaling**: Conviction-based sizing is reasonable, but `base_frac`, `min_scale`, `max_scale` overlap with vol target (choose one).
- **Funding Trim**: Funding cost awareness is good, but `threshold_bps`, `slope_per_bps`, `max_reduction` can be simplified.

**🟢 LOW RISK (Economically Sound):**

- Core signal parameters (lookbacks, weights, signal_power)
- Risk parameters (atr_mult_sl, max_daily_loss_pct)
- Execution parameters (spread_guard, dynamic_offset)

### Simplified Parameter Set (Proposed)

**CORE:**
```yaml
strategy:
  signal_power: 1.35
  lookbacks: [12, 24, 48, 96]
  lookback_weights: [0.4, 0.3, 0.2, 0.1]
  vol_lookback: 96
  k_min: 2
  k_max: 4
  gross_leverage: 1.5
  max_weight_per_asset: 0.20
  market_neutral: true
  entry_zscore_min: 0.0  # Single threshold

  # Simplified filters (keep only economically sound)
  regime_filter:
    enabled: true
    ema_len: 200
    slope_min_bps_per_day: 2.0
  symbol_filter:
    enabled: true
    whitelist: []
    banlist: []
    score:
      enabled: true
      min_trades: 8
      win_rate_threshold: 0.40  # Simplified
      pf_threshold: 1.2
      ban_hours: 24
  portfolio_vol_target:
    enabled: true
    target_ann_vol: 0.30
    lookback_hours: 72
    min_scale: 0.6
    max_scale: 1.4
```

**REMOVED (Overfit Risk):**
- `time_of_day_whitelist.*` (all)
- `dynamic_entry_band.*` (all)
- `adx_filter.use_di_alignment`, `di_hysteresis_bps`, `min_di_separation`
- `confirmation.*` (test in walk-forward first)
- `kelly.*` (redundant with vol target)
- `funding_trim.*` (keep only `funding_tilt` if needed)

---

## 4. UNUSED / LEGACY / DEAD CODE

### Unused Modules / Functions

1. **`src/meta_label_trainer.py`**
   - **Status**: Has systemd service (`xsmom-meta-trainer.service`) but meta-labeler code in `signals.py` is NOT wired into `live.py`.
   - **Recommendation**: Either wire it in or remove the systemd service + trainer code.

2. **`src/optimizer_bayes.py`**
   - **Status**: Bayesian optimization implementation appears experimental / incomplete.
   - **Recommendation**: Remove if not actively used, or complete and document.

3. **`src/optimizer_purged_wf.py`**
   - **Status**: Walk-forward optimizer with embargo. Not called by `optimizer_runner.py` or systemd.
   - **Recommendation**: Archive to `/legacy` or integrate if desired.

4. **`src/optimizer.py`** (Legacy simple grid)
   - **Status**: Hardcoded grid, superseded by `optimizer_runner.py`.
   - **Recommendation**: Remove (keep only `optimizer_runner.py` and `optimizer_cli.py`).

5. **`src/auto_opt.py`**
   - **Status**: Timeframe + regime sweep. Not called by systemd or main optimizer.
   - **Recommendation**: Archive or integrate into `optimizer_runner.py`.

### Unused Config Parameters

**Never Referenced in Code:**
- `strategy.profit_lock_steps` (legacy, replaced by `risk.profit_lock`)
- `risk.breakeven_extra_bps` (legacy)
- `risk.age_tighten` (legacy, replaced by `risk.no_progress`)
- `execution.child_order_ttl_ms` (not used)
- `execution.maker_ttl_secs` (not used, `stale_orders.max_age_sec` used instead)
- `exchange.recv_window_ms` (not used by CCXT wrapper)
- `exchange.include_symbols` / `exclude_symbols` (not used, `symbol_filter.whitelist/banlist` used)

**Partially Used:**
- `strategy.require_mtf_alignment` — Referenced but MTF confirmation logic incomplete
- `risk.use_trailing_killswitch` — Referenced but implementation unclear

### Dead Code Paths

- **`src/live.py`**: Legacy `build_targets()` call path (line ~1433) — should use `build_targets_auto()` or regime router consistently.
- **`src/config.py`**: Many `getattr()` fallbacks for legacy parameter names (backwards compat, but adds complexity).

### Recommendations

**SAFE TO DELETE:**
- `src/optimizer.py` (legacy)
- Unused config parameters (after validation)

**ARCHIVE TO `/legacy`:**
- `src/optimizer_bayes.py`
- `src/optimizer_purged_wf.py`
- `src/auto_opt.py`

**KEEP BUT REFACTOR:**
- `src/meta_label_trainer.py` — Wire meta-labeler into live loop or remove
- Legacy parameter fallbacks in `config.py` — Document deprecation, remove in next major version

---

## 5. CODE QUALITY, ARCHITECTURE & BEST PRACTICES

### Modularity: **✅ GOOD**

**Separation of Concerns:**
- ✅ Data (`exchange.py`) — Clean CCXT abstraction
- ✅ Signals (`signals.py`) — Signal generation isolated
- ✅ Sizing (`sizing.py`) — Position sizing isolated
- ✅ Risk (`risk.py`) — Risk management isolated
- ✅ Execution (`live.py`) — Order management in main loop
- ✅ State (`utils.py::read_json/write_json`) — Simple JSON persistence

**Issues:**
- ⚠️ `live.py` is too large (~2200 lines). Should split:
  - `live_execution.py` — Order placement, reconciliation
  - `live_loop.py` — Main loop orchestration
  - `live_risk.py` — Kill-switch, daily loss tracking
- ⚠️ `signals.py` mixes multiple concerns (momentum, ADX, meta-labeler, funding trim). Consider splitting.

### Testability: **❌ POOR**

**Current State:**
- `tests/test_signals.py` exists but minimal
- No integration tests
- Hard to unit test `live.py` (tightly coupled to exchange, state file)

**Recommendations:**
- Extract exchange interface (dependency injection)
- Mock exchange in tests
- Unit test signal generation, sizing, risk logic independently
- Integration test backtester with synthetic data

### Error Handling & Resilience: **🟡 MODERATE**

**Strengths:**
- ✅ Retry logic via `tenacity` in `exchange.py`
- ✅ Try/except around API calls
- ✅ Position reconciliation on startup (crash recovery)
- ✅ State file read with defaults (graceful degradation)

**Weaknesses:**
- ❌ **State file corruption risk**: `write_json()` not atomic (could corrupt on crash mid-write)
- ❌ **No circuit breaker**: Repeated API failures don't pause trading
- ❌ **Silent failures**: Many `except Exception: pass` blocks (e.g., `live.py::_update_symbol_score_on_close`)
- ❌ **No health checks**: Bot could silently stop trading (no trades for X hours)
- ❌ **Partial fills**: Not explicitly handled (assumes fills are atomic)
- ❌ **Rate limiting**: CCXT handles, but no backoff strategy if hit

**Recommendations:**
1. **Atomic state writes**: Write to temp file, then `os.rename()` (atomic on Linux)
2. **Circuit breaker**: Track API failure rate, pause trading if > threshold
3. **Health monitoring**: Alert if no trades for > 4 hours (or configurable)
4. **Partial fill handling**: Check order status, adjust targets if partially filled
5. **Structured error logging**: Replace `except Exception: pass` with logging

### Performance & Scalability: **✅ GOOD**

**Strengths:**
- ✅ FastSLTPThread runs in background (doesn't block main loop)
- ✅ Efficient pandas operations (vectorized)
- ✅ Caching of OHLCV (60s TTL in FastSLTPThread)
- ✅ Universe filtering reduces API calls

**Potential Bottlenecks:**
- ⚠️ Main loop fetches OHLCV for all symbols every cycle (could batch/cache)
- ⚠️ Symbol scoring recalculates on every trade (could be async)
- ⚠️ No async I/O (all API calls are blocking)

**Recommendations:**
- Batch OHLCV fetches (if exchange supports)
- Async I/O for non-critical operations (stats updates)
- Consider Redis for shared state (if multi-instance)

### Style & Maintainability: **🟡 MODERATE**

**Issues:**

1. **Inconsistent Error Handling:**
   - Mix of `try/except: pass`, `try/except: log.warning()`, `try/except: return default`
   - Should standardize on logging + graceful defaults

2. **Magic Numbers:**
   - Many hardcoded values (e.g., `1e-12`, `10_000.0`, `8760.0`)
   - Should use named constants

3. **Complex Conditionals:**
   - `live.py` has deeply nested if/else for filter chains
   - Consider strategy pattern or filter pipeline

4. **Type Hints:**
   - Inconsistent (some functions fully typed, others not)
   - Should add mypy type checking

5. **Documentation:**
   - Functions lack docstrings
   - Config parameters not documented in code (only in YAML example)

**Recommendations:**
1. Add comprehensive docstrings (Google style)
2. Extract constants (e.g., `BARS_PER_YEAR = 8760.0`)
3. Refactor filter chain into pipeline pattern
4. Enable mypy type checking
5. Add config parameter documentation (docstrings in `config.py`)

### Top Architectural Issues

1. **State Persistence Not Atomic** (HIGH PRIORITY)
   - Risk: Corruption on crash mid-write
   - Fix: Atomic writes (temp file + rename)

2. **No Health Monitoring** (HIGH PRIORITY)
   - Risk: Bot silently stops trading
   - Fix: Heartbeat/health check, alert on no trades

3. **`live.py` Too Large** (MEDIUM PRIORITY)
   - Risk: Hard to maintain, test, debug
   - Fix: Split into modules (execution, loop, risk)

4. **Meta-Labeler Not Integrated** (MEDIUM PRIORITY)
   - Risk: Dead code, confusion
   - Fix: Wire into live loop or remove

5. **Overly Complex Filter Chain** (MEDIUM PRIORITY)
   - Risk: Hard to reason about, overfitting
   - Fix: Simplify parameters (see Section 3), use pipeline pattern

6. **No Partial Fill Handling** (LOW PRIORITY)
   - Risk: Position drift
   - Fix: Check order status, reconcile positions

---

## 6. SELF-IMPROVEMENT & SELF-OPTIMIZATION

### Current State

**✅ WHAT EXISTS:**
- Daily optimizer (`systemd/xsmom-optimizer.timer`)
- Grid search optimizer (`optimizer_runner.py`)
- PnL ingestion for heuristics (`bin/pnl_ingest.py`)
- Config writeback (with backup)
- Symbol scoring (learns from live trades)
- Time-of-day whitelist (learns from hourly PnL)

**❌ WHAT'S MISSING:**
- **Auto-restart bot** after config update
- **Staged rollout** (paper → small size → full)
- **Rollback** on live performance degradation
- **Out-of-sample validation** (optimizer doesn't reserve holdout set)
- **Walk-forward optimization** (optimizer_runner uses single backtest window)
- **Performance attribution** (can't easily see which params/symbols contribute to PnL)
- **Meta-labeler integration** (code exists but not used)

### Proposed Self-Improvement Design

**Phase 1: Closed-Loop Optimization (Current + Auto-Restart)**

```
1. Optimizer runs daily (already done)
2. If improvement > threshold:
   a. Backup current config
   b. Write new config
   c. Restart bot service (add to optimizer_runner.py)
   d. Log config change + metrics
3. Monitor live performance for 24h
4. If live Sharpe < baseline - threshold:
   a. Rollback config (restore backup)
   b. Alert admin
```

**Phase 2: Staged Rollback**

```
1. New config → paper trading mode (dry_run=True)
2. After 48h, if paper Sharpe > baseline:
   → Small size mode (scale_all_weights × 0.25)
3. After 48h, if small size Sharpe > baseline:
   → Full size mode
4. Track live vs backtest Sharpe divergence (overfitting signal)
```

**Phase 3: Walk-Forward + Out-of-Sample**

```
1. Reserve last 20% of data as holdout set
2. Optimize on training set (80%)
3. Validate on holdout set
4. Only accept if holdout Sharpe > threshold
5. Use purged walk-forward (optimizer_purged_wf.py) to reduce overfitting
```

**Phase 4: Meta-Learning**

```
1. Integrate meta-labeler into live loop
2. Train on live trade outcomes (features: z, vol, funding, breakout)
3. Filter trades by predicted win probability
4. Retrain weekly on recent trades
```

**Implementation Priorities:**

1. **Tier 1 (Quick Win):**
   - Add auto-restart to `optimizer_runner.py` (systemd service restart)
   - Add rollback logic (restore backup if live Sharpe < threshold)
   - Add config change logging (CSV of config changes + backtest metrics)

2. **Tier 2 (Medium Effort):**
   - Walk-forward optimizer integration
   - Out-of-sample validation
   - Performance attribution (track per-param contribution)

3. **Tier 3 (Long-Term):**
   - Staged rollout (paper → small → full)
   - Meta-labeler integration
   - Reinforcement learning for parameter selection

---

## 7. 100% AUTOMATION & PRODUCTION READINESS

### Current Automation Status

**✅ AUTOMATED:**
- Bot runs via systemd (`xsmom-bot.service`)
- Optimizer runs daily via systemd timer
- State persistence (JSON)
- Position reconciliation on startup
- Daily loss kill-switch

**❌ REQUIRES MANUAL INTERVENTION:**
- **Config updates**: Optimizer writes config but doesn't restart bot
- **Emergency stops**: No remote kill switch (must SSH + systemctl stop)
- **Symbol list updates**: Universe filtering is automatic, but whitelist/banlist manual
- **Parameter tweaks**: Must edit config.yaml manually
- **Monitoring**: No alerts on failures/no trades

### Missing for True Production Readiness

#### **1. Health Checks & Monitoring** (HIGH PRIORITY)

**Current:** No health checks

**Needed:**
- **Heartbeat file**: Write timestamp to `/opt/xsmom-bot/state/heartbeat.json` every minute
- **Health check endpoint**: Simple HTTP server on localhost:8080/health (or use systemd socket)
- **No-trade detection**: Alert if no trades for > 4 hours (configurable)
- **API failure tracking**: Alert if API failure rate > 10% over 5 minutes
- **Position drift detection**: Alert if live positions diverge from targets by > 20%

**Implementation:**
```python
# src/health.py
def write_heartbeat(state_path: str):
    write_json(f"{state_path}/heartbeat.json", {"ts": utcnow().isoformat()})

def check_health(state_path: str) -> dict:
    hb = read_json(f"{state_path}/heartbeat.json", {})
    last_ts = pd.Timestamp(hb.get("ts"))
    age_sec = (utcnow() - last_ts).total_seconds()
    return {
        "healthy": age_sec < 120,  # 2 min threshold
        "age_sec": age_sec,
        "last_heartbeat": hb.get("ts")
    }
```

#### **2. Alerts** (HIGH PRIORITY)

**Current:** Logs only (no alerts)

**Needed:**
- **Email/SMS on critical events**: Daily loss limit hit, API failures, no trades
- **Prometheus metrics** (optional): Expose metrics for Grafana
- **Discord/Slack webhook** (optional): Real-time alerts

**Implementation:**
- Use `logging.handlers.SMTPHandler` for email
- Or integrate with monitoring service (Datadog, Prometheus)

#### **3. Crash Recovery & Restart Safety** (MEDIUM PRIORITY)

**Current:** Position reconciliation exists, but state file corruption risk

**Needed:**
- **Atomic state writes** (see Section 5)
- **Idempotent startup**: Handle partial state gracefully
- **Open orders cleanup**: Cancel orphaned orders on startup (config flag exists but should be default)
- **Position reconciliation logging**: Log which positions were reloaded

#### **4. Safety Brakes** (MEDIUM PRIORITY)

**Current:** Daily loss limit exists

**Needed:**
- **Max drawdown stop**: Stop trading if portfolio drawdown > X% from 30-day high
- **Symbol-level loss limits**: Stop trading symbol if daily loss > threshold (partially exists in symbol_filter.score)
- **Emergency stop file**: Create `/opt/xsmom-bot/state/EMERGENCY_STOP` to pause trading
- **Config validation**: Validate config on startup (prevent bad configs from breaking bot)

**Implementation:**
```python
# In live.py::run_live()
emergency_stop_path = f"{cfg.paths.state_path}/EMERGENCY_STOP"
if os.path.exists(emergency_stop_path):
    log.error("EMERGENCY_STOP file exists. Trading paused.")
    return

# Check max drawdown
if current_drawdown > cfg.risk.max_portfolio_drawdown_pct:
    log.error(f"Max drawdown {current_drawdown:.2f}% exceeded. Trading paused.")
    return
```

#### **5. Remote Control** (LOW PRIORITY)

**Current:** Must SSH to control bot

**Needed:**
- **REST API** (optional): Start/stop, get status, emergency stop
- **Web dashboard** (optional): View positions, PnL, config
- **Telegram bot** (optional): Simple commands via Telegram

### Prioritized Production Readiness Checklist

**Tier 1 (Critical — Do First):**
1. ✅ Atomic state writes (temp file + rename)
2. ✅ Health checks (heartbeat file, no-trade detection)
3. ✅ Alerts (email on daily loss limit, API failures)
4. ✅ Emergency stop file (pause trading remotely)
5. ✅ Auto-restart bot after optimizer update

**Tier 2 (Important — Do Soon):**
6. ✅ Max drawdown stop (portfolio-level)
7. ✅ Config validation on startup
8. ✅ Position drift alerts
9. ✅ Optimizer rollback on live degradation
10. ✅ Structured error logging (replace silent failures)

**Tier 3 (Nice-to-Have):**
11. REST API for remote control
12. Prometheus metrics
13. Web dashboard
14. Telegram bot

---

## 8. PRIORITIZED ROADMAP (MAKE MONEY FOCUSED)

### Tier 1 – Quick Wins (High Impact, Low Effort)

#### **1. Fix State File Corruption Risk** (1–2 hours)
- **Why:** Prevents data loss on crash
- **Impact:** High (data integrity)
- **Area:** `src/utils.py::write_json()`
- **Fix:** Write to temp file, then `os.rename()` (atomic on Linux)

#### **2. Add Health Checks** (2–3 hours)
- **Why:** Detects silent failures (bot stops trading)
- **Impact:** High (reliability)
- **Area:** `src/live.py` (write heartbeat), new `src/health.py`
- **Fix:** Write heartbeat every minute, alert if stale

#### **3. Simplify Config Parameters** (4–6 hours)
- **Why:** Reduces overfitting risk, improves robustness
- **Impact:** High (long-term profitability)
- **Area:** `src/config.py`, `config/config.yaml.example`
- **Fix:** Remove time_of_day_whitelist, dynamic_entry_band, simplify ADX, symbol scoring (see Section 3)

#### **4. Auto-Restart Bot After Optimizer** (1 hour)
- **Why:** Closes optimization loop (currently manual)
- **Impact:** Medium (automation)
- **Area:** `src/optimizer_runner.py::optimize()`
- **Fix:** After writing config, run `systemctl restart xsmom-bot` (with error handling)

#### **5. Add Emergency Stop File** (30 minutes)
- **Why:** Allows remote pause without SSH
- **Impact:** Medium (operational safety)
- **Area:** `src/live.py::run_live()` (startup check)
- **Fix:** Check for `state/EMERGENCY_STOP` file, exit if exists

### Tier 2 – Medium Projects (Higher Impact, More Effort)

#### **6. Add Max Drawdown Stop** (2–3 hours)
- **Why:** Protects capital during extended drawdowns
- **Impact:** High (risk management)
- **Area:** `src/live.py` (main loop), `src/risk.py`
- **Fix:** Track 30-day high equity, stop if drawdown > threshold (e.g., 15%)

#### **7. Walk-Forward Optimizer Integration** (1–2 days)
- **Why:** Reduces overfitting, more robust parameter selection
- **Impact:** High (long-term profitability)
- **Area:** `src/optimizer_runner.py`, integrate `optimizer_purged_wf.py`
- **Fix:** Use walk-forward with embargo instead of single backtest window

#### **8. Add Alerts** (4–6 hours)
- **Why:** Immediate notification of critical events
- **Impact:** Medium (operational awareness)
- **Area:** New `src/alerts.py`, integrate into `live.py`
- **Fix:** Email/SMS on daily loss limit, API failures, no trades

#### **9. Wire Meta-Labeler into Live Loop** (1 day)
- **Why:** Code exists but unused; could filter bad trades
- **Impact:** Medium (if meta-labeler adds edge)
- **Area:** `src/live.py` (after signal generation), `src/signals.py::_filter_by_meta()`
- **Fix:** Call meta-labeler after signal generation, filter trades below threshold

#### **10. Refactor `live.py` into Modules** (2–3 days)
- **Why:** Improves maintainability, testability
- **Impact:** Medium (long-term development velocity)
- **Area:** Split `live.py` into `live_execution.py`, `live_loop.py`, `live_risk.py`
- **Fix:** Extract execution logic, main loop, risk tracking into separate modules

#### **11. Add Performance Attribution** (1–2 days)
- **Why:** Understand which symbols/params contribute to PnL
- **Impact:** Medium (data-driven improvements)
- **Area:** New `src/attribution.py`, integrate into `live.py`
- **Fix:** Track per-symbol PnL, per-param contribution (via backtest ablation)

#### **12. Optimizer Rollback on Live Degradation** (1 day)
- **Why:** Prevents bad configs from hurting live performance
- **Impact:** High (risk management)
- **Area:** `src/optimizer_runner.py`, add monitoring + rollback logic
- **Fix:** Track live Sharpe vs backtest Sharpe, rollback if divergence > threshold

### Tier 3 – Long-Term / Nice-to-Have

#### **13. Staged Rollback (Paper → Small → Full)** (1 week)
- **Why:** Safer config deployment
- **Impact:** Medium (risk reduction)
- **Area:** New deployment pipeline, config versioning
- **Fix:** Paper trading mode → 25% size → 100% size, with validation gates

#### **14. Async I/O for Non-Critical Operations** (1 week)
- **Why:** Better performance, non-blocking stats updates
- **Impact:** Low (performance optimization)
- **Area:** `src/live.py` (use asyncio for stats, not trading)
- **Fix:** Async stats updates, keep trading loop synchronous for simplicity

#### **15. REST API for Remote Control** (1 week)
- **Why:** Remote management without SSH
- **Impact:** Low (operational convenience)
- **Area:** New `src/api.py` (Flask/FastAPI)
- **Fix:** Endpoints for start/stop, status, emergency stop, config read

#### **16. Reinforcement Learning for Parameter Selection** (Research Project)
- **Why:** Adaptive parameter selection based on live performance
- **Impact:** Unknown (research needed)
- **Area:** Research project, not implementation ready
- **Fix:** N/A (exploratory)

#### **17. Volatility Regime Switching** (1 week)
- **Why:** Adjust risk/leverage based on market volatility
- **Impact:** Medium (risk-adjusted returns)
- **Area:** `src/regime_router.py`, extend to volatility regimes
- **Fix:** Detect high/low vol regimes, scale leverage/position sizes

#### **18. Correlation-Based Position Limits** (3–4 days)
- **Why:** Reduce concentration risk in correlated assets
- **Impact:** Medium (risk management)
- **Area:** `src/sizing.py`, extend diversify filter
- **Fix:** Cap total exposure to correlated clusters (beyond simple pair-wise corr)

---

## SUMMARY: TOP 5 ACTION ITEMS

1. **Fix state file corruption** (atomic writes) — **1 hour, HIGH IMPACT**
2. **Simplify config parameters** (remove overfitting risk) — **4–6 hours, HIGH IMPACT**
3. **Add health checks + alerts** — **4–6 hours, HIGH IMPACT**
4. **Walk-forward optimizer integration** — **1–2 days, HIGH IMPACT**
5. **Auto-restart bot after optimizer** — **1 hour, MEDIUM IMPACT**

**Total Quick Win Effort:** ~2 days  
**Expected Impact:** Significantly improved robustness, reduced overfitting risk, better operational awareness

---

*End of Review*

