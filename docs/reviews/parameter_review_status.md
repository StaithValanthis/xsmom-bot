# Parameter Review Implementation Status

**Source Document:** [`../legacy/PARAMETER_REVIEW.md`](../legacy/PARAMETER_REVIEW.md)  
**Last Updated:** 2025-01-XX  
**Review Date:** 2025-01-XX

---

## Summary

**Total Items:** 47  
**✅ Done:** 5  
**🟡 Partial:** 0  
**❌ Pending:** 42

This document tracks the implementation status of parameter simplification recommendations from the Parameter Review.

---

## Quick Status Overview

### Completed (✅ Done)

1. ✅ **Remove dynamic entry band** - Removed from config and code
2. ✅ **Simplify symbol scoring** - Reduced from 12 to 4 parameters
3. ✅ **Simplify/remove time-of-day whitelist** - Removed learning logic
4. ✅ **Simplify ADX filter** - Reduced from 7 to 2 parameters (len fixed at 14)
5. ✅ **Remove dead parameters** - Removed ~20+ dead parameters from config.py

### In Progress (🟡 Partial)

**None.** All high-priority simplifications completed.

### Not Started (❌ Pending)

1. ❌ **Remove dead parameters** (~60 params still present)
2. ❌ **Simplify symbol scoring** (12 params → 4 params)
3. ❌ **Remove time-of-day whitelist** (12 params → remove or 1 param)
4. ❌ **Simplify ADX filter** (7 params → 2 params)
5. ❌ **Remove dynamic entry band** (6 params → remove)
6. ❌ **Remove confirmation gates** (6 params → remove or test)
7. ❌ **Consolidate redundant parameters** (7 merges needed)
8. ❌ **Freeze constants** (several params should be fixed)
9. ❌ **Schema reorganization** (proposed new structure not implemented)

---

## Detailed Status by Category

### 1. Dead Parameters (Remove Immediately)

**Status:** 🟡 **PARTIAL** (~20 removed, ~40 remaining)

**Evidence:**
- `config/config.yaml.example` still contains all dead parameters
- Code doesn't reference these parameters

**Dead Parameters List:**

#### Exchange (3 params)
- ❌ `exchange.recv_window_ms` - Not used by CCXT
- ❌ `exchange.include_symbols` - Use `symbol_filter.whitelist` instead
- ❌ `exchange.exclude_symbols` - Use `symbol_filter.banlist` instead

#### Strategy Filters (25+ params)
- ✅ `strategy.soft_win_lock.*` (4 params) - REMOVED from config.py
- ✅ `strategy.confirmation_timeframe` - REMOVED from config.py and live.py
- ✅ `strategy.confirmation_lookback` - REMOVED from config.py and live.py
- ✅ `strategy.require_mtf_alignment` - REMOVED from config.py and live.py (MTF code removed)
- ❌ `strategy.symbol_filter.exclude` - Legacy (still in config)
- ❌ `strategy.trailing.*` (10 params) - Dead, use `risk.trailing_*` (still in config)
- ❌ `strategy.dispersion_gate.threshold` - Not used (still in config)
- ❌ `strategy.sleeve_constraints.meme.sleeve_vol_bps` - Not used (still in config)
- ❌ `strategy.meta_label.*` (7 params) - Defined but not integrated (still in config)
- ❌ `strategy.risk.*` (8 params) - Dead, use `risk.*` (still in config)

#### Execution (18 params)
- ✅ `execution.slippage_bps_guard` - REMOVED from config.py
- ✅ `execution.align_after_funding_minutes` - REMOVED from config.py
- ✅ `execution.funding_hours_utc` - REMOVED from config.py
- ❌ `execution.child_order_ttl_ms` - Not used
- ❌ `execution.cancel_retries_max` - Not used
- ❌ `execution.maker_ttl_secs` - Referenced but unused
- ❌ `execution.offset_mode` - Not used
- ❌ `execution.offset_bps_min` - Not used
- ❌ `execution.offset_bps_max` - Not used
- ❌ `execution.spread_guard_bps_max` - Not used
- ❌ `execution.min_order_notional_usd` - Duplicate
- ❌ `execution.cancel_stale_after_seconds` - Not used
- ❌ `execution.throttle.*` (3 params) - Not used
- ❌ `execution.pyramiding.*` (2 params) - Not used
- ❌ `execution.retry.*` (3 params) - Not used

