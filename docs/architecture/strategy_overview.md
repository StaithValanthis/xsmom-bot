# Strategy Overview

## Core Strategy: Cross-Sectional Momentum (XSMOM)

**xsmom-bot** is an automated multi-pair crypto futures trading bot that implements a **cross-sectional momentum (XSMOM)** strategy. The bot ranks cryptocurrencies by their momentum relative to each other, goes long the top K assets (strongest momentum), and shorts the bottom K assets (weakest momentum).

**Motto:** **MAKE MONEY** — with robust, risk-managed, data-validated behavior.

---

## Strategy Flow

```
1. Fetch OHLCV Data
   ↓
2. Generate Signals (Multi-lookback momentum → z-scores → ranking)
   ↓
3. Apply Filters (Regime, symbol scoring, volatility breakout gate)
   ↓
4. Select Positions (Top K longs, bottom K shorts)
   ↓
5. Size Positions (Inverse-volatility or fixed risk-per-trade)
   ↓
6. Apply Risk Controls (Correlation limits, max positions, vol regime scaling)
   ↓
7. Combine Sleeves (Momentum + Carry sleeve with budget allocation)
   ↓
8. Execute Orders (Limit orders with spread guards)
   ↓
9. Monitor Positions (Stop-loss, trailing stops, R-multiple profit targets, breakeven, time exits)
```

---

## Signal Generation

### 1. Multi-Lookback Momentum

The bot computes momentum over multiple lookback periods (default: [12h, 24h, 48h, 96h]) and weights each period (default: [0.4, 0.3, 0.2, 0.1] — shorter lookbacks get higher weights).

**Rationale:**
- Short lookbacks (12h) capture recent momentum
- Long lookbacks (96h) capture persistent trends
- Weighted combination reduces noise while maintaining responsiveness

**Configuration:** `strategy.lookbacks`, `strategy.lookback_weights`

### 2. Cross-Sectional Z-Scores

For each bar, the bot normalizes each asset's return relative to the universe:

```
z_i = (return_i - mean(returns)) / std(returns)
```

This removes market-wide effects (beta neutral) and identifies assets with momentum **relative to peers**.

### 3. Signal Power Amplification

Strong signals are amplified nonlinearly:

```
signal_i = sign(z_i) * |z_i|^signal_power
```

Default `signal_power = 1.35` means:
- z = 2.0 → signal = 2.7 (amplified)
- z = 0.5 → signal = 0.4 (suppressed)

**Configuration:** `strategy.signal_power`

### 4. Top-K Selection

Assets are ranked by amplified signal, and the top K go long while the bottom K go short. K is dynamic (adapts to market dispersion) or fixed.

**Configuration:** `strategy.k_min`, `strategy.k_max`, `strategy.selection.enabled`

---

## Signal Filtering

### Current Filter Stack (Simplified)

After the roadmap simplification, the bot uses a lean filter stack:

1. **Regime Filter** (EMA slope) — Only trade assets showing clear trends
   - Locked: `regime_filter.ema_len = 200` (not optimized)
   - Optimized: `regime_filter.slope_min_bps_per_day`

2. **Symbol Scoring** (Performance-based) — Downweight/ban symbols with poor performance

3. **Volatility Breakout Gate** (NEW) — Only allow new entries when volatility expands
   - Requires ATR > `expansion_mult × ATR_mean`
   - Config: `strategy.volatility_entry.*`

**Removed/Locked (No Longer Optimized):**
- **ADX Filter** — Disabled by default (removed from optimizer)
- **Meta-Labeler** — Disabled by default (removed from optimizer)
- **Majors Regime** — Disabled by default (removed from optimizer)

**Rationale:** Simpler stack reduces overfitting risk and improves robustness.

---

## Position Sizing

### Sizing Modes

The bot supports two sizing modes:

#### 1. Inverse-Volatility Sizing (Default)

**Mode:** `risk.sizing_mode = "inverse_vol"`

Equal risk contribution per asset (risk parity style). Positions are weighted inversely to volatility:

```
weight_i = (1 / volatility_i) / sum(1 / volatility_j)
```

**Rationale:** Higher volatility → smaller position (less risk), lower volatility → larger position (more capital).

**Configuration:** `strategy.vol_lookback` (volatility lookback period)

#### 2. Fixed Risk-Per-Trade (NEW)

**Mode:** `risk.sizing_mode = "fixed_r"`

Fixed percentage of equity risked per trade using ATR-based stop-loss distance:

```
position_size = (risk_per_trade_pct × equity) / stop_distance
stop_distance = ATR × atr_mult_sl
```

**Example:** With 0.5% risk per trade and 2.0× ATR stop:
- Equity: $10,000
- Risk per trade: $50
- Stop distance: $500 (2× ATR)
- Position size: $50 / $500 = 0.1 units per $1 equity

**Configuration:** `risk.sizing_mode`, `risk.risk_per_trade_pct` (default: 0.5%)

---

## Risk Management Layers

### Position-Level Risk

1. **ATR-Based Stop-Loss** — `atr_mult_sl × ATR` from entry
2. **R-Multiple Profit Targets** (NEW) — Exit portions at 2R, 3R, etc.
   - Config: `risk.profit_targets[]`
   - Example: Exit 50% at 2R, 25% at 3R
3. **Breakeven Moves** — Move stop to entry after `breakeven_after_r × R` profit (default: 0.5R)
4. **Trailing Stops** — MA-ATR trailing stops (enabled by default, `trail_atr_mult = 1.0`)
5. **Time-Based Exits** — Exit after `max_hours_in_trade` (default: 48 hours)
6. **Catastrophic Stop** — Emergency exit at `catastrophic_atr_mult × ATR`

