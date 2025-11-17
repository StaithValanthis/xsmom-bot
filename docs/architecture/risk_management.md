# Risk Management

## Overview

**xsmom-bot** implements multiple layers of risk management to protect capital and limit losses:

1. **Portfolio-Level Limits** - Daily loss limits, drawdown limits, kill-switches
2. **Position-Level Limits** - Stop-loss, take-profit, trailing stops
3. **Position Sizing Controls** - Per-asset caps, portfolio caps, volatility targeting
4. **Trade Throttling** - Anti-churn cooldowns, streak pauses

---

## Portfolio-Level Risk Controls

### Daily Loss Limit

**Mechanism:**
1. Track equity at start of UTC day (`day_start_equity`)
2. Track highest equity during day (`day_high_equity`)
3. Stop trading if daily loss > threshold (default: 5% of equity)
4. Optional: Trailing kill-switch (stop if loss from daily high > threshold)

**Configuration:**
- `risk.max_daily_loss_pct`: Daily loss limit (default: 5.0%)
- `risk.use_trailing_killswitch`: Use trailing kill-switch (default: false)

**Implementation:**
- `src/risk.py::kill_switch_should_trigger()` - Checks daily loss limits
- `src/live.py::run_live()` - Monitors daily loss every cycle

**Rationale:**
- Prevents catastrophic daily losses (protects capital)
- Enables recovery (allows trading to resume after pause)
- Trailing kill-switch protects unrealized gains (stops if loss from daily high)

**Example:**
- Start equity: $10,000
- Max daily loss: 5% = $500
- Stop trading if equity < $9,500

---

### Portfolio Drawdown Limit ✅

**Status:** ✅ **Implemented** (MAKE MONEY hardening)

**Mechanism:**
1. Track equity history over rolling window (default: 30 days)
2. Find high watermark within window
3. Stop trading if drawdown from high watermark > threshold (e.g., 15%)
4. Resume trading when drawdown recovers to 80% of threshold

**Configuration:**
- `risk.max_portfolio_drawdown_pct`: Max drawdown threshold (default: 0.0 = disabled, e.g., 15.0 for 15%)
- `risk.portfolio_dd_window_days`: Lookback window (default: 30 days)

**Implementation:**
- `src/risk.py::check_max_portfolio_drawdown()` - Checks portfolio drawdown
- `src/live.py::run_live()` - Monitors drawdown every cycle

**Rationale:**
- Prevents slow death from many small losses
- Protects capital during extended losing periods
- Enables recovery (resumes trading when drawdown recovers)

**Example:**
- High watermark (30-day): $10,000
- Max drawdown: 15% = $1,500
- Stop trading if equity < $8,500
- Resume if equity recovers to > $8,800 (80% of threshold)

---

### API Circuit Breaker ✅

**Status:** ✅ **Implemented** (MAKE MONEY hardening)

**Mechanism:**
1. Track API errors in sliding window (default: 5 minutes)
2. Trip circuit breaker if error rate exceeds threshold (default: 5 errors in 5 minutes)
3. Pause trading during cooldown period (default: 10 minutes)
4. Auto-resume after cooldown expires

**Configuration:**
- `risk.api_circuit_breaker.enabled`: Enable circuit breaker (default: true)
- `risk.api_circuit_breaker.max_errors`: Max errors in window (default: 5)
- `risk.api_circuit_breaker.window_seconds`: Error window (default: 300 = 5 min)
- `risk.api_circuit_breaker.cooldown_seconds`: Cooldown period (default: 600 = 10 min)

**Implementation:**
- `src/risk_controller.py::APICircuitBreaker` - Circuit breaker logic
- `src/exchange.py::ExchangeWrapper` - Tracks API errors
- `src/live.py::run_live()` - Checks circuit breaker before trading

**Rationale:**
- Prevents trading with stale state during API failures
- Protects against position drift (bot thinks flat but has positions)
- Prevents duplicate orders during exchange downtime