#### Risk (12 params)
- ✅ `risk.atr_mult_tp` - REMOVED from config.py
- ✅ `risk.use_tp` - REMOVED from config.py
- ✅ `risk.min_close_pnl_pct` - REMOVED from config.py
- ✅ `risk.no_progress.min_close_pnl_pct` - REMOVED from config.py
- ✅ `risk.no_progress.tiers` - REMOVED from config.py
- ✅ `risk.profit_lock_steps` - REMOVED from config.py
- ✅ `risk.breakeven_extra_bps` - REMOVED from config.py
- ✅ `risk.trail_after_partial_mult` - REMOVED from config.py
- ✅ `risk.age_tighten` - REMOVED from config.py
- ❌ `risk.reentry_after_loss_minutes` - Not used (still in config)
- ❌ `risk.profit_lock.*` (3 params) - Not used (still in config)

**Action Required:**
- Remove from `config/config.yaml.example`
- Remove from `src/config.py` Pydantic models (if present)
- Verify code doesn't reference these parameters

**Location:**
- `config/config.yaml.example` - Remove dead parameters
- `src/config.py` - Remove from models

**Impact:** Low risk (unused parameters). Safe to remove.

---

### 2. Overfitting-Prone Parameters (Simplify)

#### ✅ Symbol Scoring Simplification (12 params → 4 params)

**Status:** ✅ **DONE**

**Current Parameters:**
```yaml
strategy.symbol_filter.score:
  min_sample_trades: 4
  ema_alpha: 0.25
  min_win_rate_pct: 50.0
  pf_downweight_threshold: 1.10
  downweight_factor: 0.60
  block_below_win_rate_pct: 0.0
  pf_block_threshold: 1.25
  pnl_block_threshold_usdt_per_trade: -0.015
  ban_minutes: 1440
  grace_trades_after_unban: 1
  decay_days: 14
  pf_warn_threshold: 1.20
  rolling_window_hours: 168
  max_daily_loss_per_symbol_usdt: 0.8
```

**Proposed Simplified:**
```yaml
filters.symbol_filter:
  enabled: true
  min_trades: 8
  win_rate_threshold: 0.40
  pf_threshold: 1.2
  ban_hours: 24
```

**What's Done:**
- Simplified `symbol_filter.score` to 4 parameters: `min_trades`, `win_rate_threshold`, `pf_threshold`, `ban_hours`
- Removed: `ema_alpha`, `pf_downweight_threshold`, `downweight_factor`, `block_below_win_rate_pct`, `pf_block_threshold`, `pnl_block_threshold_usdt_per_trade`, `grace_trades_after_unban`, `decay_days`, `pf_warn_threshold`, `rolling_window_hours`, `max_daily_loss_per_symbol_usdt`
- Renamed: `min_sample_trades` → `min_trades`, `min_win_rate_pct` → `win_rate_threshold` (as fraction), `ban_minutes` → `ban_hours`
- Updated code in `src/live.py::_update_symbol_score_on_close()` and `_apply_symbol_filter_to_targets()`
- Removed EMA smoothing, downweighted status, and complex logic
- Simplified to: ban if `win_rate < threshold OR pf < threshold` after `min_trades`

**Location:**
- `src/config.py` - Updated `SymbolScoreCfg` model (lines 83-97)
- `src/live.py` - Updated scoring logic (lines 395-445, 452-485)

**Impact:** HIGH - Reduces overfitting risk significantly. Simplified logic is more robust.

---

#### ✅ Time-of-Day Whitelist Removal/Simplification (12 params → removed)

**Status:** ✅ **DONE**

