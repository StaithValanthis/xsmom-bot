# Strategy Logic

## Strategy Type: Cross-Sectional Momentum (XSMOM) with Regime Switching

**xsmom-bot** implements a **cross-sectional momentum (XSMOM)** strategy that ranks cryptocurrencies by their momentum relative to each other, goes long the top K assets (strongest momentum), and shorts the bottom K assets (weakest momentum).

---

## Core Signal Generation

### 1. Multi-Lookback Momentum

**Process:**
1. Compute returns over multiple lookback periods (default: [12h, 24h, 48h, 96h])
2. Weight each lookback (default: [0.4, 0.3, 0.2, 0.1] - longer lookbacks get lower weights)
3. Compute weighted average return per asset

**Configuration:**
- `strategy.lookbacks`: List of lookback periods (hours/bars)
- `strategy.lookback_weights`: Weights for each lookback (should sum to ~1.0)

**Rationale:**
- Short lookbacks (12h) capture recent momentum
- Long lookbacks (96h) capture persistent trends
- Weighted combination reduces noise while maintaining responsiveness

### 2. Cross-Sectional Z-Scores

**Process:**
1. For each bar, compute returns for all assets
2. Compute cross-sectional mean and std (across all assets)
3. Normalize each asset's return to z-score: `z = (return - mean) / std`

**Formula:**
```
z_i = (return_i - mean(returns)) / std(returns)
```

**Rationale:**
- Normalizes returns relative to the universe
- Removes market-wide effects (beta neutral)
- Identifies assets with momentum **relative to peers**

### 3. Signal Power Amplification

**Process:**
1. Apply nonlinear amplification: `signal = sign(z) * |z|^power`
2. Default power: `strategy.signal_power = 1.35`

**Formula:**
```
signal_i = sign(z_i) * |z_i|^signal_power
```

**Rationale:**
- Amplifies strong signals (z > 1.0) more than weak signals
- Suppresses noise (z < 0.5) more aggressively
- Power > 1.0 creates convexity (strong signals dominate)

**Example:**
- z = 1.0 → signal = 1.0 (no change)
- z = 2.0 → signal = 2.7 (amplified)
- z = 0.5 → signal = 0.4 (suppressed)

### 4. Top-K Selection

**Process:**
1. Rank assets by amplified signal (descending)
2. Select top K assets → **long positions**
3. Select bottom K assets → **short positions**
4. K is dynamic: adapts to market dispersion (optional)

**Configuration:**
- `strategy.k_min`: Minimum K (default: 2)
- `strategy.k_max`: Maximum K (default: 6)
- `strategy.selection.enabled`: Enable dynamic K selection

**Dynamic K Logic:**
- **High dispersion** (z-scores spread out) → Wider K (more positions)
- **Low dispersion** (z-scores clustered) → Tighter K (fewer positions)
- **Formula**: `K = f(dispersion)` where `dispersion = median(|z-scores|)`

**Rationale:**
- Adapts to market regime (trending vs choppy)
- More positions when clear trends, fewer when noisy
- Maintains diversification while reducing noise

---

## Signal Filtering

### 1. Regime Filter (EMA Slope)

**Process:**
1. Compute EMA(close, length) for each asset (default: 200 bars)
2. Compute EMA slope (change over last N bars, normalized to bps/day)
3. Only trade assets with slope >= threshold (default: 3.5 bps/day)

**Configuration:**
- `strategy.regime_filter.enabled`: Enable regime filter
- `strategy.regime_filter.ema_len`: EMA length (default: 200)
- `strategy.regime_filter.slope_min_bps_per_day`: Minimum slope (default: 3.5 bps/day)
- `strategy.regime_filter.use_abs`: Use absolute slope (trend in either direction)

**Rationale:**
- Only trade when market shows clear trend
- Avoids choppy/trendless markets (reduce whipsaws)
- EMA slope is a robust trend indicator

### 2. ADX Filter (Optional)

**Process:**
1. Compute ADX (Average Directional Index) for each asset
2. Only trade assets with ADX >= threshold (default: 28)
3. Optional: Require rising ADX (trend strengthening)

**Configuration:**
- `strategy.adx_filter.enabled`: Enable ADX filter
- `strategy.adx_filter.min_adx`: Minimum ADX (default: 28.0)
- `strategy.adx_filter.require_rising`: Require rising ADX

**Rationale:**
- ADX measures trend strength (not direction)
- High ADX (>25) = strong trend
- Low ADX (<20) = weak trend (avoid)

**Note:** ADX is a lagging indicator. Use with caution (overfitting risk).