**Example:**
- 5 API errors in 5 minutes → circuit breaker trips
- Trading paused for 10 minutes
- After cooldown, circuit breaker resets

---

### Margin Call Protection ✅

**Status:** ✅ **Implemented** (MAKE MONEY hardening)

**Mechanism:**
1. Fetch margin ratio from exchange (used_margin / equity)
2. Soft limit: Pause new trades if margin usage > threshold (default: 80%)
3. Hard limit: Close all positions if margin usage > threshold (default: 90%)

**Configuration:**
- `risk.margin_soft_limit_pct`: Soft limit (default: 0.0 = disabled, e.g., 80.0 for 80%)
- `risk.margin_hard_limit_pct`: Hard limit (default: 0.0 = disabled, e.g., 90.0 for 90%)
- `risk.margin_action`: Action on hard limit (default: "pause", or "close")

**Implementation:**
- `src/exchange.py::get_margin_ratio()` - Fetches margin ratio
- `src/risk_controller.py::check_margin_ratio()` - Checks limits
- `src/live.py::run_live()` - Monitors margin every cycle

**Rationale:**
- Prevents exchange liquidations (worse prices, fees)
- Protects capital during high leverage periods
- Soft limit prevents reaching hard limit

**Example:**
- Equity: $10,000
- Used margin: $8,500
- Margin usage: 85%
- Soft limit: 80% → Pause new trades
- Hard limit: 90% → Close all positions

---

### Position Reconciliation on Errors ✅

**Status:** ✅ **Implemented** (MAKE MONEY hardening)

**Mechanism:**
1. If `fetch_positions()` fails, set `reconciliation_failed = True`
2. Pause trading until reconciliation succeeds
3. Periodic reconciliation every cycle (checks positions match exchange)

**Implementation:**
- `src/live.py::run_live()` - Forces reconciliation on API errors
- `src/exchange.py::fetch_positions()` - Tracks errors in circuit breaker

**Rationale:**
- Prevents position drift (bot thinks flat but has positions)
- Prevents duplicate orders (doubles exposure)
- Ensures state matches exchange reality

**Example:**
- `fetch_positions()` fails → reconciliation_failed = True
- Trading paused
- Next successful fetch → reconciliation_failed = False
- Trading resumes

---

## Position-Level Risk Controls

### Stop-Loss (ATR-Based)

**Mechanism:**
1. Compute ATR (Average True Range) for each asset (default: 28 bars)
2. Set stop-loss at: `entry_price ± (atr_mult_sl × ATR)`
3. Exit position if price hits stop level

**Configuration:**
- `risk.atr_mult_sl`: Stop-loss multiplier (default: 2.0)
- `risk.atr_len`: ATR period (default: 28)

**Implementation:**
- `src/signals.py::compute_atr()` - Computes ATR
- `src/live.py::FastSLTPThread` - Monitors stops every 2 seconds (background thread)

**Rationale:**
- Limits loss per trade (ATR-based stops adapt to volatility)
- Higher volatility → wider stops (avoids premature exits)
- Lower volatility → tighter stops (protects capital)

**Example:**
- Entry price: $50,000
- ATR: $500
- Stop-loss multiplier: 2.0
- Stop level: $50,000 - (2.0 × $500) = $49,000
- Max loss: $1,000 per unit (2% of entry)

---

### Trailing Stop (Optional)

**Mechanism:**
1. Track highest price since entry (for longs) or lowest price (for shorts)
2. Set trailing stop at: `price_high - (trail_atr_mult × ATR)` (for longs)
3. Move trailing stop up as price moves favorably (never down)
4. Exit position if price hits trailing stop level

**Configuration:**
- `risk.trailing_enabled`: Enable trailing stops (default: false)
- `risk.trail_atr_mult`: Trailing stop multiplier (default: 0.0)

**Implementation:**
- `src/live.py::FastSLTPThread` - Monitors trailing stops every 2 seconds