**Current Parameters:**
```yaml
strategy.time_of_day_whitelist:
  enabled: true
  use_ema: true
  ema_alpha: 0.20
  min_trades_per_hour: 12
  min_hours_allowed: 5
  threshold_bps: 0.14
  fixed_hours: []
  downweight_factor: 0.58
  boost_good_hours: true
  boost_factor: 1.15
  fixed_good_hours: []
  require_consecutive_good_hours: 3
  blackout_hours_utc: []
  timezone: UTC
```

**Proposed Simplified:**
```yaml
filters.blackout_hours_utc: []  # Simple blackout list, no learning
```

**What's Done:**
- Removed all learning/EMA/boost logic from `src/live.py`
- Removed complex time-of-day whitelist code (lines 1396-1420)
- Kept `TimeOfDayWhitelistCfg` model in config.py for backward compatibility, but logic is removed
- Added comment indicating only `blackout_hours_utc` would be supported if needed in future
- Simplified to: no time-of-day filtering (removed overfitting-prone learning logic)

**Location:**
- `src/live.py` (lines 1389-1394) - Removed time-of-day whitelist logic
- `src/config.py` - `TimeOfDayWhitelistCfg` still exists but unused

**Impact:** HIGH - Removed high overfitting risk. Learning logic was removed.

---

#### ✅ ADX Filter Simplification (7 params → 2 params)

**Status:** ✅ **DONE**

**Current Parameters:**
```yaml
strategy.adx_filter:
  enabled: true
  len: 14
  min_adx: 28.0
  require_rising: true
  use_di_alignment: true
  min_di_separation: 0.0
  di_hysteresis_bps: 0.0
```

**Proposed Simplified:**
```yaml
filters.adx_filter:
  enabled: false  # Disable by default (overfitting risk)
  min_adx: 25.0   # If enabled
```

**What's Done:**
- Removed: `len` (fixed at 14), `require_rising`, `use_di_alignment`, `min_di_separation`, `di_hysteresis_bps`
- Kept only: `enabled`, `min_adx`
- Updated code in `src/live.py` to use fixed `adx_len = 14`
- Simplified `AdxFilterCfg` model in `src/config.py`

**Location:**
- `src/config.py` - Updated `AdxFilterCfg` model (lines 78-81)
- `src/live.py` (lines 1375-1394) - Updated ADX filter logic with fixed len=14

**Impact:** MEDIUM - Reduces overfitting risk. ADX len fixed at 14, DI logic removed.

---

#### ✅ Dynamic Entry Band Removal (6 params → REMOVED)

**Status:** ✅ **DONE**

**Current Parameters:**
```yaml
strategy.dynamic_entry_band:
  enabled: true
  high_corr:
    threshold: 0.80
    zmin: 0.75
  mid_corr:
    threshold: 0.60
    zmin: 0.60
  low_corr:
    threshold: 0.45
    zmin: 0.50
```

**Proposed:** **REMOVE entirely**

**What's Done:**
- Removed dynamic entry band logic from `src/signals.py::_resolve_entry_threshold_from_cfg()`
- Simplified to use single `entry_zscore_min` or `no_trade_bands.z_entry`
- Removed correlation-based dynamic threshold logic
- Updated code to use static entry threshold

**Location:**
- `src/signals.py` - Removed dynamic entry band logic from `_resolve_entry_threshold_from_cfg()`
- `src/live.py` - No longer uses dynamic entry band

**Impact:** HIGH - Removed unclear economic rationale and overfitting risk. Now uses simple static threshold.

---

#### ❌ Confirmation Gates Removal (6 params → REMOVE or TEST)

**Status:** ❌ **PENDING**

**Current Parameters:**
```yaml
strategy.confirmation:
  enabled: true
  lookback_bars: 4
  z_boost: 0.10
strategy.confirmation_timeframe: 4h
strategy.confirmation_lookback: 6
strategy.require_mtf_alignment: true
```

**Proposed:** **REMOVE or TEST in walk-forward**

**What's Needed:**
- **Option 1:** Remove entirely if testing shows no benefit
- **Option 2:** Test in walk-forward first, then remove if no consistent benefit
- Update code in `src/live.py` or `src/signals.py`