### 3. Symbol Scoring (Performance-Based Filtering)

**Process:**
1. Track per-symbol trade statistics (win rate, profit factor, PnL)
2. Downweight or ban symbols with poor performance
3. EMA-smooth statistics to reduce noise

**Configuration:**
- `strategy.symbol_filter.score.enabled`: Enable symbol scoring
- `strategy.symbol_filter.score.min_win_rate_pct`: Minimum win rate (default: 50%)
- `strategy.symbol_filter.score.pnl_block_threshold_usdt_per_trade`: PnL threshold for ban (default: -0.015)

**Rationale:**
- Prevents trading assets with consistently poor performance
- Adaptive filtering (learns from live trading)
- Reduces exposure to bad actors

### 4. Time-of-Day Whitelist (Optional)

**Process:**
1. Track per-hour trade performance
2. Only trade during "good hours" (profitable on average)
3. Downweight or skip "bad hours" (unprofitable on average)

**Configuration:**
- `strategy.time_of_day_whitelist.enabled`: Enable ToD filter
- `strategy.time_of_day_whitelist.min_trades_per_hour`: Minimum trades required (default: 12)
- `strategy.time_of_day_whitelist.blackout_hours_utc`: Fixed blackout hours

**Rationale:**
- Some hours are more profitable than others (liquidity, volatility)
- Avoids trading during low-liquidity hours
- Adaptive filtering (learns from live trading)

**Note:** High overfitting risk. Use fixed blackout hours if needed.

### 5. Breadth Gate

**Process:**
1. Count fraction of assets passing entry threshold (z >= entry_zscore_min)
2. Only trade if fraction >= threshold (default: 20%)
3. If gate fails, zero all positions (avoid isolated trades)

**Configuration:**
- `strategy.breadth_gate.enabled`: Enable breadth gate
- `strategy.breadth_gate.min_fraction`: Minimum fraction passing (default: 0.20)

**Rationale:**
- Ensures broad market participation (not just one or two assets)
- Reduces concentration risk
- Avoids trading on isolated signals (likely noise)

---

## Position Sizing

### 1. Inverse-Volatility Sizing

**Process:**
1. Compute volatility for each asset (std of returns over vol_lookback, default: 96 bars)
2. Weight inversely to volatility: `weight_i = (1 / volatility_i) / sum(1 / volatility_j)`
3. Normalize to portfolio gross leverage

**Configuration:**
- `strategy.vol_lookback`: Volatility lookback period (default: 96 bars)
- `strategy.gross_leverage`: Portfolio gross leverage (default: 0.95)

**Rationale:**
- Equal risk contribution per asset (risk parity style)
- Higher volatility → smaller position (less risk)
- Lower volatility → larger position (more capital)

### 2. Market Neutrality

**Process:**
1. Compute long weights (top K assets)
2. Compute short weights (bottom K assets)
3. Ensure long/short balance: `sum(longs) = sum(shorts)`

**Configuration:**
- `strategy.market_neutral`: Enable market neutrality (default: true)

**Rationale:**
- Removes market beta (profit from relative performance, not market direction)
- Reduces exposure to market crashes
- Focuses on cross-sectional alpha

### 3. Per-Asset Caps

**Process:**
1. Cap each asset's weight at threshold (default: 9% of portfolio)
2. Redistribute excess to other positions
3. Apply absolute notional cap (default: $20k per asset)

**Configuration:**
- `strategy.max_weight_per_asset`: Per-asset weight cap (default: 0.09)
- `liquidity.notional_cap_usdt`: Absolute notional cap (default: 20000.0)

**Rationale:**
- Prevents over-concentration in one asset
- Limits exposure to illiquid assets
- Maintains diversification

### 4. Portfolio Volatility Targeting

**Process:**
1. Measure recent portfolio volatility (default: 72 hours)
2. Scale all positions up/down to target volatility (default: 24% annualized)
3. Apply min/max scale factors to prevent extreme scaling

**Configuration:**
- `strategy.portfolio_vol_target.enabled`: Enable vol targeting
- `strategy.portfolio_vol_target.target_ann_vol`: Target annualized volatility (default: 0.24)
- `strategy.portfolio_vol_target.min_scale`: Minimum scale factor (default: 0.5)
- `strategy.portfolio_vol_target.max_scale`: Maximum scale factor (default: 1.6)

**Rationale:**
- Maintains consistent risk level across market regimes
- Scales up in low-volatility markets (more positions)
- Scales down in high-volatility markets (less risk)

### 5. Kelly Scaling (Optional)

