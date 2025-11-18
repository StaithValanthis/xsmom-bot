# Strategy Improvement Roadmap: Deep Diagnostic & Design

**Date:** 2025-01-XX  
**Reviewer:** Systematic Trading Engineer & Quantitative Researcher  
**Goal:** Understand current bot, identify improvements, research robust methods, and create prioritized roadmap for **MAKE MONEY**.

---

## 1. Current Architecture & Workflow (Pipeline Map)

### Data Layer

**Historical Data Fetching:**
- **Source**: Bybit USDT-perp futures via CCXT (`src/exchange.py`)
- **Method**: `fetch_ohlcv()` with automatic pagination for limits > 1000 bars
- **Pagination**: `fetch_ohlcv_range()` for date-range queries (forward pagination, deduplication)
- **Storage**: In-memory only (pandas DataFrames), no persistent cache
- **Rate Limiting**: 200ms throttle between pagination chunks, 2s wait on rate limit errors
- **Limits**: 1000 bars per request (Bybit limit), 50,000 total safety cap, 100 max pagination requests

**Live Data:**
- **Frequency**: Every hour (at minute 1, configurable via `execution.rebalance_minute`)
- **Symbols**: Filtered by volume/price (`min_usd_volume_24h`, `min_price`, `max_symbols`)
- **Deduplication**: Removes duplicate timestamps immediately after fetch
- **Rate Limiting**: 100ms delay between symbol fetches, 1s wait on rate limit errors

**Database Storage:**
- **Optimizer DB**: SQLite (`data/optimizer.db`) - stores trials, studies, bad combinations
- **State File**: JSON (`state.json`) - positions, cooldowns, equity history, symbol stats
- **No TimescaleDB/PostgreSQL**: All historical data fetched on-demand

### Strategy/Signal Layer

**Core Signal Generation** (`src/signals.py`, `src/sizing.py`):
- **Multi-lookback momentum**: Weighted returns over [12h, 24h, 48h, 96h] with weights [0.4, 0.3, 0.2, 0.1]
- **Cross-sectional z-scores**: `z = (return - mean) / std` across universe (beta-neutral)
- **Signal power amplification**: `signal = sign(z) * |z|^signal_power` (default: 1.35)
- **Top-K selection**: Long top K, short bottom K (K dynamic: 2-6 based on dispersion)

**Signal Filters** (`src/live.py`, `src/signals.py`):
- **Regime filter**: EMA slope threshold (default: 3.5 bps/day) - blocks choppy markets
- **ADX filter**: Minimum ADX threshold (default: 28) - blocks low-trend markets
- **Symbol scoring**: Performance-based bans (win rate < 40%, PF < 1.2)
- **Meta-labeler**: ML filter on predicted win probability (optional, recently added)
- **Majors regime**: Blocks/downweights when BTC/ETH not trending
- **Microstructure gate**: Spread/depth checks before entry
- **Breadth gate**: Requires 20%+ of universe passing entry threshold

### Position Sizing & Risk

**Position Sizing** (`src/sizing.py::build_targets()`):
- **Inverse-volatility sizing**: Equal risk contribution per asset
- **Market-neutral centering**: `sum(longs) = sum(shorts)`
- **Per-asset caps**: 9% weight cap, $20k notional cap
- **Portfolio vol targeting**: Scales all positions to target 24% annualized vol (optional)
- **Kelly scaling**: Fractional Kelly based on historical win rate (optional)
- **Gross leverage cap**: 0.95 (95% gross exposure)

**Risk Controls** (`src/live.py`, `src/risk.py`, `src/risk_controller.py`):
- **Daily loss limit**: 5% trailing kill-switch (from day high)
- **Portfolio drawdown**: 15% max from 30-day high (optional)
- **Margin protection**: Soft limit (80%), hard limit (90%) with auto-close
- **API circuit breaker**: Pauses trading on excessive API failures
- **Position reconciliation**: Pauses on fetch failures
- **Emergency stop file**: File-based kill switch

**Per-Position Risk** (`src/live.py::FastSLTPThread`):
- **ATR-based stops**: `entry_price ± (atr_mult_sl × ATR)` (default: 2.0× ATR)
- **Trailing stops**: MA-ATR trailing (optional, newer) or legacy ATR-based trailing
- **Breakeven moves**: Move stop to entry after `breakeven_after_r × R` profit
- **Partial profit-taking**: Exit portion at `partial_tp_r × R` (optional)
- **Catastrophic stops**: 3.5× ATR emergency exit
- **No-progress exits**: Time-based + RR threshold (optional)

### Execution

**Order Management** (`src/exchange.py`, `src/live.py::_reconcile_open_orders()`):
- **Order type**: Limit orders, post-only (maker fees)
- **Dynamic offset**: Price offset based on spread
- **Stale order cleanup**: Cancels orders older than threshold
- **Retry logic**: `tenacity` with exponential backoff (3 attempts)
- **Partial fill handling**: **NOT EXPLICITLY HANDLED** - assumes full fills

**Position Management**:
- **Reconciliation**: On startup, reloads positions from exchange
- **Fast SL/TP thread**: Checks stops every 2s on 5-minute bars
- **Order placement**: Limit orders with reduce-only flags for exits

### Optimizer