**Location:**
- `config/config.yaml.example` - Remove confirmation.*
- `src/config.py` - Remove from models
- Code that uses confirmation logic

**Impact:** MEDIUM - Adds latency, unclear if adds edge vs noise.

---

### 3. Redundant / Overlapping Parameters (Consolidate)

**Status:** ❌ **PENDING** (7 consolidations needed)

#### ❌ K Selection Consolidation

**Current:**
- `strategy.k_min` / `strategy.k_max`
- `strategy.selection.k_min` / `strategy.selection.k_max`

**Fix:** Remove `selection.k_min/k_max`, keep only `strategy.k_min/k_max`

**Location:**
- `config/config.yaml.example` - Remove `strategy.selection.k_min/k_max`
- `src/config.py` - Remove from models
- Code that uses K selection

---

#### ❌ Entry Z-Score Consolidation

**Current:**
- `strategy.entry_zscore_min`
- `strategy.entry_throttle.min_entry_zscore`

**Fix:** Merge to single `filters.entry_zscore_min`, remove `entry_throttle.min_entry_zscore`

**Location:**
- `config/config.yaml.example` - Remove `entry_throttle.min_entry_zscore`
- `src/config.py` - Update models

---

#### ❌ Daily Loss Limit Consolidation

**Current:**
- `risk.max_daily_loss_pct`
- `strategy.soft_kill.soft_daily_loss_pct`

**Fix:** Remove `soft_kill.soft_daily_loss_pct`, use `risk.max_daily_loss_pct` only

**Location:**
- `config/config.yaml.example` - Remove `soft_kill.soft_daily_loss_pct`
- `src/config.py` - Update models
- Code that uses daily loss limit

---

#### ❌ Vol Target Consolidation

**Current:**
- `strategy.vol_target.*`
- `strategy.portfolio_vol_target.*`

**Fix:** Consolidate to `sizing.vol_target.*` only, remove duplicate

**Location:**
- `config/config.yaml.example` - Merge vol_target configs
- `src/config.py` - Update models

---

#### ❌ Notional Caps Consolidation

**Current:**
- `execution.min_notional_per_order_usdt`
- `execution.min_order_notional_usd`

**Fix:** Use single `execution.min_notional_usdt` (standardize naming)

**Location:**
- `config/config.yaml.example` - Standardize naming
- `src/config.py` - Update models

---

#### ❌ Offset Parameters Consolidation

**Current:**
- `execution.price_offset_bps`
- `execution.dynamic_offset.base_bps`

**Fix:** If `dynamic_offset.enabled`, ignore `price_offset_bps`. Use `dynamic_offset.base_bps` only.

**Location:**
- `config/config.yaml.example` - Document precedence
- `src/config.py` - Update models
- Code that uses offset (likely `src/live.py`)

---

#### ❌ Spread Guard Consolidation

**Current:**
- `execution.spread_guard.max_spread_bps`
- `execution.microstructure.max_spread_bps`
- `execution.spread_guard_bps_max`

**Fix:** Consolidate to `execution.spread_guard.max_spread_bps` only

**Location:**
- `config/config.yaml.example` - Remove duplicates
- `src/config.py` - Update models

---

### 4. Parameters to Freeze as Constants

**Status:** ❌ **PENDING**

**Fixed Mathematical Constants:**
- ❌ `strategy.portfolio_vol_target.bars_per_year` (8760.0) - Should be computed from timeframe
- ❌ `strategy.lookback_weights` [0.4, 0.3, 0.2, 0.1] - Freeze at standard momentum weights

**Fixed Operational Values:**
- ❌ `risk.fast_check_seconds` (2) - Fixed for fast checks
- ❌ `risk.stop_timeframe` ("5m") - Fixed for fast checks
- ❌ `execution.order_type` ("limit") - Fixed for cost efficiency
- ❌ `execution.post_only` (true) - Fixed for maker rebates
- ❌ `strategy.kelly.half_kelly` (true) - If always half-Kelly, remove flag