### Portfolio-Level Risk

1. **Daily Loss Limit** — Stop trading if daily loss > threshold (default: 5%)
2. **Portfolio Drawdown Limit** — Stop trading if 30-day drawdown > threshold
3. **Max Open Positions** (NEW) — Hard cap on total positions (default: 8)
4. **Correlation Limits** (NEW) — Remove high-correlation positions
   - Config: `risk.correlation.*`
   - Removes lowest-weight positions when too many pairs exceed `max_allowed_corr`
5. **Volatility Regime-Based Leverage** (NEW) — Scale down exposure in high-vol regimes
   - Uses ATR of BTC (or proxy) vs rolling mean
   - Scales gross leverage down when ATR > `high_vol_mult × baseline`
   - Config: `risk.volatility_regime.*`
6. **Long-Term Drawdown Tracking** (NEW) — Monitor 90/180/365-day drawdowns
   - Config: `risk.long_term_dd.*`

### Execution Risk

1. **API Circuit Breaker** — Pause trading on API failures
2. **Margin Protection** — Soft/hard limits on margin usage
3. **Position Reconciliation** — Verify positions match exchange state

---

## Carry Sleeve (Optional)

The bot includes an optional **carry trading sleeve** that exploits funding rate differentials and futures basis.

### Budget Allocation

The carry sleeve uses a fixed budget fraction:

```
final_targets = (1 - carry_budget_frac) × momentum + carry_budget_frac × carry
```

**Configuration:** `strategy.carry.budget_frac` (default: 0.25, optimized in optimizer)

### Funding Carry

Ranks assets by funding rate, filters by 30-day percentile, and goes long assets with high funding (hedged delta-neutral).

### Basis Carry

Computes basis (futures - spot), ranks by annualized basis, and trades high-basis assets (hedged delta-neutral).

**Rationale:** Carry is orthogonal to momentum (uncorrelated), providing diversification.

---

## Entry/Exit Logic

### Entry Conditions

1. Asset ranked in top/bottom K by amplified signal
2. Signal >= entry threshold (default: 0.0, locked — not optimized)
3. Passes all filters (regime, symbol scoring, volatility breakout gate)
4. Not in cooldown (anti-churn)

### Exit Conditions

1. **Stop-Loss** — Price hits ATR-based stop
2. **R-Multiple Profit Targets** — Exit portions at configured R-levels
3. **Trailing Stop** — Price hits trailing stop (locks in profits)
4. **Breakeven** — Stop moved to entry after profit threshold
5. **Time-Based Exit** — Exit after max hours in trade (default: 48h)
6. **Catastrophic Stop** — Emergency exit for extreme moves
7. **No Progress** — Exit if position hasn't moved after time threshold

---

## Data Pipeline

### Historical OHLCV Cache (NEW)

The bot uses a **SQLite cache** to reduce API calls:

- **Storage:** `data/ohlcv_cache.db`
- **Behavior:** Cache checked first, missing data fetched from exchange, results stored in cache
- **Config:** `data.cache.*`

### Data Validation (NEW)

All fetched data is validated for:

- **OHLC Consistency** — Low ≤ open/close ≤ high
- **Negative Prices/Volumes** — Detected and logged
- **Gaps** — Missing bars detected vs timeframe
- **Spikes** — Extreme moves detected via z-score

**Config:** `data.validation.*`

**Behavior:** Validation logs warnings/errors but continues (non-fatal unless catastrophic).

---

## Equity History & Long-Term Tracking

### Extended Equity History (NEW)

The bot now tracks **365 days of equity history** (extended from 60 days) for long-term drawdown analysis.

### Long-Term Drawdown Metrics (NEW)

Tracks 90-day, 180-day, and 365-day drawdowns from high watermarks:

- **90-day DD** — Short-term drawdown tracking
- **180-day DD** — Medium-term drawdown tracking
- **365-day DD** — Long-term drawdown tracking

**Config:** `risk.long_term_dd.*` (optional thresholds for alerts)

---

## How It All Works Together

1. **Signals → Sizing:** Multi-lookback momentum generates signals, positions sized via inverse-vol or fixed-R
2. **Filters → Selection:** Regime filter and volatility breakout gate reduce noise, correlation limits reduce concentration
3. **Risk → Execution:** Multiple risk layers (daily loss, DD limits, max positions, vol regime) protect capital
4. **Execution → Monitoring:** Orders placed with spread guards, positions monitored for stops/TPs/exits
5. **Monitoring → State:** Equity history tracked for long-term analysis, funding costs tracked for cost awareness

**All layers work together** to create a robust, risk-managed trading system focused on **MAKE MONEY** — consistently, with controlled risk.

---

## Configuration

See [`../reference/config_reference.md`](../reference/config_reference.md) for complete parameter list.

Key config sections:
- `strategy.*` — Signal generation, filtering, sizing
- `risk.*` — Position-level and portfolio-level risk controls
- `data.*` — Historical data fetching, caching, validation
- `optimizer.*` — Optimizer service configuration

---

## Next Steps

- **Risk Management:** [`risk_management.md`](risk_management.md) — Detailed risk controls
- **Strategy Logic:** [`strategy_logic.md`](strategy_logic.md) — Detailed signal generation
- **Data Pipeline:** [`data_pipeline.md`](data_pipeline.md) — Data fetching, caching, validation
- **Config Reference:** [`../reference/config_reference.md`](../reference/config_reference.md) — All parameters

---

**Motto: MAKE MONEY** — with a clear, well-understood, and well-documented strategy. 📈

