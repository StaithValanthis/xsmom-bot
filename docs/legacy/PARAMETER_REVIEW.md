# Deep Parameter Review & Simplification

**Goal:** Turn this bot into a **robust, auto-tunable, MAKE MONEY machine**, not a fragile, over-parameterized science experiment.

---

## 1. PARAMETER MAP & USAGE

### Summary Statistics

- **Total parameters in config.yaml.example**: ~150+
- **Parameters actually used in code**: ~100
- **Parameters with high overfitting risk**: ~40
- **Dead parameters (never used)**: ~60
- **Proposed core parameters**: ~18

### Complete Parameter Inventory

*(See detailed tables in previous submission - continuing with analysis)*

---

## 2. UNUSED / REDUNDANT / OVERLAPPING PARAMETERS

### Dead Code (Safe to Remove Immediately)

**Total Dead Parameters: ~60**

#### Exchange (3 params)
- `exchange.recv_window_ms` — Not used by CCXT
- `exchange.include_symbols` — Use `symbol_filter.whitelist`
- `exchange.exclude_symbols` — Use `symbol_filter.banlist`

#### Strategy Filters (25+ params)
- `strategy.symbol_filter.exclude` — Legacy
- `strategy.symbol_filter.score.decay_days` — Not used
- `strategy.symbol_filter.score.pf_warn_threshold` — Not used
- `strategy.symbol_filter.score.rolling_window_hours` — Not used
- `strategy.symbol_filter.score.max_daily_loss_per_symbol_usdt` — Not used
- `strategy.soft_win_lock.*` (4 params) — Not used
- `strategy.confirmation_timeframe/lookback/require_mtf_alignment` (3 params) — Partial wiring
- `strategy.trailing.*` (10 params) — Dead, use `risk.trailing_*`
- `strategy.dispersion_gate.threshold` — Not used
- `strategy.sleeve_constraints.meme.sleeve_vol_bps` — Not used
- `strategy.meta_label.*` (7 params) — Defined but not integrated
- `strategy.risk.*` (8 params) — Dead, use `risk.*`

#### Execution (18 params)
- `execution.slippage_bps_guard` — Not used
- `execution.align_after_funding_minutes` — Not used
- `execution.funding_hours_utc` — Not used
- `execution.child_order_ttl_ms` — Not used
- `execution.cancel_retries_max` — Not used
- `execution.maker_ttl_secs` — Referenced once but unused
- `execution.offset_mode/min/max` (3 params) — Not used
- `execution.spread_guard_bps_max` — Not used
- `execution.min_order_notional_usd` — Duplicate
- `execution.cancel_stale_after_seconds` — Not used
- `execution.throttle.*` (3 params) — Not used
- `execution.pyramiding.*` (2 params) — Not used
- `execution.retry.*` (3 params) — Not used

#### Risk (12 params)
- `risk.atr_mult_tp` — Not used
- `risk.use_tp` — Not used
- `risk.reentry_after_loss_minutes` — Not used
- `risk.min_close_pnl_pct` — Not used
- `risk.no_progress.min_close_pnl_pct/tiers` (2 params) — Not used
- `risk.profit_lock.*` (3 params) — Not used
- `risk.profit_lock_steps` — Not used
- `risk.breakeven_extra_bps` — Not used
- `risk.trail_after_partial_mult` — Not used
- `risk.age_tighten` — Not used

### Redundant / Overlapping Parameters

1. **K Selection**: `strategy.k_min/k_max` vs `strategy.selection.k_min/k_max`
   - **Fix:** Remove `selection.k_min/k_max`, keep only `strategy.k_min/k_max`

2. **Entry Z-Score**: `strategy.entry_zscore_min` vs `strategy.entry_throttle.min_entry_zscore`
   - **Fix:** Merge to single `strategy.entry_zscore_min`

3. **Daily Loss Limit**: `risk.max_daily_loss_pct` vs `strategy.soft_kill.soft_daily_loss_pct`
   - **Fix:** Remove `soft_kill.soft_daily_loss_pct`, use `risk.max_daily_loss_pct`

4. **Vol Target**: `strategy.vol_target.*` vs `strategy.portfolio_vol_target.*`
   - **Fix:** Consolidate to `strategy.portfolio_vol_target.*` only

5. **Notional Caps**: Multiple overlapping caps
   - **Fix:** Use `execution.min_notional_usdt` for orders, `liquidity.notional_cap_usdt` for portfolio

6. **Offset Parameters**: `execution.price_offset_bps` vs `execution.dynamic_offset.base_bps`
   - **Fix:** If `dynamic_offset.enabled`, ignore `price_offset_bps`