**Process:**
1. Estimate win rate and win/loss ratio per asset (from historical trades)
2. Compute Kelly fraction: `f = (p * win - (1-p) * loss) / win`
3. Scale positions by fractional Kelly (default: 0.5× = half-Kelly)

**Configuration:**
- `strategy.kelly.enabled`: Enable Kelly scaling
- `strategy.kelly.base_frac`: Base Kelly fraction (default: 0.5)

**Rationale:**
- Optimal position sizing based on historical performance
- Scales up high-conviction positions (high win rate)
- Scales down low-conviction positions (low win rate)

**Note:** Overlaps with vol targeting. Choose one or combine carefully.

---

## Entry / Exit Logic

### Entry Conditions

**For long positions:**
1. Asset ranked in top K by amplified signal
2. Signal >= entry_zscore_min (default: 0.0)
3. Passes all filters (regime, ADX, symbol scoring, ToD)
4. Breadth gate passes (if enabled)
5. Not in cooldown (anti-churn)

**For short positions:**
1. Asset ranked in bottom K by amplified signal
2. Signal <= -entry_zscore_min
3. Same filters as longs

### Exit Conditions

**Stop-Loss:**
- **ATR-based**: Exit if price moves against position by `risk.atr_mult_sl × ATR` (default: 2.0× ATR)
- **Trailing stop**: Stop moves up as price moves favorably (default: `risk.trail_atr_mult × ATR`)
- **Breakeven**: Move stop to entry price after `risk.breakeven_after_r × R` (default: 0.60× R)

**Take-Profit:**
- **Partial TP**: Exit portion of position at `risk.partial_tp_r × R` (default: 0.75× R)
- **Full TP**: Optional (disabled by default)

**Other Exits:**
- **Regime flip**: Exit if EMA slope reverses (if enabled)
- **No progress**: Exit if position hasn't moved after time threshold (if enabled)
- **Max hold time**: Exit after `risk.max_hours_in_trade` hours (default: 10 hours)

See [`risk_management.md`](risk_management.md) for detailed stop/TP logic.

---

## Carry Trading (Optional)

**xsmom-bot** includes an optional **carry trading sleeve** that exploits funding rate differentials and futures basis.

### Funding Carry

**Process:**
1. Rank assets by funding rate (8-hour funding)
2. Filter by 30-day percentile (only trade when funding is high/low)
3. Go long assets with high funding (positive carry)
4. Hedge with inverse position (delta-neutral)

**Configuration:**
- `strategy.carry.enabled`: Enable carry sleeve
- `strategy.carry.budget_frac`: Budget allocated to carry (default: 0.20)
- `strategy.carry.funding.min_percentile_30d`: Minimum 30d percentile (default: 0.8)

### Basis Carry

**Process:**
1. Compute basis (futures price - spot price) for futures markets
2. Rank by annualized basis
3. Go long assets with high basis (positive carry)
4. Hedge with inverse position (delta-neutral)

**Configuration:**
- `strategy.carry.basis.use`: Enable basis carry
- `strategy.carry.basis.min_annualized`: Minimum annualized basis (default: 0.05)

### Combination

**Process:**
1. Compute momentum targets (main strategy)
2. Compute carry targets (funding + basis)
3. Combine: `final_targets = (1 - carry_budget) × momentum + carry_budget × carry`
4. Apply same risk controls (caps, vol targeting)

**Rationale:**
- Diversifies return sources (momentum + carry)
- Carry is orthogonal to momentum (uncorrelated)
- Delta-neutral hedging removes market risk

See [`../kb/framework_overview.md`](../kb/framework_overview.md) for detailed carry logic.

---

## Regime Switching (Optional)

**xsmom-bot** can dynamically switch between **XSMOM** (cross-sectional) and **TSMOM** (time-series) based on market conditions.

**Process:**
1. Measure market correlations (average pairwise correlation)
2. Measure majors trend (BTC/ETH EMA slope)
3. Measure dispersion (z-score spread)
4. Choose strategy:
   - **XSMOM**: High correlations + strong majors trend → rank relative momentum
   - **TSMOM**: Low correlations + weak majors trend → trade absolute momentum

**Configuration:**
- `regime_router.py` implements switching logic
- Enabled via `build_targets_auto()` in `live.py`

**Rationale:**
- Adapts to market regime (trending vs choppy)
- XSMOM works best in correlated, trending markets
- TSMOM works best in uncorrelated, choppy markets

See [`../kb/framework_overview.md`](../kb/framework_overview.md) for detailed regime switching logic.

---

## Strategy Flow Summary