**Optimization Pipeline** (`src/optimizer/full_cycle.py`):
- **Walk-forward optimization**: Purged segments with embargo (default: 120/30/2 days)
- **Bayesian optimization**: Optuna TPE sampler (default: 100 trials per segment)
- **Monte Carlo stress testing**: Bootstrap + cost perturbation (default: 1000 runs)
- **Database persistence**: SQLite storage for trials, studies, bad combos
- **Historical lookup**: Skips duplicate parameter combinations
- **Bad-combo filtering**: Auto-marks poor parameter regions

**Parameter Space** (`src/optimizer/bo_runner.py::define_parameter_space()`):
- **15 core parameters** (reduced from 130+):
  - Signal: `signal_power`, `lookbacks[0-2]`, `entry_zscore_min`
  - Selection: `k_min`, `k_max`
  - Filters: `regime_filter.ema_len`, `regime_filter.slope_min_bps_per_day`
  - Risk: `atr_mult_sl`, `trail_atr_mult`, `gross_leverage`, `max_weight_per_asset`
  - Sizing: `portfolio_vol_target.target_ann_vol`

### Rollout

**Staging System** (`src/rollout/`):
- **Candidate queue**: Ranked by improvement metrics
- **Staging environment**: Parallel live/staging bots (environment-specific state files)
- **Promotion logic**: Compares staging vs live metrics
- **Rollback CLI**: Manual rollback to previous config versions

### Monitoring

**Logging & Alerts**:
- **Discord notifications**: Optimizer results, daily reports, circuit breaker alerts
- **No-trade detection**: Alerts if no trades for > 4 hours
- **Cost tracking**: Funding costs tracked (approximate), fees/slippage not explicitly tracked
- **Heartbeat**: File-based heartbeat for external monitoring

---

## 2. Parameters & Optimization (Findings & Suggestions)

### Current Optimizer Search Space

**15 Core Parameters** (from `src/optimizer/bo_runner.py::define_parameter_space()`):

1. `strategy.signal_power`: [1.0, 2.0] - Signal amplification power
2. `strategy.lookbacks[0]`: [6, 24] - Short lookback (hours)
3. `strategy.lookbacks[1]`: [12, 48] - Medium lookback (hours)
4. `strategy.lookbacks[2]`: [24, 96] - Long lookback (hours)
5. `strategy.k_min`: [2, 4] - Minimum K (positions)
6. `strategy.k_max`: [4, 8] - Maximum K (positions)
7. `strategy.regime_filter.ema_len`: [100, 300] - EMA length for regime filter
8. `strategy.regime_filter.slope_min_bps_per_day`: [1.0, 5.0] - Minimum slope threshold
9. `strategy.entry_zscore_min`: [0.0, 1.0] - Entry threshold
10. `risk.atr_mult_sl`: [1.5, 3.0] - Stop-loss multiplier
11. `risk.trail_atr_mult`: [0.5, 1.5] - Trailing stop multiplier
12. `strategy.gross_leverage`: [1.0, 2.0] - Portfolio gross leverage
13. `strategy.max_weight_per_asset`: [0.10, 0.30] - Per-asset weight cap
14. `strategy.portfolio_vol_target.target_ann_vol`: [0.20, 0.50] - Target annualized volatility

**Effective Dimensionality**: 15 parameters (good reduction from 130+)

### Parameter Assessment

**✅ Keep in Optimizer (Core, High Value):**
- `signal_power` - Core signal amplification (but range may be too wide)
- `lookbacks[0-2]` - Core momentum periods (but 3 params may be redundant)
- `k_min`, `k_max` - Position count control
- `regime_filter.slope_min_bps_per_day` - Regime threshold (critical filter)
- `atr_mult_sl` - Stop-loss distance (critical risk control)
- `gross_leverage` - Portfolio exposure control
- `portfolio_vol_target.target_ann_vol` - Risk level control