**Rationale:**
- Locks in profits as price moves favorably (trailing stop moves up)
- Protects unrealized gains (exits if price reverses)
- Never reduces position profit (trailing stop never moves down)

**Example:**
- Entry price: $50,000
- ATR: $500
- Trailing stop multiplier: 1.5
- Price moves to $52,000 → Trailing stop: $52,000 - (1.5 × $500) = $51,250
- Price reverses to $51,200 → Exit at $51,250 (locked in $1,250 profit)

---

### Breakeven Move (Optional)

**Mechanism:**
1. Track profit since entry (in units of R, where R = entry_price - stop_price)
2. Move stop to entry price (breakeven) after `breakeven_after_r × R` profit
3. Exit position if price hits breakeven stop (protects capital)

**Configuration:**
- `risk.breakeven_after_r`: Breakeven threshold (default: 0.0 = disabled)

**Implementation:**
- `src/live.py::FastSLTPThread` - Monitors breakeven moves every 2 seconds

**Rationale:**
- Protects capital once position is in profit (breakeven stop prevents losses)
- Locks in risk-free trade (exits at entry price if price reverses)

**Example:**
- Entry price: $50,000
- Stop price: $49,000
- R = $50,000 - $49,000 = $1,000
- Breakeven threshold: 0.60×R = $600
- Price moves to $50,600 → Move stop to $50,000 (breakeven)
- Price reverses to $49,900 → Exit at $50,000 (breakeven, no loss)

---

### Partial Profit-Taking (Optional)

**Mechanism:**
1. Track profit since entry (in units of R)
2. Exit portion of position at `partial_tp_r × R` profit (default: 0.75×R)
3. Keep remaining position open (let winners run)

**Configuration:**
- `risk.partial_tp_enabled`: Enable partial TP (default: false)
- `risk.partial_tp_r`: Partial TP threshold (default: 0.0)
- `risk.partial_tp_size`: Portion to exit (default: 0.0)

**Implementation:**
- `src/live.py::FastSLTPThread` - Monitors partial TP every 2 seconds

**Rationale:**
- Locks in profits while letting winners run (partial exit at TP level)
- Reduces exposure to reversals (smaller position size after partial TP)
- Maintains upside potential (keeps portion of position open)

**Example:**
- Entry price: $50,000
- Stop price: $49,000
- R = $1,000
- Partial TP threshold: 0.75×R = $750
- Partial TP size: 50% of position
- Price moves to $50,750 → Exit 50% of position (locks in $375 profit)
- Keep 50% open (let winners run)

---

### Catastrophic Stop (Emergency Exit)

**Mechanism:**
1. Compute catastrophic stop at: `entry_price ± (catastrophic_atr_mult × ATR)`
2. Default: 3.5× ATR (wider than normal stop-loss)
3. Exit position immediately if price hits catastrophic stop (emergency exit)

**Configuration:**
- `risk.catastrophic_atr_mult`: Catastrophic stop multiplier (default: 3.5)

**Implementation:**
- `src/live.py::FastSLTPThread` - Monitors catastrophic stops every 2 seconds

**Rationale:**
- Emergency exit for extreme moves (wider than normal stop-loss)
- Prevents catastrophic losses (exits before position becomes unmanageable)
- Last resort (only triggers in extreme scenarios)

**Example:**
- Entry price: $50,000
- ATR: $500
- Catastrophic stop multiplier: 3.5
- Catastrophic stop level: $50,000 - (3.5 × $500) = $48,250
- Max loss: $1,750 per unit (3.5% of entry)

---

## Position Sizing Controls

### Per-Asset Caps

**Mechanism:**
1. Cap each asset's weight at threshold (default: 9% of portfolio)
2. Redistribute excess to other positions
3. Apply absolute notional cap (default: $20k per asset)

**Configuration:**
- `strategy.max_weight_per_asset`: Per-asset weight cap (default: 0.09)
- `liquidity.notional_cap_usdt`: Absolute notional cap (default: 20000.0)