7. **Spread Guard**: Multiple overlapping spread thresholds
   - **Fix:** Consolidate to `execution.spread_guard.max_spread_bps` only

### Parameters to Freeze as Constants

**Fixed Mathematical Constants:**
- `strategy.portfolio_vol_target.bars_per_year` (8760.0) — Computed from timeframe
- `strategy.lookback_weights` — Freeze at [0.4, 0.3, 0.2, 0.1] (standard momentum)

**Fixed Operational Values:**
- `risk.fast_check_seconds` (2) — Fixed for fast checks
- `risk.stop_timeframe` ("5m") — Fixed for fast checks
- `execution.order_type` ("limit") — Fixed for cost efficiency
- `execution.post_only` (true) — Fixed for maker rebates
- `strategy.kelly.half_kelly` (true) — If always half-Kelly, remove flag

---

## 3. OVERFITTING RISK ASSESSMENT

### 🔴 CRITICAL RISK (Must Simplify)

#### 1. Symbol Scoring (12+ params → 4 params)

**Current:** 12 parameters controlling dynamic symbol bans/downweights
- `min_sample_trades`, `ema_alpha`, `min_win_rate_pct`, `pf_downweight_threshold`, `downweight_factor`, `block_below_win_rate_pct`, `pf_block_threshold`, `pnl_block_threshold_usdt_per_trade`, `ban_minutes`, `grace_trades_after_unban`, `decay_days`, `pf_warn_threshold`

**Problem:** Too many knobs → overfit to historical symbol performance

**Impact:** Banning good symbols, keeping bad ones → reduced profitability

**Fix:** Simplify to:
- `min_trades` (int, default: 8)
- `win_rate_threshold` (float, default: 0.40)
- `pf_threshold` (float, default: 1.2)
- `ban_hours` (int, default: 24)

#### 2. Time-of-Day Whitelist (12 params → 1 param)

**Current:** Complex hourly tracking with EMA smoothing, boost factors, thresholds

**Problem:** Overfit to specific hours of past data

**Impact:** Trading at wrong times, missing opportunities

**Fix:** Remove or simplify to fixed `blackout_hours_utc: []` list only

#### 3. ADX Filter (7 params → 2 params)

**Current:** `enabled`, `len`, `min_adx`, `require_rising`, `use_di_alignment`, `min_di_separation`, `di_hysteresis_bps`

**Problem:** ADX is lagging indicator; multiple thresholds invite curve-fitting

**Impact:** Filtering out good trades, keeping bad ones

**Fix:** Simplify to:
- `enabled` (bool)
- `min_adx` (float, default: 25.0)

#### 4. Dynamic Entry Band (6 params → REMOVE)

**Current:** Correlation-based entry threshold with 3 tiers (high/mid/low corr, each with threshold + zmin)

**Problem:** Unclear economic rationale; invites curve-fitting

**Impact:** Wrong entry thresholds in different regimes

**Fix:** **REMOVE** entirely; use single `entry_zscore_min`

#### 5. Confirmation Gates (6 params → REMOVE or TEST)

**Current:** 2-bar confirmation, MTF alignment, z_boost

**Problem:** Adds latency; unclear if it adds edge vs. noise

**Impact:** Delayed entries, missed opportunities

**Fix:** Test in walk-forward; remove if no consistent benefit

### 🟡 HIGH RISK (Simplify)

#### 6. Adaptive Risk Scaling (9 params → 2 tiers or REMOVE)

**Current:** 3 tiers (low/mid/high vol) × 3 scales (sl_scale, trail_scale, ladder_r_scale) = 9 params

**Problem:** Overfit to volatility regimes in training data

**Fix:** Simplify to 2 tiers (low/high) OR remove adaptive scaling entirely

#### 7. Funding Trim (3 params → 2 params)

**Current:** `threshold_bps`, `slope_per_bps`, `max_reduction`

**Problem:** Slope and max_reduction invite overfitting

**Fix:** Simplify to `enabled`, `threshold_bps` (remove slope logic)

#### 8. Kelly Scaling (5 params → 2 params OR REMOVE)

**Current:** `base_frac`, `half_kelly`, `min_scale`, `max_scale`, `enabled`

**Problem:** Overlap with vol target; too many scale parameters

**Fix:** Choose Kelly OR vol target (not both). If Kelly: `enabled`, `base_frac` only

#### 9. Ensemble Weights (3 params → FREEZE)

**Current:** `weights.xsec`, `weights.ts`, `weights.breakout` (must sum to ~1.0)

**Problem:** Overfit to historical correlation patterns

**Fix:** Freeze at [0.6, 0.2, 0.2] or test only enabled/disabled