**⚠️ Fix/Lock (Don't Optimize, Pick Sensible Defaults):**
- `regime_filter.ema_len` - Lock at 200 (standard EMA length, no need to optimize)
- `entry_zscore_min` - Lock at 0.0 (or 0.1) - entry threshold is micro-tuning
- `trail_atr_mult` - Lock at 1.0 or disable - trailing stops are optional, easy to overfit
- `max_weight_per_asset` - Lock at 0.10 (10%) - diversification cap, not optimization target

**❌ Remove/Deprecate (Dead or Harmful):**
- None in current 15-param space (already cleaned up)

### Critical Issues

1. **Lookback weights not optimized**: Weights [0.4, 0.3, 0.2, 0.1] are fixed. If optimizing lookbacks, should also optimize weights or use equal weights.

2. **Signal power range too wide**: [1.0, 2.0] allows extreme amplification. Should narrow to [1.0, 1.5] to prevent overfitting.

3. **Three lookback params may be redundant**: Consider optimizing only 2 lookbacks (short + long) and fixing medium as average.

4. **Missing important dimensions**:
   - **No volatility lookback optimization**: `vol_lookback` is fixed at 96 bars
   - **No regime filter enable/disable**: Regime filter is always enabled if configured
   - **No carry budget fraction**: Carry sleeve budget is fixed (not in optimizer space)

### Recommendations

**Reduce to 10-12 Core Parameters:**
1. `signal_power`: [1.0, 1.5] (narrower range)
2. `lookbacks[0]`: [6, 24] (short)
3. `lookbacks[2]`: [48, 96] (long, skip medium)
4. `k_min`: [2, 4]
5. `k_max`: [4, 8]
6. `regime_filter.slope_min_bps_per_day`: [1.0, 5.0]
7. `atr_mult_sl`: [1.5, 3.0]
8. `gross_leverage`: [0.75, 1.5] (wider range, include lower leverage)
9. `portfolio_vol_target.target_ann_vol`: [0.15, 0.40] (narrower range)
10. `vol_lookback`: [48, 144] (NEW - optimize volatility lookback)
11. `strategy.carry.budget_frac`: [0.0, 0.40] (NEW - optimize carry budget if carry enabled)

**Lock These:**
- `regime_filter.ema_len` = 200 (standard)
- `entry_zscore_min` = 0.0 (no threshold)
- `trail_atr_mult` = 0.0 (disable trailing stops, or lock at 1.0)
- `max_weight_per_asset` = 0.10 (10% cap)

---

## 3. Data Collection & Storage (Findings & Suggestions)

### Current State

**Historical Data:**
- **Fetched on-demand**: No persistent cache, re-fetches for each optimizer run
- **Pagination**: Automatic for limits > 1000 bars (5 requests for 5000 bars)
- **Deduplication**: Removes duplicates during fetch
- **Rate Limiting**: 200ms throttle, 2s wait on rate limit errors
- **Storage**: In-memory pandas DataFrames only

**Live Data:**
- **Frequency**: Every hour (at minute 1)
- **Deduplication**: Immediate after fetch
- **Rate Limiting**: 100ms between symbols, 1s on rate limit errors

**Database:**
- **Optimizer DB**: SQLite (`data/optimizer.db`) - trials, studies, bad combos
- **State File**: JSON (`state.json`) - positions, equity history (60-day rolling window)
- **No historical OHLCV cache**: Re-fetches for each backtest/optimizer run

### Issues

1. **No persistent historical cache**: Optimizer re-fetches 5000+ bars for each run (wasteful, slow)
2. **No gap detection**: Doesn't check for missing bars in historical data
3. **No data quality checks**: Doesn't validate OHLCV data (e.g., negative volumes, impossible prices)
4. **Equity history limited**: Only 60 days in state file (may not be enough for long-term drawdown tracking)

### Recommendations

**Architectural Improvements:**

1. **Add SQLite historical data cache** (`src/data/cache.py`):
   - Store OHLCV bars by (symbol, timeframe, timestamp)
   - Backfill missing bars on-demand
   - TTL-based invalidation (e.g., 1-hour TTL for recent bars)
   - **Impact**: High (reduces API calls, speeds up optimizer)
   - **Effort**: Medium

2. **Add data quality validation** (`src/data/validator.py`):
   - Check for negative volumes, impossible OHLC relationships
   - Detect gaps (missing bars)
   - Flag suspicious data (e.g., 0-volume bars, extreme price moves)
   - **Impact**: Medium (prevents bad backtests)
   - **Effort**: Small

3. **Extend equity history**:
   - Store full equity history (not just 60 days) in separate file or DB
   - Enable long-term drawdown tracking (e.g., 90-day, 180-day windows)
   - **Impact**: Medium (better risk monitoring)
   - **Effort**: Small

4. **Add data backfill service**:
   - Background service to backfill historical gaps
   - Periodic validation of data completeness
   - **Impact**: Low-Medium (data quality)
   - **Effort**: Medium

---

## 4. Trade Lifecycle: Entry, Management, Exit (Findings & Suggestions)

### Current Entry Logic

**Entry Conditions** (`src/live.py`, `src/sizing.py`):
1. Signal generation: Multi-lookback momentum → z-scores → amplification
2. Filtering: Regime, ADX, symbol scoring, meta-labeler, majors regime, microstructure
3. Top-K selection: Long top K, short bottom K
4. Position sizing: Inverse-volatility, caps, vol targeting
5. Risk checks: Daily loss, drawdown, margin, circuit breaker
6. Order placement: Limit orders (post-only)

**Entry Filters (Many):**
- Regime filter (EMA slope)
- ADX filter
- Symbol scoring (win rate, PF)
- Meta-labeler (ML prediction)
- Majors regime (BTC/ETH trend)
- Microstructure gate (spread/depth)
- Breadth gate (20%+ universe passing)
- Anti-churn cooldowns

**Issues:**
- **Too many filters**: Combined filters may block most trades
- **No explicit entry confirmation**: Relies on signal strength only
- **No volatility-based entry timing**: Doesn't wait for low volatility to enter

### Current Exit Logic

**Stop-Loss** (`src/live.py::FastSLTPThread`):
- **ATR-based**: `entry_price ± (atr_mult_sl × ATR)` (default: 2.0× ATR)
- **Trailing**: MA-ATR trailing (newer) or legacy ATR-based trailing
- **Breakeven**: Move to entry after `breakeven_after_r × R` profit
- **Catastrophic**: 3.5× ATR emergency exit

**Take-Profit:**
- **Partial TP**: Exit portion at `partial_tp_r × R` (optional, disabled by default)
- **No full TP**: Exits only via stops or signal flips (no explicit profit target)

**Other Exits:**
- **Regime flip**: Exit if EMA slope reverses (optional)
- **No progress**: Exit after time threshold + RR check (optional)
- **Max hold time**: Exit after N hours (optional)

**Issues:**
1. **No explicit profit-taking policy**: Relies on trailing stops or signal flips only
2. **Trailing stops may be too tight**: Default `trail_atr_mult = 0.0` (disabled) - if enabled, may exit winners too early
3. **No R-multiple targets**: Doesn't set explicit profit targets (e.g., 2R, 3R)
4. **Breakeven may be too early**: Default `breakeven_after_r = 0.0` (disabled)

### Recommendations

**Entry Improvements:**

1. **Simplify filter stack**:
   - Keep: Regime filter (critical), Symbol scoring (adaptive)
   - Remove or lock: ADX filter (lagging, overfit-prone), Meta-labeler (needs validation), Majors regime (redundant with regime filter)
   - **Impact**: High (reduces overfitting, increases trade frequency)
   - **Effort**: Small

2. **Add volatility-based entry timing**:
   - Wait for low volatility before entering (avoid entering during high volatility spikes)
   - **Impact**: Medium (reduces entry slippage)
   - **Effort**: Small

**Exit Improvements:**

1. **Add explicit R-multiple profit targets**:
   - Set profit targets at 2R, 3R (exit portion at each level)
   - **Impact**: High (locks in profits systematically)
   - **Effort**: Small

2. **Refine trailing stop logic**:
   - Use MA-ATR trailing (newer implementation) instead of legacy
   - Set `trail_atr_mult = 1.0` (not too tight, not too loose)
   - **Impact**: Medium (better profit protection)
   - **Effort**: Small

3. **Enable breakeven moves**:
   - Set `breakeven_after_r = 0.5` (move stop to entry after 0.5R profit)
   - **Impact**: Medium (protects capital on winners)
   - **Effort**: Small

4. **Add time-based exits**:
   - Exit if position hasn't moved after 24-48 hours (prevents dead positions)
   - **Impact**: Low-Medium (reduces opportunity cost)
   - **Effort**: Small

---

## 5. Risk Management & Portfolio Controls (Findings & Suggestions)

### Current Risk Controls

**Portfolio-Level:**
- ✅ Daily loss limit: 5% trailing kill-switch
- ✅ Portfolio drawdown: 15% max from 30-day high (optional)
- ✅ Margin protection: Soft (80%), hard (90%) limits
- ✅ API circuit breaker: Pauses on excessive failures
- ✅ Position reconciliation: Pauses on fetch failures
- ✅ Emergency stop file: File-based kill switch

**Position-Level:**
- ✅ ATR-based stops: 2.0× ATR default
- ✅ Trailing stops: Optional (disabled by default)
- ✅ Breakeven moves: Optional (disabled by default)
- ✅ Partial TP: Optional (disabled by default)
- ✅ Catastrophic stops: 3.5× ATR

**Position Sizing:**
- ✅ Per-asset caps: 9% weight, $20k notional
- ✅ Gross leverage cap: 0.95 (95%)
- ✅ Portfolio vol targeting: Optional (24% target)
- ✅ Inverse-volatility sizing: Equal risk contribution

### Gaps & Issues

1. **No risk budgeting**: All positions get same ATR-based stop (not fixed % of equity per trade)
2. **No correlation limits**: Can open positions in highly correlated assets simultaneously
3. **No max position count limit**: Can open up to `max_open_positions` (default: 12) - if all go against bot, drawdown could exceed daily limit
4. **No volatility regime-based leverage**: Doesn't reduce leverage in high-volatility regimes
5. **Funding costs not fully modeled**: Approximate tracking, not subtracted from equity in real-time

### Recommendations

**High-Priority Risk Improvements:**

1. **Add fixed risk per trade** (`src/sizing.py`):
   - Allocate fixed risk per position (e.g., 0.5% of equity per trade)
   - Adjust position size to meet risk target: `position_size = risk_per_trade / (entry_price - stop_price)`
   - **Impact**: High (consistent risk per trade, better risk management)
   - **Effort**: Medium

2. **Add correlation limits** (`src/sizing.py`):
   - Compute pairwise correlations (rolling window)
   - Limit positions in highly correlated assets (e.g., max 2 positions with corr > 0.8)
   - **Impact**: High (reduces simultaneous losses from correlated assets)
   - **Effort**: Medium

3. **Add max position count limit** (`src/live.py`):
   - Hard cap on open positions (e.g., 8 max, regardless of K selection)
   - **Impact**: Medium (prevents over-concentration)
   - **Effort**: Small

4. **Add volatility regime-based leverage** (`src/sizing.py`):
   - Reduce gross leverage when market volatility is high (e.g., scale by VIX-like metric)
   - **Impact**: Medium (reduces risk in volatile markets)
   - **Effort**: Medium

5. **Improve funding cost tracking** (`src/live.py`):
   - Fetch actual funding payments from exchange (not approximate)
   - Subtract from equity in real-time
   - **Impact**: Medium (accurate PnL tracking)
   - **Effort**: Small-Medium

---

## 6. External Research: Robust Crypto Strategies (Summary)

### Trend-Following / Time-Series Momentum

**Core Idea:**
- Buy assets with positive recent returns, sell assets with negative returns
- Exploits momentum persistence (trends continue)

**Typical Signals:**
- Moving average crossovers (e.g., 50/200 EMA)
- Breakouts above/below price ranges
- RSI momentum (RSI > 70 = overbought, RSI < 30 = oversold)

**Holding Periods:**
- Medium-term: Days to weeks
- Risk: ATR-based stops, trailing stops

**Known Pitfalls:**
- Whipsaws in choppy markets (mean-reversion kills momentum)
- Funding costs in perps (longs pay shorts in contango)
- Tail risk (momentum crashes)

**Research Findings:**
- **Academic**: Momentum effect is well-documented in crypto (Jegadeesh & Titman style)
- **Crypto-specific**: Stronger in altcoins than BTC (higher volatility = more momentum)
- **Timeframe**: 12-48 hour lookbacks work well (shorter = noise, longer = lag)

### Cross-Sectional Momentum (XSMOM)

**Core Idea:**
- Rank assets by momentum, go long top K, short bottom K
- Market-neutral (removes beta)

**Typical Signals:**
- Cross-sectional z-scores (relative momentum)
- Top/bottom decile selection
- Dynamic K based on dispersion

**Holding Periods:**
- Medium-term: Days to weeks
- Risk: ATR-based stops, portfolio-level caps

**Known Pitfalls:**
- Over-filtering (too many gates → few trades)
- High turnover (frequent rebalancing → fees)
- Correlation risk (top K may be highly correlated)

**Research Findings:**
- **Academic**: XSMOM outperforms TSMOM in crypto (Moskowitz et al. style)
- **Crypto-specific**: Works best in trending markets (regime filter critical)
- **Optimal K**: 3-6 positions (balance between diversification and signal strength)

### Carry / Funding Yield Strategies

**Core Idea:**
- Exploit funding rate differentials (long assets with high funding, hedge delta-neutral)
- Exploit futures basis (contango/backwardation)

**Typical Signals:**
- Funding rate ranking (8-hour funding)
- Basis ranking (futures - spot)
- 30-day percentile filters (only trade when funding is extreme)

**Holding Periods:**
- Long-term: Weeks to months (carry accumulates)
- Risk: Delta-neutral hedging, funding rate reversals

**Known Pitfalls:**
- Funding rate spikes (can reverse quickly)
- Basis risk (futures can gap vs spot)
- Liquidity risk (hedge leg may be illiquid)

**Research Findings:**
- **Academic**: Carry strategies are profitable in crypto (positive Sharpe, low correlation to momentum)
- **Crypto-specific**: Funding rates are more volatile than traditional markets (higher risk/reward)
- **Optimal allocation**: 20-40% of portfolio to carry (diversifies return sources)

### Volatility Breakout / Range Expansion

**Core Idea:**
- Enter when volatility breaks out of recent range (volatility expansion = trend start)
- Exit when volatility contracts (volatility contraction = trend end)

**Typical Signals:**
- ATR expansion (ATR > recent ATR mean + threshold)
- Bollinger Band breakouts (price breaks bands = volatility expansion)
- VIX-like metrics (implied volatility spikes)

**Holding Periods:**
- Short to medium-term: Hours to days
- Risk: Volatility-based stops (wider stops in high vol)

**Known Pitfalls:**
- False breakouts (volatility expands but no trend)
- Whipsaws (volatility oscillates)
- Overfitting (volatility thresholds are easy to overfit)

**Research Findings:**
- **Academic**: Volatility breakout strategies work in crypto (higher volatility = more breakouts)
- **Crypto-specific**: Works best in altcoins (higher volatility than BTC)
- **Optimal threshold**: 1.5-2.0× recent ATR mean (not too tight, not too loose)

### Volatility Selling vs Harvesting

**Volatility Selling:**
- Sell options, collect premium (market making)
- **Risk**: Tail risk (large moves can wipe out gains)
- **Not recommended**: Too risky for systematic trading

**Volatility Harvesting:**
- Rebalance more frequently in high volatility (capture volatility premium)
- **Risk**: Higher turnover (fees)
- **Research**: Works in crypto but fees can eat edge

---

## 7. Gap Analysis: Bot vs Known Profitable Methods

### Overlaps (Already Implemented)

1. **✅ Cross-Sectional Momentum (XSMOM)**: Fully implemented
   - Multi-lookback momentum
   - Cross-sectional z-scores
   - Top-K selection
   - Market-neutral design

2. **✅ Regime Filtering**: Partially implemented
   - EMA slope filter (blocks choppy markets)
   - **Gap**: No explicit volatility regime detection

3. **✅ Carry Trading**: Partially implemented
   - Funding carry sleeve (delta-neutral if hedge available)
   - Basis carry (if spot+dated futures available)
   - **Gap**: Carry budget not optimized, hedge execution not fully automated

4. **✅ ATR-Based Risk Management**: Fully implemented
   - ATR-based stops
   - Trailing stops
   - Volatility-adaptive sizing

### Gaps (Not Implemented or Weak)

1. **❌ Volatility Breakout Entry Timing**: Not implemented
   - **Current**: Enters based on momentum signal only
   - **Gap**: Doesn't wait for volatility expansion before entering
   - **Impact**: High (could reduce entry slippage, improve win rate)

2. **❌ Explicit R-Multiple Profit Targets**: Not implemented
   - **Current**: Relies on trailing stops or signal flips
   - **Gap**: No systematic profit-taking at 2R, 3R levels
   - **Impact**: High (locks in profits systematically)

3. **❌ Correlation Limits**: Not implemented
   - **Current**: Can open positions in highly correlated assets
   - **Gap**: No pairwise correlation checks
   - **Impact**: High (reduces simultaneous losses)

4. **❌ Fixed Risk Per Trade**: Not implemented
   - **Current**: Inverse-volatility sizing (equal risk contribution)
   - **Gap**: Not fixed % of equity per trade
   - **Impact**: Medium (more consistent risk management)

5. **❌ Volatility Regime-Based Leverage**: Not implemented
   - **Current**: Fixed gross leverage (or vol targeting)
   - **Gap**: Doesn't reduce leverage in high-volatility regimes
   - **Impact**: Medium (reduces risk in volatile markets)

6. **⚠️ Carry Sleeve**: Partially implemented
   - **Current**: Funding/basis carry exists but budget not optimized
   - **Gap**: Hedge execution not fully automated, budget fraction fixed
   - **Impact**: Medium (carry is profitable but underutilized)

### Current Bot Status by Strategy Archetype

| Strategy Archetype | Status | What's Needed |
|-------------------|--------|---------------|
| **Cross-Sectional Momentum** | ✅ Fully Used | Simplify filters, reduce overfitting |
| **Carry/Funding Yield** | ⚠️ Partially Used | Optimize budget, automate hedge execution |
| **Volatility Breakout** | ❌ Not Used | Add volatility expansion entry timing |
| **Regime Switching** | ✅ Fully Used | Add volatility regime detection |
| **Risk Parity Sizing** | ✅ Fully Used | Add fixed risk per trade option |

---

## 8. Prioritized Improvement Roadmap

### Priority 1: High Impact, Medium Effort (Weeks 1-2)

#### 1.1 Simplify Signal Stack (Parameters, High Impact, Small Effort)

**What:**
- Remove ADX filter from optimizer (lock at disabled or fixed threshold)
- Remove meta-labeler from optimizer (needs validation first)
- Remove majors regime filter (redundant with regime filter)
- Keep: Regime filter, Symbol scoring, Breadth gate

**Why:**
- Reduces overfitting risk (fewer filters = more robust)
- Increases trade frequency (more opportunities)
- Aligned with research: XSMOM works best with minimal filters

**Files:**
- `src/live.py` (filter logic)
- `src/optimizer/bo_runner.py` (remove from parameter space)
- `config/config.yaml.example` (document locked params)

**Impact:** High | **Effort:** Small

---

#### 1.2 Add Explicit R-Multiple Profit Targets (Entry/Exit, High Impact, Small Effort)

**What:**
- Add profit targets at 2R, 3R (exit portion at each level)
- Example: Exit 50% at 2R, exit 25% at 3R, let remainder run with trailing stop
- Config: `risk.profit_targets: [{r_multiple: 2.0, exit_pct: 0.5}, {r_multiple: 3.0, exit_pct: 0.25}]`

**Why:**
- Locks in profits systematically (prevents winners turning to losers)
- Aligned with research: R-multiple targets are robust profit-taking method
- Better than relying on trailing stops alone

**Files:**
- `src/live.py::FastSLTPThread` (add profit target checks)
- `src/config.py` (add `ProfitTargetsCfg`)
- `config/config.yaml.example` (add config)

**Impact:** High | **Effort:** Small

---

#### 1.3 Add Correlation Limits (Risk, High Impact, Medium Effort)

**What:**
- Compute pairwise correlations (rolling 48-hour window)
- Limit positions in highly correlated assets (e.g., max 2 positions with corr > 0.8)
- When selecting top K, prefer less correlated assets

**Why:**
- Prevents simultaneous losses from correlated assets (reduces tail risk)
- Aligned with research: Correlation limits are critical for portfolio risk
- Current bot can open positions in highly correlated assets (e.g., BTC/ETH)

**Files:**
- `src/sizing.py` (add correlation computation and limits)
- `src/config.py` (add `max_correlation` config)
- `config/config.yaml.example` (add config)

**Impact:** High | **Effort:** Medium

---

#### 1.4 Add Fixed Risk Per Trade Option (Risk, High Impact, Medium Effort)

**What:**
- Add option to use fixed risk per trade (e.g., 0.5% of equity per trade)
- Adjust position size to meet risk target: `position_size = risk_per_trade / (entry_price - stop_price)`
- Keep inverse-volatility as alternative (configurable)

**Why:**
- More consistent risk per trade (better risk management)
- Aligned with research: Fixed risk per trade is standard in professional trading
- Current inverse-volatility sizing doesn't guarantee fixed risk

**Files:**
- `src/sizing.py::build_targets()` (add fixed risk option)
- `src/config.py` (add `risk_per_trade_pct` config)
- `config/config.yaml.example` (add config)

**Impact:** High | **Effort:** Medium

---

### Priority 2: High Impact, Large Effort (Weeks 3-4)

#### 2.1 Add Volatility Breakout Entry Timing (Entry/Exit, High Impact, Medium Effort)

**What:**
- Wait for volatility expansion before entering (ATR > recent ATR mean + threshold)
- Enter only when volatility breaks out of recent range (volatility expansion = trend start)
- Config: `strategy.volatility_entry.enabled`, `strategy.volatility_entry.atr_expansion_mult`

**Why:**
- Reduces entry slippage (enters when volatility is expanding, not contracting)
- Aligned with research: Volatility breakout strategies work in crypto
- Current bot enters based on momentum only (may enter during volatility spikes)

**Files:**
- `src/signals.py` (add volatility expansion detection)
- `src/live.py` (add volatility gate before entry)
- `src/config.py` (add `VolatilityEntryCfg`)

**Impact:** High | **Effort:** Medium

---

#### 2.2 Optimize Carry Budget Fraction (Parameters, Medium Impact, Small Effort)

**What:**
- Add `strategy.carry.budget_frac` to optimizer parameter space ([0.0, 0.40])
- Optimize allocation between momentum and carry sleeves
- Current: Fixed at 0.35 (35% to carry)

**Why:**
- Carry is profitable but underutilized (budget is fixed, not optimized)
- Aligned with research: Optimal carry allocation is 20-40% (needs optimization)
- Diversifies return sources (momentum + carry)

**Files:**
- `src/optimizer/bo_runner.py` (add to parameter space)
- `src/config.py` (ensure carry budget is optimizable)

**Impact:** Medium | **Effort:** Small

---

#### 2.3 Add Historical Data Cache (Data, Medium Impact, Medium Effort)

**What:**
- Create SQLite cache for OHLCV bars (`data/ohlcv_cache.db`)
- Store bars by (symbol, timeframe, timestamp)
- Backfill missing bars on-demand
- TTL-based invalidation (1-hour TTL for recent bars)

**Why:**
- Reduces API calls (optimizer re-fetches 5000+ bars for each run)
- Speeds up optimizer (faster backtests)
- Aligned with best practices: Persistent cache for historical data

**Files:**
- `src/data/cache.py` (new module)
- `src/exchange.py` (use cache if available)
- `src/optimizer/backtest_runner.py` (use cache)

**Impact:** Medium | **Effort:** Medium

---

### Priority 3: Medium Impact, Small Effort (Weeks 5-6)

#### 3.1 Enable Breakeven Moves (Entry/Exit, Medium Impact, Small Effort)

**What:**
- Set `risk.breakeven_after_r = 0.5` (move stop to entry after 0.5R profit)
- Protects capital on winners (locks in risk-free trades)

**Why:**
- Prevents winners from turning to losers (breakeven stop protects capital)
- Aligned with research: Breakeven moves are standard in professional trading
- Current: Disabled by default

**Files:**
- `config/config.yaml.example` (enable breakeven)
- `src/live.py::FastSLTPThread` (already implemented, just needs config)

**Impact:** Medium | **Effort:** Small

---

#### 3.2 Refine Trailing Stop Logic (Entry/Exit, Medium Impact, Small Effort)

**What:**
- Use MA-ATR trailing (newer implementation) instead of legacy
- Set `risk.trailing_enabled = true`, `risk.trail_atr_mult = 1.0`
- Better profit protection than legacy ATR-based trailing

**Why:**
- Locks in profits as price moves favorably (trailing stop moves up)
- Aligned with research: Trailing stops are robust profit protection
- Current: Disabled by default

**Files:**
- `config/config.yaml.example` (enable trailing stops)
- `src/live.py::FastSLTPThread` (already implemented)

**Impact:** Medium | **Effort:** Small

---

#### 3.3 Add Max Position Count Limit (Risk, Medium Impact, Small Effort)

**What:**
- Hard cap on open positions (e.g., 8 max, regardless of K selection)
- Prevents over-concentration (if all positions go against bot, drawdown could exceed daily limit)

**Why:**
- Reduces tail risk (fewer positions = less simultaneous loss potential)
- Aligned with research: Position count limits are standard risk control
- Current: Can open up to `max_open_positions` (default: 12)

**Files:**
- `src/live.py` (add max position count check)
- `src/config.py` (add `max_open_positions_hard` config)

**Impact:** Medium | **Effort:** Small

---

#### 3.4 Narrow Optimizer Parameter Ranges (Parameters, Medium Impact, Small Effort)

**What:**
- Narrow `signal_power`: [1.0, 1.5] (was [1.0, 2.0])
- Narrow `portfolio_vol_target.target_ann_vol`: [0.15, 0.40] (was [0.20, 0.50])
- Remove `regime_filter.ema_len` from optimizer (lock at 200)
- Remove `entry_zscore_min` from optimizer (lock at 0.0)
- Remove `trail_atr_mult` from optimizer (lock at 1.0 or disable)
- Remove `max_weight_per_asset` from optimizer (lock at 0.10)

**Why:**
- Reduces overfitting risk (narrower ranges = less optimization space)
- Locks micro-tuning params (EMA length, entry threshold are not core)
- Aligned with research: Fewer, well-chosen params outperform many params

**Files:**
- `src/optimizer/bo_runner.py::define_parameter_space()` (update ranges, remove params)

**Impact:** Medium | **Effort:** Small

---

### Priority 4: Medium Impact, Medium Effort (Weeks 7-8)

#### 4.1 Add Volatility Regime-Based Leverage (Risk, Medium Impact, Medium Effort)

**What:**
- Compute market volatility (VIX-like metric: rolling ATR of portfolio or BTC)
- Reduce gross leverage when volatility is high (e.g., scale by `1 / (1 + vol_ratio)`)
- Config: `risk.volatility_regime.enabled`, `risk.volatility_regime.lookback_hours`, `risk.volatility_regime.scale_factor`

**Why:**
- Reduces risk in volatile markets (leverage scales down automatically)
- Aligned with research: Volatility regime-based leverage is standard risk management
- Current: Fixed gross leverage (or vol targeting, but not regime-based)

**Files:**
- `src/sizing.py` (add volatility regime detection and scaling)
- `src/config.py` (add `VolatilityRegimeCfg`)

**Impact:** Medium | **Effort:** Medium

---

#### 4.2 Improve Funding Cost Tracking (Execution, Medium Impact, Small-Medium Effort)

**What:**
- Fetch actual funding payments from exchange (not approximate)
- Subtract from equity in real-time
- Compare to backtest assumptions, alert if gap > threshold

**Why:**
- Accurate PnL tracking (funding costs can eat 10-20% annually)
- Aligned with research: Funding costs are critical in crypto perps
- Current: Approximate tracking, not subtracted from equity

**Files:**
- `src/exchange.py` (add funding history fetch)
- `src/live.py` (improve funding cost tracking)
- `src/notifications/discord_notifier.py` (add cost alerts)

**Impact:** Medium | **Effort:** Small-Medium

---

#### 4.3 Add Data Quality Validation (Data, Medium Impact, Small Effort)

**What:**
- Check for negative volumes, impossible OHLC relationships
- Detect gaps (missing bars)
- Flag suspicious data (e.g., 0-volume bars, extreme price moves)

**Why:**
- Prevents bad backtests (bad data → bad optimization)
- Aligned with best practices: Data quality validation is critical

**Files:**
- `src/data/validator.py` (new module)
- `src/exchange.py` (validate after fetch)
- `src/optimizer/backtest_runner.py` (validate before backtest)

**Impact:** Medium | **Effort:** Small

---

### Priority 5: Low Impact, Small Effort (Weeks 9+)

#### 5.1 Add Time-Based Exits (Entry/Exit, Low-Medium Impact, Small Effort)

**What:**
- Exit if position hasn't moved after 24-48 hours (prevents dead positions)
- Config: `risk.max_hours_in_trade` (already exists, just needs to be enabled)

**Why:**
- Reduces opportunity cost (dead positions tie up capital)
- Aligned with research: Time-based exits are standard in momentum strategies

**Files:**
- `config/config.yaml.example` (enable time-based exits)
- `src/live.py::FastSLTPThread` (already implemented)

**Impact:** Low-Medium | **Effort:** Small

---

#### 5.2 Extend Equity History (Risk, Low-Medium Impact, Small Effort)

**What:**
- Store full equity history (not just 60 days) in separate file or DB
- Enable long-term drawdown tracking (e.g., 90-day, 180-day windows)

**Why:**
- Better risk monitoring (long-term drawdown tracking)
- Aligned with best practices: Full equity history for risk analysis

**Files:**
- `src/live.py` (extend equity history storage)
- `src/risk.py` (add long-term drawdown checks)

**Impact:** Low-Medium | **Effort:** Small

---

## Summary: Top 10 Improvements

| Priority | Name | Category | Impact | Effort | Description |
|----------|------|----------|--------|--------|-------------|
| 1 | Simplify Signal Stack | Parameters | High | Small | Remove ADX, meta-labeler, majors regime from optimizer |
| 2 | Add R-Multiple Profit Targets | Entry/Exit | High | Small | Exit 50% at 2R, 25% at 3R, let remainder run |
| 3 | Add Correlation Limits | Risk | High | Medium | Limit positions in highly correlated assets (max 2 with corr > 0.8) |
| 4 | Add Fixed Risk Per Trade | Risk | High | Medium | Option to use fixed 0.5% risk per trade instead of inverse-vol |
| 5 | Add Volatility Breakout Entry | Entry/Exit | High | Medium | Wait for volatility expansion before entering |
| 6 | Optimize Carry Budget | Parameters | Medium | Small | Add carry budget fraction to optimizer ([0.0, 0.40]) |
| 7 | Add Historical Data Cache | Data | Medium | Medium | SQLite cache for OHLCV bars (reduces API calls) |
| 8 | Enable Breakeven Moves | Entry/Exit | Medium | Small | Move stop to entry after 0.5R profit |
| 9 | Refine Trailing Stops | Entry/Exit | Medium | Small | Use MA-ATR trailing, set multiplier to 1.0 |
| 10 | Narrow Parameter Ranges | Parameters | Medium | Small | Narrow signal_power, vol_target ranges, lock micro-tuning params |

---

## Implementation Order

**Week 1-2 (High Impact, Quick Wins):**
1. Simplify signal stack (remove ADX, meta-labeler, majors regime)
2. Add R-multiple profit targets
3. Enable breakeven moves
4. Refine trailing stops
5. Narrow parameter ranges

**Week 3-4 (High Impact, Medium Effort):**
6. Add correlation limits
7. Add fixed risk per trade option
8. Add volatility breakout entry timing

**Week 5-6 (Medium Impact, Infrastructure):**
9. Add historical data cache
10. Optimize carry budget fraction
11. Add max position count limit

**Week 7+ (Polish & Monitoring):**
12. Add volatility regime-based leverage
13. Improve funding cost tracking
14. Add data quality validation

---

**Motto: MAKE MONEY** — with robust, risk-managed, data-driven, and maintainable improvements. 📈