**Implementation:**
- `src/sizing.py::build_targets()` - Applies per-asset caps

**Rationale:**
- Prevents over-concentration in one asset (diversification)
- Limits exposure to illiquid assets (absolute notional cap)
- Maintains risk management (no single asset dominates portfolio)

**Example:**
- Portfolio equity: $10,000
- Per-asset cap: 9% = $900
- Absolute cap: $20,000
- Asset weight: 15% = $1,500 → Capped at $900 (9%)

---

### Portfolio Gross Leverage Cap

**Mechanism:**
1. Limit total long + short exposure as fraction of equity
2. Default: 0.95 (95% gross leverage)
3. Normalize all positions to meet gross leverage cap

**Configuration:**
- `strategy.gross_leverage`: Portfolio gross leverage cap (default: 0.95)

**Implementation:**
- `src/sizing.py::build_targets()` - Applies gross leverage cap

**Rationale:**
- Limits total portfolio exposure (both long and short)
- Prevents over-leverage (maintains capital cushion)
- Risk management (caps total risk)

**Example:**
- Portfolio equity: $10,000
- Gross leverage cap: 0.95 = $9,500
- Current exposure: $11,000 → Normalized to $9,500 (0.95×)

---

### Portfolio Volatility Targeting

**Mechanism:**
1. Measure recent portfolio volatility (default: 72 hours)
2. Scale all positions up/down to target volatility (default: 24% annualized)
3. Apply min/max scale factors to prevent extreme scaling (default: 0.6-1.4×)

**Configuration:**
- `strategy.portfolio_vol_target.enabled`: Enable vol targeting (default: false)
- `strategy.portfolio_vol_target.target_ann_vol`: Target annualized volatility (default: 0.24)
- `strategy.portfolio_vol_target.lookback_hours`: Volatility lookback (default: 72)
- `strategy.portfolio_vol_target.min_scale`: Minimum scale factor (default: 0.6)
- `strategy.portfolio_vol_target.max_scale`: Maximum scale factor (default: 1.4)

**Implementation:**
- `src/sizing.py::build_targets()` - Applies volatility targeting

**Rationale:**
- Maintains consistent risk level across market regimes (volatility targeting)
- Scales up in low-volatility markets (more positions)
- Scales down in high-volatility markets (less risk)

**Example:**
- Target volatility: 24% annualized
- Recent portfolio volatility: 18% annualized (too low)
- Scale factor: 24% / 18% = 1.33×
- Scale all positions by 1.33× (increase exposure)

---

## Trade Throttling

### Anti-Churn Cooldowns

**Mechanism:**
1. Track cooldowns per symbol (ban list, trade throttling)
2. Prevent re-entry into same symbol for `cooldown_minutes` after exit
3. Optional: Extended cooldown after stop-loss (prevent re-entry after loss)

**Configuration:**
- `risk.anti_churn.enabled`: Enable anti-churn (default: true)
- `risk.anti_churn.cooldown_minutes`: Cooldown after exit (default: 20)
- `risk.anti_churn.after_stop_cooldown_minutes`: Cooldown after stop (default: 120)

**Implementation:**
- `src/anti_churn.py::ReEntryGuard` - Manages cooldowns

**Rationale:**
- Prevents overtrading (cooldowns reduce trade frequency)
- Avoids re-entry into losing symbols (extended cooldown after stops)
- Reduces costs (fewer trades = lower fees)

**Example:**
- Exit position in BTC/USDT
- Cooldown: 20 minutes
- Prevent re-entry into BTC/USDT for 20 minutes

---

### Streak Pause

**Mechanism:**
1. Track consecutive losses per symbol (loss streak)
2. Pause trading in symbol if loss streak >= threshold (default: 2 losses)
3. Resume trading after `streak_pause_minutes` (default: 180 minutes)