### 🟢 MEDIUM/LOW RISK (Keep but Monitor)

- **Regime Filter** (4 params): Reasonable, but test in walk-forward
- **Selection/K Selection** (5 params): Reasonable, but dynamic K adds complexity
- **Vol Target** (5 params): Economically sound, but 2 separate configs confusing

---

## 4. PROPOSED CLEAN PARAMETER SCHEMA

### Core Parameters (18 total)

#### **RISK** (6 params) — Safety Limits (Optimize with Caution)

| Key | Type | Default | Safe Range | What It Does | Optimizer? |
|-----|------|---------|------------|--------------|------------|
| `risk.max_daily_loss_pct` | float | 5.0 | [3.0, 8.0] | Daily loss kill-switch (%) | **NO** (safety limit) |
| `risk.max_portfolio_drawdown_pct` | float | 15.0 | [10.0, 20.0] | Max drawdown stop (%) | **NO** (safety limit) |
| `risk.atr_mult_sl` | float | 2.0 | [1.5, 3.0] | Stop loss (×ATR) | **YES** (tight range) |
| `risk.trail_atr_mult` | float | 1.0 | [0.5, 1.5] | Trailing stop (×ATR) | **YES** (tight range) |
| `risk.gross_leverage` | float | 1.5 | [1.0, 2.0] | Portfolio gross leverage | **YES** (tight range) |
| `risk.max_weight_per_asset` | float | 0.20 | [0.10, 0.30] | Per-asset weight cap | **YES** (tight range) |

**Rationale:** Safety limits should NOT be heavily optimized. Optimizer can adjust within tight bounds only.

#### **SIGNALS** (6 params) — Good Optimizer Knobs

| Key | Type | Default | Safe Range | What It Does | Optimizer? |
|-----|------|---------|------------|--------------|------------|
| `signals.signal_power` | float | 1.35 | [1.0, 2.0] | Nonlinear z-score amplification | **YES** |
| `signals.lookback_1h` | int | 12 | [6, 24] | Short lookback (hours) | **YES** |
| `signals.lookback_2h` | int | 24 | [12, 48] | Medium lookback (hours) | **YES** |
| `signals.lookback_3h` | int | 48 | [24, 96] | Long lookback (hours) | **YES** |
| `signals.k_min` | int | 2 | [2, 4] | Min top-K selection | **YES** |
| `signals.k_max` | int | 6 | [4, 8] | Max top-K selection | **YES** |

**Rationale:** Core signal parameters. Changes produce smooth performance changes. Good for optimizer.

**Note:** Freeze `lookback_weights` at [0.4, 0.3, 0.2, 0.1] (standard momentum weights).

#### **FILTERS** (4 params) — Entry Filters

| Key | Type | Default | Safe Range | What It Does | Optimizer? |
|-----|------|---------|------------|--------------|------------|
| `filters.regime_filter.enabled` | bool | true | [true, false] | Enable EMA slope filter | **NO** (enable/disable) |
| `filters.regime_filter.ema_len` | int | 200 | [100, 300] | EMA length (bars) | **YES** (wide range) |
| `filters.regime_filter.slope_min_bps_per_day` | float | 2.0 | [1.0, 5.0] | Min slope (bps/day) | **YES** |
| `filters.entry_zscore_min` | float | 0.0 | [0.0, 1.0] | Minimum entry z-score | **YES** |

**Rationale:** Entry filters reduce noise. Keep simple; avoid fine-tuned thresholds.

**Removed:** ADX filter, ToD whitelist, dynamic entry band, confirmation gate

#### **SIZING** (2 params) — Position Sizing

| Key | Type | Default | Safe Range | What It Does | Optimizer? |
|-----|------|---------|------------|--------------|------------|
| `sizing.vol_target_enabled` | bool | true | [true, false] | Enable portfolio vol targeting | **NO** (enable/disable) |
| `sizing.target_ann_vol` | float | 0.30 | [0.20, 0.50] | Target annualized vol | **YES** |

**Rationale:** Vol targeting is economically sound. Keep simple (1 target, fixed lookback 72h).

**Removed:** `vol_target` (duplicate), Kelly scaling (overlap), `portfolio_vol_target.lookback_hours` (freeze at 72h), `min_scale/max_scale` (freeze at 0.6/1.4)

---

### Secondary Parameters (Keep but Don't Optimize)

These are useful but should be fixed or tuned manually, not by optimizer:

#### **EXECUTION** (8 params) — Fixed or Manual Tuning