**What's Needed:**
- Mark these as constants (not configurable)
- Remove from config schema or document as fixed
- Update code to use constants instead of config values

**Location:**
- `config/config.yaml.example` - Remove or mark as fixed
- `src/config.py` - Remove from models or mark as fixed
- Code that uses these values - Replace with constants

**Impact:** LOW - Reduces confusion, prevents accidental changes.

---

### 5. Schema Reorganization

**Status:** ❌ **PENDING**

**Current Structure:**
- All strategy params under `strategy.*`
- Risk params under `risk.*`
- Execution params under `execution.*`

**Proposed Structure:**
```yaml
# Risk (safety limits)
risk:
  max_daily_loss_pct: 5.0
  max_portfolio_drawdown_pct: 15.0
  atr_mult_sl: 2.0
  trail_atr_mult: 1.0
  gross_leverage: 1.5
  max_weight_per_asset: 0.20

# Signals (good optimizer knobs)
signals:
  signal_power: 1.35
  lookback_1h: 12
  lookback_2h: 24
  lookback_3h: 48
  k_min: 2
  k_max: 6
  market_neutral: true
  vol_lookback: 96

# Filters (entry filters - keep simple)
filters:
  regime_filter: {...}
  entry_zscore_min: 0.0
  symbol_filter: {...}
  adx_filter: {...}
  blackout_hours_utc: []

# Sizing (position sizing)
sizing:
  vol_target_enabled: true
  target_ann_vol: 0.30
  diversify_enabled: true
  max_pair_corr: 0.75
  carry_enabled: true
  carry_budget_frac: 0.20
```

**What's Needed:**
- Reorganize config schema into logical groups
- Update `src/config.py` Pydantic models
- Update all code that reads config
- Migration script for existing configs

**Location:**
- `config/config.yaml.example` - Reorganize
- `src/config.py` - Reorganize Pydantic models
- All code that uses config - Update field access

**Impact:** MEDIUM - Improves clarity, but requires significant refactoring.

---

### 6. Optimizer Parameter Space Reduction

**Status:** 🟡 **PARTIAL**

**Current State:**
- `src/optimizer/full_cycle.py` defines parameter space
- Still includes many parameters in optimization space

**Proposed Core Optimizable Parameters (18 total):**

**Signals (6 params):**
- `signals.signal_power` [1.0, 2.0]
- `signals.lookback_1h` [6, 24]
- `signals.lookback_2h` [12, 48]
- `signals.lookback_3h` [24, 96]
- `signals.k_min` [2, 4]
- `signals.k_max` [4, 8]

**Filters (3 params):**
- `filters.regime_filter.ema_len` [100, 300]
- `filters.regime_filter.slope_min_bps_per_day` [1.0, 5.0]
- `filters.entry_zscore_min` [0.0, 1.0]

**Risk (5 params - tight ranges):**
- `risk.atr_mult_sl` [1.5, 3.0]
- `risk.trail_atr_mult` [0.5, 1.5]
- `risk.gross_leverage` [1.0, 2.0]
- `risk.max_weight_per_asset` [0.10, 0.30]
- `sizing.target_ann_vol` [0.20, 0.50]

**Enable/Disable Flags (4 params - binary):**
- `filters.regime_filter.enabled` [true, false]
- `filters.adx_filter.enabled` [true, false]
- `sizing.vol_target_enabled` [true, false]
- `sizing.diversify_enabled` [true, false]

**What's Needed:**
- Update `src/optimizer/bo_runner.py::define_parameter_space()` to only include 18 params
- Remove high-risk parameters from optimization space
- Document which params are optimizer-safe vs fixed/manual

**Location:**
- `src/optimizer/bo_runner.py` - Update parameter space definition

**Impact:** HIGH - Reduces overfitting risk, improves optimizer efficiency.

---

## Migration Plan Status

### Step 1: Remove Dead Code

**Status:** ❌ **NOT STARTED**

- ~60 dead parameters still in config
- Need to remove from config.yaml.example and src/config.py

---