```
1. Fetch OHLCV bars for universe
   ↓
2. Compute multi-lookback momentum
   ↓
3. Compute cross-sectional z-scores
   ↓
4. Apply signal power amplification
   ↓
5. Filter signals:
   ├─ Regime filter (EMA slope)
   ├─ ADX filter (optional)
   ├─ Symbol scoring (optional)
   └─ Time-of-day whitelist (optional)
   ↓
6. Apply breadth gate (optional)
   ↓
7. Select top K longs, bottom K shorts
   ↓
8. Size positions:
   ├─ Inverse-volatility sizing
   ├─ Market-neutral centering
   ├─ Per-asset caps
   ├─ Portfolio vol targeting
   └─ Kelly scaling (optional)
   ↓
9. Combine with carry sleeve (optional)
   ↓
10. Apply risk checks (daily loss, drawdown limits)
   ↓
11. Place/reconcile orders (limit orders, post-only)
   ↓
12. Monitor positions (stop-loss, trailing stops, partial TP)
```

---

## Strategy Rationale

### Why XSMOM?

1. **Market Neutral**: Removes market beta (profit from relative performance, not market direction)
2. **Robust**: Cross-sectional ranking is more stable than absolute returns
3. **Adaptive**: Dynamic K selection adapts to market dispersion
4. **Risk-Managed**: Inverse-volatility sizing maintains equal risk contribution

### Why Multi-Lookback?

1. **Reduces Noise**: Weighted combination filters out short-term noise
2. **Captures Trends**: Long lookbacks capture persistent trends
3. **Maintains Responsiveness**: Short lookbacks capture recent momentum
4. **Robust**: Works across different market regimes

### Why Signal Power?

1. **Amplifies Strong Signals**: Convexity means strong signals dominate
2. **Suppresses Noise**: Weak signals are suppressed more aggressively
3. **Nonlinear**: Better captures momentum persistence (momentum begets momentum)

### Why Regime Filtering?

1. **Avoids Choppy Markets**: Only trade when clear trend exists
2. **Reduces Whipsaws**: Fewer false signals in trendless markets
3. **Improves Win Rate**: Higher win rate in trending markets

### Why Inverse-Volatility Sizing?

1. **Risk Parity**: Equal risk contribution per asset
2. **Stable Returns**: Less volatility drag from high-volatility assets
3. **Robust**: Works across different volatility regimes

---

## Parameter Sensitivity

### High-Impact Parameters

These parameters have **significant impact** on performance:

- `strategy.signal_power` (1.0-2.0) - Controls signal amplification
- `strategy.k_min` / `strategy.k_max` (2-8) - Controls position count
- `strategy.gross_leverage` (0.5-2.0) - Controls portfolio exposure
- `risk.atr_mult_sl` (1.5-3.0) - Controls stop-loss distance
- `strategy.portfolio_vol_target.target_ann_vol` (0.15-0.40) - Controls risk level

### Medium-Impact Parameters

These parameters have **moderate impact**:

- `strategy.lookbacks` - Lookback periods
- `strategy.regime_filter.slope_min_bps_per_day` - Regime threshold
- `strategy.max_weight_per_asset` - Per-asset cap
- `risk.trail_atr_mult` - Trailing stop distance

### Low-Impact Parameters

These parameters have **minor impact** (fine-tuning):

- `strategy.lookback_weights` - Lookback weights (if using standard momentum)
- `strategy.entry_zscore_min` - Entry threshold (if > 0.0)
- `strategy.adx_filter.min_adx` - ADX threshold

See [`../reference/config_reference.md`](../reference/config_reference.md) for complete parameter list.

---

## Strategy Limitations

1. **Requires Trending Markets**: Regime filter means bot only trades in trending markets (may miss choppy markets)
2. **Market-Neutral Focus**: Removes market beta (can't profit from strong bull/bear markets)
3. **Position Sizing Sensitivity**: Inverse-volatility sizing assumes volatility predicts risk (may not hold in tail events)
4. **Overfitting Risk**: Many parameters (150+) create overfitting risk if not optimized carefully

**Mitigations:**
- Walk-forward optimization (reduces overfitting)
- Conservative parameter ranges (prevents extreme values)
- Regular re-optimization (adapts to market changes)

---

## Next Steps

- **Risk Management**: [`risk_management.md`](risk_management.md) - Stop-loss, take-profit, kill-switches
- **Config System**: [`config_system.md`](config_system.md) - How parameters control behavior
- **Knowledge Base**: [`../kb/framework_overview.md`](../kb/framework_overview.md) - Complete framework map

---

**Motto: MAKE MONEY** — with a clear, well-understood, and well-documented strategy. 📈