| Key | Type | Default | What It Does | Optimizer? |
|-----|------|---------|--------------|------------|
| `execution.reload_positions_on_start` | bool | true | Reload positions on start | **NO** (fixed) |
| `execution.order_type` | str | "limit" | Order type | **NO** (fixed) |
| `execution.post_only` | bool | true | Post-only orders | **NO** (fixed) |
| `execution.spread_guard.enabled` | bool | true | Enable spread guard | **NO** (fixed) |
| `execution.spread_guard.max_spread_bps` | float | 10.0 | Max spread | **NO** (manual) |
| `execution.dynamic_offset.enabled` | bool | true | Enable dynamic offset | **NO** (fixed) |
| `execution.dynamic_offset.base_bps` | float | 3.0 | Base limit offset | **NO** (manual) |
| `execution.min_notional_usdt` | float | 5.0 | Min order notional | **NO** (fixed) |

#### **SYMBOL FILTER** (4 params) — Simplified

| Key | Type | Default | What It Does | Optimizer? |
|-----|------|---------|--------------|------------|
| `filters.symbol_filter.enabled` | bool | true | Enable symbol scoring | **NO** (fixed) |
| `filters.symbol_filter.min_trades` | int | 8 | Min trades before ban | **NO** (manual) |
| `filters.symbol_filter.win_rate_threshold` | float | 0.40 | Min win rate | **NO** (manual) |
| `filters.symbol_filter.pf_threshold` | float | 1.2 | Min profit factor | **NO** (manual) |
| `filters.symbol_filter.ban_hours` | int | 24 | Ban duration (hours) | **NO** (manual) |
| `filters.symbol_filter.whitelist` | list | [] | Manual whitelist | **NO** (manual) |
| `filters.symbol_filter.banlist` | list | [] | Manual banlist | **NO** (manual) |

#### **RISK EXITS** (6 params) — Fixed or Manual

| Key | Type | Default | What It Does | Optimizer? |
|-----|------|---------|--------------|------------|
| `risk.trailing_sl.enabled` | bool | true | Enable trailing stops | **NO** (fixed) |
| `risk.trailing_sl.ma_len` | int | 34 | MA length for trailing | **NO** (manual) |
| `risk.trailing_sl.multiplier` | float | 1.5 | Trailing multiplier | **NO** (manual) |
| `risk.breakeven_after_r` | float | 0.6 | Move to BE after N×R | **NO** (manual) |
| `risk.partial_tp_r` | float | 0.75 | Partial TP at N×R | **NO** (manual) |
| `risk.partial_tp_size` | float | 0.45 | Partial TP size | **NO** (manual) |
| `risk.max_hours_in_trade` | int | 10 | Max hold time (hours) | **NO** (manual) |

#### **DIVERSIFICATION** (2 params) — Fixed

| Key | Type | Default | What It Does | Optimizer? |
|-----|------|---------|--------------|------------|
| `sizing.diversify_enabled` | bool | true | Enable correlation filter | **NO** (fixed) |
| `sizing.max_pair_corr` | float | 0.75 | Max pairwise correlation | **NO** (manual, fixed at 0.75) |

#### **CARRY SLEEVE** (2 params) — Fixed or Manual

| Key | Type | Default | What It Does | Optimizer? |
|-----|------|---------|--------------|------------|
| `sizing.carry_enabled` | bool | true | Enable carry sleeve | **NO** (fixed) |
| `sizing.carry_budget_frac` | float | 0.20 | Carry budget fraction | **NO** (manual) |

---

### Summary: Parameter Count

**Current:** ~150 parameters
**Proposed Core (Optimizable):** 18 parameters
**Proposed Secondary (Fixed/Manual):** ~25 parameters
**Total Proposed:** ~43 parameters (down from 150)

**Removed:** ~107 parameters (71% reduction)

---

## 5. MIGRATION PLAN

### Step 1: Remove Dead Code

**Delete from config.yaml:**
```yaml
# Exchange
- exchange.recv_window_ms
- exchange.include_symbols
- exchange.exclude_symbols

# Strategy (dead)
- strategy.soft_win_lock.*
- strategy.trailing.*
- strategy.dispersion_gate.*
- strategy.meta_label.*
- strategy.risk.*
- strategy.symbol_filter.exclude
- strategy.symbol_filter.score.decay_days
- strategy.symbol_filter.score.pf_warn_threshold
- strategy.symbol_filter.score.rolling_window_hours
- strategy.symbol_filter.score.max_daily_loss_per_symbol_usdt
- strategy.confirmation_timeframe
- strategy.confirmation_lookback
- strategy.require_mtf_alignment

# Execution (dead)
- execution.slippage_bps_guard
- execution.align_after_funding_minutes
- execution.funding_hours_utc
- execution.child_order_ttl_ms
- execution.cancel_retries_max
- execution.maker_ttl_secs
- execution.offset_mode
- execution.offset_bps_min
- execution.offset_bps_max
- execution.spread_guard_bps_max
- execution.min_order_notional_usd
- execution.cancel_stale_after_seconds
- execution.throttle.*
- execution.pyramiding.*
- execution.retry.*

# Risk (dead)
- risk.atr_mult_tp
- risk.use_tp
- risk.reentry_after_loss_minutes
- risk.min_close_pnl_pct
- risk.no_progress.min_close_pnl_pct
- risk.no_progress.tiers
- risk.profit_lock.*
- risk.profit_lock_steps
- risk.breakeven_extra_bps
- risk.trail_after_partial_mult
- risk.age_tighten
```