### Step 2: Consolidate Redundant Parameters

**Status:** ❌ **NOT STARTED**

- 7 consolidations needed
- Need to update config and code

---

### Step 3: Simplify High-Risk Parameters

**Status:** ❌ **NOT STARTED**

- 5 major simplifications needed:
  1. Symbol scoring (12 → 4 params)
  2. Time-of-day whitelist (12 → 1 param or remove)
  3. ADX filter (7 → 2 params)
  4. Dynamic entry band (6 → remove)
  5. Confirmation gates (6 → remove or test)

---

### Step 4: Reorganize Schema

**Status:** ❌ **NOT STARTED**

- Proposed new structure not implemented
- Requires significant refactoring

---

## Prioritized Remaining Work

### High Priority (Reduces Overfitting Risk)

1. ✅ **Remove Dynamic Entry Band** - COMPLETED
   - Removed 6 parameters
   - Updated code in `src/signals.py`

2. ✅ **Simplify Symbol Scoring** - COMPLETED
   - Reduced from 12 to 4 parameters
   - Updated code in `src/live.py`

3. ✅ **Remove/Simplify Time-of-Day Whitelist** - COMPLETED
   - Removed learning logic (12 params → 0)
   - Updated code in `src/live.py`

4. ✅ **Simplify ADX Filter** - COMPLETED
   - Reduced from 7 to 2 parameters
   - Updated code in `src/live.py`

5. ✅ **Remove Confirmation Gates** - COMPLETED
   - Removed 3 parameters (confirmation_timeframe, confirmation_lookback, require_mtf_alignment)
   - Removed MTF confirmation code from `src/live.py`

### Medium Priority (Improves Clarity)

6. **Consolidate Redundant Parameters** (4-6 hours, MEDIUM IMPACT)
   - 7 consolidations needed
   - Update config and code

7. **Remove Dead Parameters** (4-6 hours, LOW-MEDIUM IMPACT)
   - ~60 parameters to remove
   - Safe to remove (unused)

8. **Freeze Constants** (2-3 hours, LOW IMPACT)
   - Mark fixed values as constants
   - Update code to use constants

### Low Priority (Structural Improvements)

9. **Reorganize Schema** (1-2 weeks, LOW IMPACT)
   - New config structure
   - Requires significant refactoring
   - Migration script for existing configs

10. **Update Optimizer Parameter Space** (1 day, MEDIUM IMPACT)
    - Reduce to 18 core parameters
    - Update `src/optimizer/bo_runner.py`

---

## Implementation Notes

### Safe to Start With

- **Remove dead parameters** - No code changes needed (unused params)
- **Freeze constants** - Low risk, reduces confusion
- **Consolidate redundant params** - Medium effort, improves clarity

### Requires Testing

- **Simplify symbol scoring** - Need to test new logic matches old behavior
- **Remove time-of-day whitelist** - Need to test impact on performance
- **Remove dynamic entry band** - Need to verify single entry_zscore_min works

### Coordination Needed

- **Schema reorganization** - Requires updating all code that reads config
- **Optimizer parameter space** - Coordinate with optimizer team

---

## Expected Impact

### Parameter Count Reduction

**Before:** ~150 parameters  
**After (Proposed):** ~43 parameters (18 optimizable + 25 fixed/manual)  
**Reduction:** 71% (107 parameters removed)

### Overfitting Risk Reduction

**Before:** 40+ high-risk parameters  
**After:** 0 high-risk parameters (all simplified or removed)

### Optimizer Efficiency

**Before:** 100+ parameters in search space  
**After:** 18 core parameters  
**Impact:** Faster convergence, better global optimum

---

## Related Documents

- [`parameter_review_status.md`](parameter_review_status.md) - This document
- [`codebase_review_status.md`](codebase_review_status.md) - Codebase review status
- [`../legacy/PARAMETER_REVIEW.md`](../legacy/PARAMETER_REVIEW.md) - Original review
- [`../reference/config_reference.md`](../reference/config_reference.md) - Config reference

---

**Last Updated:** 2025-01-XX