**Configuration:**
- `risk.anti_churn.streak_pause_after_losses`: Loss streak threshold (default: 2)
- `risk.anti_churn.streak_pause_minutes`: Pause duration (default: 180)

**Implementation:**
- `src/anti_churn.py::ReEntryGuard` - Manages streak pauses

**Rationale:**
- Prevents trading in symbols with recent losses (avoid bad actors)
- Reduces exposure to symbols with poor performance (streak pause)
- Enables recovery (resumes trading after pause)

**Example:**
- BTC/USDT: 2 consecutive losses
- Streak pause threshold: 2 losses
- Pause trading in BTC/USDT for 180 minutes

---

## Risk Management Flow

```
1. Position Entry
   ↓
2. Set Initial Stop-Loss (ATR-based)
   ↓
3. Monitor Position (every 2 seconds):
   ├─ Check stop-loss → Exit if triggered
   ├─ Check trailing stop → Exit if triggered
   ├─ Check breakeven → Move stop to entry if profit > threshold
   ├─ Check partial TP → Exit portion if profit > threshold
   └─ Check catastrophic stop → Exit if triggered
   ↓
4. Position Exit
   ↓
5. Apply Anti-Churn:
   ├─ Set cooldown (prevent re-entry for N minutes)
   └─ Update streak counter (pause if streak >= threshold)
```

---

## Risk Configuration

### Recommended Settings

**Conservative (Lower Risk):**
```yaml
risk:
  max_daily_loss_pct: 3.0          # Lower daily loss limit
  atr_mult_sl: 2.5                  # Wider stops
  trail_atr_mult: 2.0               # Wider trailing stops
  gross_leverage: 0.75              # Lower leverage
  max_weight_per_asset: 0.06        # Lower per-asset cap
```

**Moderate (Default):**
```yaml
risk:
  max_daily_loss_pct: 5.0          # Standard daily loss limit
  atr_mult_sl: 2.0                  # Standard stops
  trail_atr_mult: 1.5               # Standard trailing stops
  gross_leverage: 0.95              # Standard leverage
  max_weight_per_asset: 0.09        # Standard per-asset cap
```

**Aggressive (Higher Risk):**
```yaml
risk:
  max_daily_loss_pct: 7.0          # Higher daily loss limit
  atr_mult_sl: 1.5                  # Tighter stops
  trail_atr_mult: 1.0               # Tighter trailing stops
  gross_leverage: 1.5               # Higher leverage
  max_weight_per_asset: 0.15        # Higher per-asset cap
```

**⚠️ Warning:** Aggressive settings increase risk. Use with caution.

---

## Risk Monitoring

### Daily Metrics

**Tracked:**
- `day_start_equity`: Equity at start of UTC day
- `day_high_equity`: Highest equity during day
- `current_equity`: Current equity
- `daily_pnl`: Change in equity since start of day
- `daily_pnl_pct`: Daily return percentage

**Monitored:**
- Daily loss limit (stop trading if daily_pnl_pct < -max_daily_loss_pct)
- Trailing kill-switch (stop if current_equity < day_high_equity - threshold)

### Position-Level Metrics

**Tracked:**
- Entry price, stop price, current price
- Profit/loss (absolute and in units of R)
- Time in position (minutes/hours)
- ATR (current and entry-time)

**Monitored:**
- Stop-loss triggers (exit if price hits stop)
- Trailing stop triggers (exit if price hits trailing stop)
- Breakeven moves (move stop to entry if profit > threshold)
- Partial TP triggers (exit portion if profit > threshold)

---

## Next Steps

- **Config System**: [`config_system.md`](config_system.md) - How risk parameters control behavior
- **Strategy Logic**: [`strategy_logic.md`](strategy_logic.md) - How risk controls integrate with strategy
- **Knowledge Base**: [`../kb/framework_overview.md`](../kb/framework_overview.md) - Complete framework map

---

**Motto: MAKE MONEY** — but with robust risk management that protects capital. 📈