### Step 2: Consolidate Redundant Parameters

**Merge operations:**
```yaml
# Merge K selection
strategy.k_min → signals.k_min
strategy.k_max → signals.k_max
DELETE: strategy.selection.k_min/k_max

# Merge entry z-score
strategy.entry_zscore_min → filters.entry_zscore_min
DELETE: strategy.entry_throttle.min_entry_zscore

# Merge daily loss
risk.max_daily_loss_pct → (keep, update strategy.soft_kill to use this)
DELETE: strategy.soft_kill.soft_daily_loss_pct

# Merge vol target
strategy.portfolio_vol_target.* → sizing.vol_target.*
DELETE: strategy.vol_target.*

# Merge notional caps
execution.min_notional_per_order_usdt → execution.min_notional_usdt
DELETE: execution.min_order_notional_usd

# Merge offset
execution.dynamic_offset.base_bps (if enabled) OR execution.price_offset_bps (if disabled)
DELETE: execution.price_offset_bps (use dynamic_offset.base_bps only)

# Merge spread guard
execution.spread_guard.max_spread_bps → (keep only this)
DELETE: execution.microstructure.max_spread_bps
DELETE: execution.spread_guard_bps_max
```

### Step 3: Simplify High-Risk Parameters

**Symbol Scoring (12 params → 4):**
```yaml
# OLD
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
  # ... more

# NEW
filters.symbol_filter:
  enabled: true
  min_trades: 8
  win_rate_threshold: 0.40
  pf_threshold: 1.2
  ban_hours: 24
```

**Time-of-Day Whitelist (12 params → 1):**
```yaml
# OLD
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
  # ...

# NEW (REMOVE entirely, OR keep only blackout hours)
filters.blackout_hours_utc: []  # Manual list, no learning
```

**ADX Filter (7 params → 2):**
```yaml
# OLD
strategy.adx_filter:
  enabled: true
  len: 14
  min_adx: 28.0
  require_rising: true
  use_di_alignment: true
  min_di_separation: 0.0
  di_hysteresis_bps: 0.0

# NEW
filters.adx_filter:
  enabled: false  # Disable by default (overfitting risk)
  min_adx: 25.0   # If enabled
```

**Dynamic Entry Band (6 params → REMOVE):**
```yaml
# DELETE entirely
strategy.dynamic_entry_band:  # REMOVE
```

**Confirmation Gates (6 params → REMOVE):**
```yaml
# DELETE entirely (test in walk-forward first)
strategy.confirmation:  # REMOVE
strategy.confirmation_timeframe:  # REMOVE
strategy.confirmation_lookback:  # REMOVE
strategy.require_mtf_alignment:  # REMOVE
```

### Step 4: Reorganize Schema

