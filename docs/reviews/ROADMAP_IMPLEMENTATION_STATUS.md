# Strategy Improvement Roadmap - Implementation Status

**Date:** 2025-01-XX  
**Status:** In Progress  
**Goal:** Track implementation of all roadmap items from `STRATEGY_IMPROVEMENT_ROADMAP.md`

---

## ✅ COMPLETED

### 1. Signal Stack Simplification
- **Status:** ✅ COMPLETE
- **Changes:**
  - ADX filter disabled by default (`AdxFilterCfg.enabled = False`)
  - Meta-labeler disabled by default (`strategy.meta_label.enabled = False`)
  - Majors regime disabled by default (`MajorsRegimeCfg.enabled = False`)
  - Removed from optimizer parameter space (locked at defaults)
- **Files Modified:**
  - `src/config.py`: Updated defaults for ADX, meta-labeler, majors regime
  - `src/optimizer/bo_runner.py`: Removed from parameter space

### 2. Entry/Exit Improvements
- **Status:** ✅ COMPLETE
- **Changes:**
  - **R-multiple profit targets:** Added `ProfitTargetsCfg` and `_r_multiple_profit_targets()` method
  - **Breakeven moves:** Enabled by default (`breakeven_after_r = 0.5`)
  - **Trailing stops:** Enabled by default (`trailing_enabled = True`, `trail_atr_mult = 1.0`)
  - **Time-based exits:** Enabled by default (`max_hours_in_trade = 48`)
- **Files Modified:**
  - `src/config.py`: Added `ProfitTargetsCfg`, updated defaults
  - `src/live.py`: Added `_r_multiple_profit_targets()` method, integrated into FastSLTPThread

### 3. Optimizer Parameter Space Tightening
- **Status:** ✅ COMPLETE
- **Changes:**
  - Reduced from 15 to 11 parameters
  - Narrowed `signal_power`: [1.0, 1.5] (was [1.0, 2.0])
  - Removed `lookbacks[1]` (medium), keep only short and long
  - Removed `regime_filter.ema_len` (locked at 200)
  - Removed `entry_zscore_min` (locked at 0.0)
  - Removed `trail_atr_mult` (locked at 1.0)
  - Removed `max_weight_per_asset` (locked at 0.10)
  - Narrowed `portfolio_vol_target.target_ann_vol`: [0.15, 0.40] (was [0.20, 0.50])
  - Narrowed `gross_leverage`: [0.75, 1.5] (was [1.0, 2.0])
  - Added `vol_lookback`: [48, 144] (NEW)
  - Added `strategy.carry.budget_frac`: [0.0, 0.40] (NEW)
- **Files Modified:**
  - `src/optimizer/bo_runner.py`: Updated `define_parameter_space()`

---

## 🚧 IN PROGRESS

### 4. Risk Improvements
- **Status:** 🚧 PARTIAL
- **Completed:**
  - Config models added for:
    - Fixed risk-per-trade (`risk.sizing_mode`, `risk.risk_per_trade_pct`)
    - Correlation limits (`risk.correlation`)
    - Max position count hard cap (`risk.max_open_positions_hard`)
    - Volatility regime-based leverage (`risk.volatility_regime`)
- **Remaining:**
  - Implement fixed risk-per-trade logic in `src/sizing.py` or `src/live.py`
  - Implement correlation limits in position selection
  - Implement max position count hard cap enforcement
  - Implement volatility regime-based leverage scaling

### 5. Data Improvements
- **Status:** 🚧 PARTIAL
- **Completed:**
  - Config models added for:
    - Historical OHLCV cache (`data.cache`)
    - Data quality validation (`data.validation`)
- **Remaining:**
  - Create `src/data/cache.py` for SQLite OHLCV cache
  - Create `src/data/validator.py` for data quality checks
  - Integrate cache into `src/exchange.py`
  - Integrate validation into fetch pipeline
  - Extend equity history storage (365 days)

### 6. Volatility Breakout Entry Timing
- **Status:** 🚧 PARTIAL
- **Completed:**
  - Config model added (`strategy.volatility_entry`)
- **Remaining:**
  - Implement volatility breakout detection in `src/signals.py`
  - Integrate into entry logic in `src/live.py`

### 7. Carry Sleeve Budget Optimization
- **Status:** 🚧 PARTIAL
- **Completed:**
  - Added `strategy.carry.budget_frac` to optimizer parameter space
  - Config model added
- **Remaining:**
  - Ensure carry budget fraction is used in position sizing/sleeve allocation

### 8. Funding Cost Tracking & Monitoring
- **Status:** 🚧 PARTIAL
- **Completed:**
  - Config model added (`notifications.monitoring.cost_tracking`)
- **Remaining:**
  - Improve funding cost tracking in `src/exchange.py` and `src/live.py`
  - Add funding PnL to Discord notifications
  - Add cost deviation alerts

---

## 📋 PENDING

### 9. Documentation Updates
- **Status:** 📋 PENDING
- **Files to Update:**
  - `docs/architecture/strategy_overview.md`
  - `docs/architecture/risk_management.md`
  - `docs/architecture/data_pipeline.md` (create if needed)
  - `docs/usage/optimizer.md` or `docs/usage/optimizer_service.md`
  - `docs/reference/config_reference.md`
  - `config/config.yaml.example`

---

## 📝 NOTES

1. **Fixed Risk-Per-Trade:** Requires ATR/stop-loss information, which is not available in `build_targets()`. Consider implementing as post-processing step in `live.py` after targets are built.

2. **Correlation Limits:** Need to compute correlation matrix for candidate assets and filter position selection to enforce limits.

3. **Volatility Regime-Based Leverage:** Need to compute volatility metric (ATR-based) and scale gross leverage dynamically.

4. **Data Cache:** SQLite implementation needed with TTL logic and gap-filling.

5. **Data Validation:** Need to implement checks for OHLC consistency, negative volumes, gaps, and spikes.

---

## 🎯 NEXT STEPS

1. Complete risk improvements (fixed risk, correlation limits, max positions, vol regime)
2. Implement data cache and validation
3. Implement volatility breakout entry timing
4. Update all documentation
5. Create verification checklist

---

**Motto: MAKE MONEY** — with robust, risk-managed, data-driven improvements. 📈