**New structure:**
```yaml
# Exchange (unchanged, just remove dead params)
exchange:
  id: bybit
  account_type: swap
  quote: USDT
  only_perps: true
  unified_margin: true
  testnet: false
  max_symbols: 36
  min_usd_volume_24h: 100000000
  min_price: 0.05
  timeframe: 1h
  candles_limit: 1500

# Risk (safety limits - optimize with caution)
risk:
  max_daily_loss_pct: 5.0
  max_portfolio_drawdown_pct: 15.0
  atr_mult_sl: 2.0
  trail_atr_mult: 1.0
  gross_leverage: 1.5
  max_weight_per_asset: 0.20
  
  # Exit rules (fixed/manual)
  trailing_sl:
    enabled: true
    ma_len: 34
    multiplier: 1.5
  breakeven_after_r: 0.6
  partial_tp_r: 0.75
  partial_tp_size: 0.45
  max_hours_in_trade: 10
  atr_len: 28
  fast_check_seconds: 2
  stop_timeframe: "5m"

# Signals (good optimizer knobs)
signals:
  signal_power: 1.35
  lookback_1h: 12
  lookback_2h: 24
  lookback_3h: 48
  k_min: 2
  k_max: 6
  market_neutral: true
  vol_lookback: 96  # For inverse-vol sizing

# Filters (entry filters - keep simple)
filters:
  regime_filter:
    enabled: true
    ema_len: 200
    slope_min_bps_per_day: 2.0
  entry_zscore_min: 0.0
  symbol_filter:
    enabled: true
    min_trades: 8
    win_rate_threshold: 0.40
    pf_threshold: 1.2
    ban_hours: 24
    whitelist: []
    banlist: []
  adx_filter:
    enabled: false  # Disable by default
    min_adx: 25.0
  blackout_hours_utc: []  # Simple blackout list

# Sizing (position sizing)
sizing:
  vol_target_enabled: true
  target_ann_vol: 0.30
  diversify_enabled: true
  max_pair_corr: 0.75
  carry_enabled: true
  carry_budget_frac: 0.20

# Execution (fixed/manual)
execution:
  reload_positions_on_start: true
  order_type: limit
  post_only: true
  rebalance_minute: 1
  poll_seconds: 10
  min_notional_usdt: 5.0
  min_rebalance_delta_bps: 6.0
  
  spread_guard:
    enabled: true
    max_spread_bps: 10.0
  dynamic_offset:
    enabled: true
    base_bps: 3.0
    per_spread_coeff: 0.5
    max_offset_bps: 20.0
  microstructure:
    enabled: true
    min_obi: 0.20
    max_spread_bps: 6.0
    min_top_of_book_depth_usd: 10000
  stale_orders:
    enabled: true
    cleanup_interval_sec: 60
    max_age_sec: 180
    reprice_if_far_bps: 15.0

# Liquidity (caps)
liquidity:
  adv_cap_pct: 0.08
  notional_cap_usdt: 20000.0

# Paths, logging, costs (unchanged)
paths: ...
logging: ...
costs: ...
```

---

## 6. SELF-OPTIMIZATION FRIENDLINESS & MAKE MONEY RATIONALE

### Optimizer-Friendly Design

#### Minimal Parameter Set for Walk-Forward Optimizer

**Optimizable Parameters (18 total):**

1. **Signals (6 params):**
   - `signals.signal_power` [1.0, 2.0]
   - `signals.lookback_1h` [6, 24]
   - `signals.lookback_2h` [12, 48]
   - `signals.lookback_3h` [24, 96]
   - `signals.k_min` [2, 4]
   - `signals.k_max` [4, 8]

2. **Filters (3 params):**
   - `filters.regime_filter.ema_len` [100, 300]
   - `filters.regime_filter.slope_min_bps_per_day` [1.0, 5.0]
   - `filters.entry_zscore_min` [0.0, 1.0]

3. **Risk (5 params - tight ranges):**
   - `risk.atr_mult_sl` [1.5, 3.0]
   - `risk.trail_atr_mult` [0.5, 1.5]
   - `risk.gross_leverage` [1.0, 2.0]
   - `risk.max_weight_per_asset` [0.10, 0.30]
   - `sizing.target_ann_vol` [0.20, 0.50]

4. **Enable/Disable Flags (4 params - binary):**
   - `filters.regime_filter.enabled` [true, false]
   - `filters.adx_filter.enabled` [true, false]
   - `sizing.vol_target_enabled` [true, false]
   - `sizing.diversify_enabled` [true, false]

**Total Search Space:** 18 parameters (down from 150+)

#### Recommended Optimizer Strategy

1. **Walk-Forward Optimization:**
   - Use purged walk-forward with embargo (reduces overfitting)
   - Train on 80% of data, validate on 20% holdout
   - Only accept if holdout Sharpe > baseline

2. **Parameter Ranges:**
   - Use reasonable bounds (no absurd leverage, no negative values)
   - Safety limits (max_daily_loss_pct, max_drawdown_pct) should NOT be optimized

3. **Objective Function:**
   - Primary: Sharpe ratio (risk-adjusted returns)
   - Secondary: Calmar ratio (return / max drawdown)
   - Penalty: Turnover (to avoid overtrading)
   - **Score = Sharpe - 0.001 × Turnover**

4. **Constraints:**
   - Max drawdown < 20%
   - Annualized vol < 50%
   - Min trades > 100 (enough data)

### How This Design Helps MAKE MONEY

#### 1. **Reduced Overfitting Risk**

**Before:** 40+ high-risk parameters → overfit to training data → poor out-of-sample performance

**After:** 18 core parameters with reasonable ranges → more robust → consistent live performance

**Impact:** Strategy performs similarly in backtest and live trading → **consistent profits**

#### 2. **Faster Optimization**

**Before:** 150 parameters → exponential search space → slow optimization, local minima

**After:** 18 parameters → manageable search space → faster convergence, better global optimum

**Impact:** Optimizer finds better parameters faster → **better risk-adjusted returns**

#### 3. **Clearer Economic Interpretation**

**Before:** Many parameters with unclear relationships (e.g., dynamic entry band, ToD whitelist)

**After:** Each parameter has clear economic meaning (leverage, stop distance, entry threshold)

**Impact:** Easier to reason about strategy behavior → **fewer bugs, better risk management**

#### 4. **Easier Maintenance**

**Before:** 150 parameters → hard to understand, easy to misconfigure

**After:** 43 parameters (18 core + 25 fixed) → easier to understand and maintain

**Impact:** Less operational risk → **fewer trading errors → consistent profits**

#### 5. **Stable Out-of-Sample Performance**

**Before:** Overfitted parameters → great backtest, poor live performance

**After:** Simplified parameters → more stable across regimes

**Impact:** Live performance matches backtest → **predictable profits**

---

### Example: Cleaned-Up `config.yaml`

```yaml
# ========================================
# XSMOM Trading Bot - Simplified Config
# ========================================
# Core: 18 optimizable params + 25 fixed params
# Removed: 107 dead/redundant params (71% reduction)

# ----------------------------------------
# Exchange Configuration
# ----------------------------------------
exchange:
  id: bybit
  account_type: swap
  quote: USDT
  only_perps: true
  unified_margin: true
  testnet: false
  max_symbols: 36
  min_usd_volume_24h: 100000000
  min_price: 0.05
  timeframe: 1h
  candles_limit: 1500

# ----------------------------------------
# Risk Parameters (Safety Limits)
# ----------------------------------------
# WARNING: Do NOT heavily optimize safety limits
# Optimizer should only adjust within tight bounds
risk:
  # Safety limits (DO NOT optimize heavily)
  max_daily_loss_pct: 5.0          # Daily loss kill-switch (%)
  max_portfolio_drawdown_pct: 15.0 # Max drawdown stop (%)
  
  # Risk knobs (optimize within tight ranges)
  atr_mult_sl: 2.0                 # Stop loss (×ATR) [1.5, 3.0]
  trail_atr_mult: 1.0              # Trailing stop (×ATR) [0.5, 1.5]
  gross_leverage: 1.5              # Portfolio gross leverage [1.0, 2.0]
  max_weight_per_asset: 0.20       # Per-asset weight cap [0.10, 0.30]
  
  # Exit rules (fixed/manual tuning)
  trailing_sl:
    enabled: true
    ma_len: 34                     # MA length for trailing stop
    multiplier: 1.5                # Trailing multiplier (×ATR)
  breakeven_after_r: 0.6           # Move to breakeven after N×R
  partial_tp_r: 0.75               # Partial TP at N×R
  partial_tp_size: 0.45            # Partial TP size (%)
  max_hours_in_trade: 10           # Max hold time (hours)
  
  # Fixed values (do not change)
  atr_len: 28                      # ATR period (fixed)
  fast_check_seconds: 2            # Fast check interval (fixed)
  stop_timeframe: "5m"             # Stop check timeframe (fixed)

# ----------------------------------------
# Signal Generation (Optimizer Knobs)
# ----------------------------------------
signals:
  signal_power: 1.35               # Nonlinear z-score amplification [1.0, 2.0]
  lookback_1h: 12                  # Short lookback (hours) [6, 24]
  lookback_2h: 24                  # Medium lookback (hours) [12, 48]
  lookback_3h: 48                  # Long lookback (hours) [24, 96]
  k_min: 2                         # Min top-K selection [2, 4]
  k_max: 6                         # Max top-K selection [4, 8]
  market_neutral: true             # De-mean cross-section (fixed)
  vol_lookback: 96                 # Vol lookback for inverse-vol sizing (fixed)

# Note: lookback_weights frozen at [0.4, 0.3, 0.2, 0.1] (standard momentum)

# ----------------------------------------
# Entry Filters (Keep Simple)
# ----------------------------------------
filters:
  regime_filter:
    enabled: true                  # Enable EMA slope filter
    ema_len: 200                   # EMA length (bars) [100, 300]
    slope_min_bps_per_day: 2.0    # Min slope (bps/day) [1.0, 5.0]
  
  entry_zscore_min: 0.0            # Minimum entry z-score [0.0, 1.0]
  
  symbol_filter:
    enabled: true                  # Enable symbol scoring
    min_trades: 8                  # Min trades before ban (fixed)
    win_rate_threshold: 0.40       # Min win rate (fixed)
    pf_threshold: 1.2              # Min profit factor (fixed)
    ban_hours: 24                  # Ban duration (hours) (fixed)
    whitelist: []                  # Manual whitelist (manual)
    banlist: []                    # Manual banlist (manual)
  
  adx_filter:
    enabled: false                 # Disable by default (overfitting risk)
    min_adx: 25.0                  # Min ADX (if enabled) (fixed)
  
  blackout_hours_utc: []           # Simple blackout hours (manual, no learning)

# ----------------------------------------
# Position Sizing
# ----------------------------------------
sizing:
  vol_target_enabled: true         # Enable portfolio vol targeting
  target_ann_vol: 0.30             # Target annualized vol [0.20, 0.50]
  # Note: lookback_hours frozen at 72h, min_scale/max_scale frozen at 0.6/1.4
  
  diversify_enabled: true          # Enable correlation filter (fixed)
  max_pair_corr: 0.75              # Max pairwise correlation (fixed)
  
  carry_enabled: true              # Enable carry sleeve (fixed)
  carry_budget_frac: 0.20          # Carry budget fraction (fixed, manual)

# ----------------------------------------
# Execution (Fixed/Manual)
# ----------------------------------------
execution:
  reload_positions_on_start: true  # Reload positions on start (fixed)
  order_type: limit                # Order type (fixed: cost efficiency)
  post_only: true                  # Post-only orders (fixed: maker rebates)
  rebalance_minute: 1              # Rebalance minute (fixed)
  poll_seconds: 10                 # Poll interval (fixed)
  min_notional_usdt: 5.0           # Min order notional (fixed)
  min_rebalance_delta_bps: 6.0     # Min rebalance delta (manual)
  
  spread_guard:
    enabled: true                  # Enable spread guard (fixed)
    max_spread_bps: 10.0           # Max spread (manual)
  
  dynamic_offset:
    enabled: true                  # Enable dynamic offset (fixed)
    base_bps: 3.0                  # Base limit offset (manual)
    per_spread_coeff: 0.5          # Spread coefficient (manual)
    max_offset_bps: 20.0           # Max offset (manual)
  
  microstructure:
    enabled: true                  # Enable micro checks (fixed)
    min_obi: 0.20                  # Min order book imbalance (manual)
    max_spread_bps: 6.0            # Max spread for micro (manual)
    min_top_of_book_depth_usd: 10000  # Min depth (manual)
  
  stale_orders:
    enabled: true                  # Enable stale cleanup (fixed)
    cleanup_interval_sec: 60       # Cleanup interval (fixed)
    max_age_sec: 180               # Max order age (fixed)
    reprice_if_far_bps: 15.0       # Reprice threshold (manual)

# ----------------------------------------
# Liquidity Caps
# ----------------------------------------
liquidity:
  adv_cap_pct: 0.08                # ADV% cap (fixed)
  notional_cap_usdt: 20000.0       # Absolute notional cap (fixed)

# ----------------------------------------
# Paths, Logging, Costs (Unchanged)
# ----------------------------------------
paths:
  state_path: /opt/xsmom-bot/state.json
  logs_dir: /opt/xsmom-bot/logs
  metrics_path: null

logging:
  level: INFO
  file_max_mb: 20
  file_backups: 5

costs:
  maker_fee_bps: 1.0
  taker_fee_bps: 5.0
  slippage_bps: 2.0
  maker_fill_ratio: 0.5
```

---

## SUMMARY

### Key Achievements

1. **Reduced Parameters:** 150+ → 43 (71% reduction)
2. **Core Optimizable:** 18 parameters (down from 100+)
3. **Removed Overfitting Risk:** Eliminated 40+ high-risk parameters
4. **Clearer Structure:** Organized into logical groups (risk, signals, filters, sizing, execution)
5. **Optimizer-Friendly:** Manageable search space, reasonable ranges, clear objectives

### Next Steps

1. **Delete dead parameters** from config files (60 params)
2. **Consolidate redundant parameters** (7 merges)
3. **Simplify high-risk parameters** (5 major simplifications)
4. **Update code** to use new parameter names (backward compat shims)
5. **Test** cleaned config with backtest → verify behavior unchanged
6. **Run walk-forward optimizer** on new 18-param set
7. **Monitor live performance** vs. backtest (should be closer)

### Expected Impact

**Before:** Fragile, over-parameterized → overfit backtest → poor live performance

**After:** Robust, simplified → stable backtest → consistent live performance → **MAKE MONEY**

---

*End of Parameter Review*

