# Framework Overview

**Last updated:** 2025-11-17

## Introduction

This document provides a **complete overview** of xsmom-bot's framework: architecture, strategy, risk management, optimizer, and operational model.

**Target audience:**
- New quant developers joining the project
- Future you returning in 6-12 months
- Operators who need deep understanding of the system

**Purpose:**
- Explain **what** the framework does
- Explain **why** design choices were made
- Provide **cross-references** to detailed documentation

---

## Framework Philosophy

**Motto: MAKE MONEY** — but with:

1. **Robustness over complexity** - Simple, well-tested strategies beat fragile, overfitted ones
2. **Risk management first** - Never risk more than you can afford to lose
3. **Automation everywhere** - Minimize manual intervention
4. **Clear documentation** - Easy to understand, maintain, and extend
5. **Continuous improvement** - Automated optimization, but with safety guards

---

## High-Level Architecture

### System Components

```
┌─────────────────────────────────────────────────────────────┐
│                    XSMOM-BOT FRAMEWORK                      │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  ┌──────────────┐      ┌──────────────┐      ┌──────────┐ │
│  │   Exchange   │ ───► │   Signals    │ ───► │  Sizing  │ │
│  │   (Bybit)    │      │  Generator   │      │  Engine  │ │
│  └──────────────┘      └──────────────┘      └──────────┘ │
│       │                    │                    │           │
│       │                    ▼                    ▼           │
│       │          ┌──────────────────────────┐              │
│       │          │       Filters            │              │
│       │          │ (Regime, ADX, Symbol)    │              │
│       │          └──────────────────────────┘              │
│       │                    │                                │
│       │                    ▼                                │
│       │          ┌──────────────────────────┐              │
│       └────────► │    Risk Management       │              │
│                  │ (Kill-switch, Stops)     │              │
│                  └──────────────────────────┘              │
│                             │                                │
│                             ▼                                │
│                  ┌──────────────────────────┐              │
│                  │    Order Execution       │              │
│                  │ (Limit orders, Post-only)│              │
│                  └──────────────────────────┘              │
│                                                              │
│  ┌──────────────────────────────────────────────────────┐  │
│  │         Optimizer (Walk-Forward + Bayesian)          │  │
│  │  • Runs weekly (systemd timer)                         │  │
│  │  • Optimizes 18 core parameters                        │  │
│  │  • Deploys new configs with versioning                 │  │
│  └──────────────────────────────────────────────────────┘  │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

---

## Strategy Families

### Primary Strategy: Cross-Sectional Momentum (XSMOM)

**Core Concept:**
1. Rank assets by momentum **relative to each other** (cross-sectional ranking)
2. Go **long** top K assets (strongest momentum)
3. Go **short** bottom K assets (weakest momentum)
4. Maintain **market neutrality** (long/short balance)

**Why XSMOM?**
- **Market neutral**: Removes market beta (profit from relative performance, not market direction)
- **Robust**: Cross-sectional ranking is more stable than absolute returns
- **Adaptive**: Dynamic K selection adapts to market dispersion
- **Risk-managed**: Inverse-volatility sizing maintains equal risk contribution

### Secondary Strategy: Time-Series Momentum (TSMOM) - Optional

**Regime Switching:**
- Dynamically switches between XSMOM and TSMOM based on market conditions
- **XSMOM**: High correlations + strong majors trend → rank relative momentum
- **TSMOM**: Low correlations + weak majors trend → trade absolute momentum

**Rationale:**
- Adapts to market regime (trending vs choppy)
- XSMOM works best in correlated, trending markets
- TSMOM works best in uncorrelated, choppy markets

### Tertiary Strategy: Carry Trading - Optional

**Carry Sleeve:**
- Funding carry (exploits funding rate differentials)
- Basis carry (exploits futures basis)
- Delta-neutral hedging (removes market risk)
- Budget split: 20% carry, 80% momentum (configurable)

**Rationale:**
- Diversifies return sources (momentum + carry)
- Carry is orthogonal to momentum (uncorrelated)
- Delta-neutral hedging removes market risk

---

## Signal Generation Flow

### 1. Multi-Lookback Momentum

**Process:**
1. Compute returns over multiple lookback periods (default: [12h, 24h, 48h, 96h])
2. Weight each lookback (default: [0.4, 0.3, 0.2, 0.1] - longer lookbacks get lower weights)
3. Compute weighted average return per asset

**Rationale:**
- Reduces noise (weighted combination filters out short-term noise)
- Captures trends (long lookbacks capture persistent trends)
- Maintains responsiveness (short lookbacks capture recent momentum)

### 2. Cross-Sectional Z-Scores

**Process:**
1. For each bar, compute returns for all assets
2. Compute cross-sectional mean and std (across all assets)
3. Normalize each asset's return to z-score: `z = (return - mean) / std`

**Rationale:**
- Normalizes returns relative to the universe (removes market-wide effects)
- Identifies assets with momentum **relative to peers** (not absolute momentum)
- Beta neutral (removes market beta)

### 3. Signal Power Amplification

**Process:**
1. Apply nonlinear amplification: `signal = sign(z) * |z|^power`
2. Default power: `signal_power = 1.35`

**Rationale:**
- Amplifies strong signals (z > 1.0) more than weak signals
- Suppresses noise (z < 0.5) more aggressively
- Power > 1.0 creates convexity (strong signals dominate)

### 4. Top-K Selection

**Process:**
1. Rank assets by amplified signal (descending)
2. Select top K assets → **long positions**
3. Select bottom K assets → **short positions**
4. K is dynamic: adapts to market dispersion (optional)

**Dynamic K Logic:**
- **High dispersion** (z-scores spread out) → Wider K (more positions)
- **Low dispersion** (z-scores clustered) → Tighter K (fewer positions)

**Rationale:**
- Adapts to market regime (trending vs choppy)
- More positions when clear trends, fewer when noisy
- Maintains diversification while reducing noise

See [`../architecture/strategy_logic.md`](../architecture/strategy_logic.md) for detailed signal generation flow.

---

## Signal Filtering

### Regime Filter (EMA Slope)

**Process:**
1. Compute EMA(close, length) for each asset (default: 200 bars)
2. Compute EMA slope (change over last N bars, normalized to bps/day)
3. Only trade assets with slope >= threshold (default: 3.5 bps/day)

**Rationale:**
- Only trade when market shows clear trend (avoids choppy markets)
- Reduces whipsaws (fewer false signals in trendless markets)
- Improves win rate (higher win rate in trending markets)

### ADX Filter (Optional)

**Process:**
1. Compute ADX (Average Directional Index) for each asset
2. Only trade assets with ADX >= threshold (default: 28)
3. Optional: Require rising ADX (trend strengthening)

**Rationale:**
- ADX measures trend strength (not direction)
- High ADX (>25) = strong trend (good for trading)
- Low ADX (<20) = weak trend (avoid)

**Note:** ADX is a lagging indicator. Use with caution (overfitting risk).

### Symbol Scoring (Performance-Based Filtering)

**Process:**
1. Track per-symbol trade statistics (win rate, profit factor, PnL)
2. Downweight or ban symbols with poor performance
3. EMA-smooth statistics to reduce noise

**Rationale:**
- Prevents trading assets with consistently poor performance
- Adaptive filtering (learns from live trading)
- Reduces exposure to bad actors

### Time-of-Day Whitelist (Optional)

**Process:**
1. Track per-hour trade performance
2. Only trade during "good hours" (profitable on average)
3. Downweight or skip "bad hours" (unprofitable on average)

**Rationale:**
- Some hours are more profitable than others (liquidity, volatility)
- Avoids trading during low-liquidity hours
- Adaptive filtering (learns from live trading)

**Note:** High overfitting risk. Use fixed blackout hours if needed.

---

## Position Sizing

### 1. Inverse-Volatility Sizing

**Process:**
1. Compute volatility for each asset (std of returns over vol_lookback, default: 96 bars)
2. Weight inversely to volatility: `weight_i = (1 / volatility_i) / sum(1 / volatility_j)`
3. Normalize to portfolio gross leverage

**Rationale:**
- Equal risk contribution per asset (risk parity style)
- Higher volatility → smaller position (less risk)
- Lower volatility → larger position (more capital)

### 2. Market Neutrality

**Process:**
1. Compute long weights (top K assets)
2. Compute short weights (bottom K assets)
3. Ensure long/short balance: `sum(longs) = sum(shorts)`

**Rationale:**
- Removes market beta (profit from relative performance, not market direction)
- Reduces exposure to market crashes
- Focuses on cross-sectional alpha

### 3. Per-Asset Caps

**Process:**
1. Cap each asset's weight at threshold (default: 9% of portfolio)
2. Redistribute excess to other positions
3. Apply absolute notional cap (default: $20k per asset)

**Rationale:**
- Prevents over-concentration in one asset
- Limits exposure to illiquid assets
- Maintains diversification

### 4. Portfolio Volatility Targeting

**Process:**
1. Measure recent portfolio volatility (default: 72 hours)
2. Scale all positions up/down to target volatility (default: 24% annualized)
3. Apply min/max scale factors to prevent extreme scaling

**Rationale:**
- Maintains consistent risk level across market regimes
- Scales up in low-volatility markets (more positions)
- Scales down in high-volatility markets (less risk)

### 5. Kelly Scaling (Optional)

**Process:**
1. Estimate win rate and win/loss ratio per asset (from historical trades)
2. Compute Kelly fraction: `f = (p * win - (1-p) * loss) / win`
3. Scale positions by fractional Kelly (default: 0.5× = half-Kelly)

**Rationale:**
- Optimal position sizing based on historical performance
- Scales up high-conviction positions (high win rate)
- Scales down low-conviction positions (low win rate)

**Note:** Overlaps with vol targeting. Choose one or combine carefully.

See [`../architecture/strategy_logic.md`](../architecture/strategy_logic.md) for detailed sizing logic.

---

## Risk Management

### Daily Loss Limits

**Process:**
1. Track equity at start of UTC day (`day_start_equity`)
2. Track highest equity during day (`day_high_equity`)
3. Stop trading if daily loss > threshold (default: 5% of equity)
4. Optional: Trailing kill-switch (stop if loss from daily high > threshold)

**Rationale:**
- Prevents catastrophic daily losses
- Protects capital during bad days
- Enables recovery (allows trading to resume after pause)

### Stop-Loss / Take-Profit

**ATR-Based Stops:**
- **Initial stop**: `entry_price ± (atr_mult_sl × ATR)` (default: 2.0× ATR)
- **Trailing stop**: Stop moves up as price moves favorably (default: `trail_atr_mult × ATR`)
- **Breakeven**: Move stop to entry price after `breakeven_after_r × R` (default: 0.60× R)

**Partial Profit-Taking:**
- Exit portion of position at `partial_tp_r × R` (default: 0.75× R)
- Lock in profits while letting winners run

**Rationale:**
- Limits losses per trade (ATR-based stops)
- Locks in profits (trailing stops, partial TP)
- Reduces drawdown (breakeven moves, partial TP)

### Position-Level Risk Controls

**Per-Asset Caps:**
- Maximum weight per asset (default: 9% of portfolio)
- Maximum notional per asset (default: $20k)

**Portfolio-Level Caps:**
- Gross leverage cap (default: 95% of equity)
- Volatility target (default: 24% annualized)

**Rationale:**
- Prevents over-concentration (per-asset caps)
- Limits total exposure (portfolio-level caps)
- Maintains diversification (volatility targeting)

See [`../architecture/risk_management.md`](../architecture/risk_management.md) for detailed risk logic.

---

## Execution & Order Routing

### Order Types

**Limit Orders:**
- Default order type (better fees, may not fill)
- Post-only (ensures maker fee)
- Dynamic offset based on spread (optional)

**Market Orders:**
- Used only for exits (stops, take-profits)
- Faster fills (guaranteed execution)
- Higher fees (taker fee)

### Order Placement

**Process:**
1. Compute target positions (from sizing engine)
2. Reconcile with current positions
3. Cancel stale orders (older than max_age_sec, default: 180s)
4. Place new limit orders for delta positions
5. Reprice orders if far from target (spread guard)

**Rationale:**
- Limits order book depth (reduces market impact)
- Better fees (post-only limit orders)
- Faster execution (stale order cleanup)

### Spread Guard

**Process:**
1. Check bid-ask spread for each asset
2. Skip trading if spread > threshold (default: 15 bps)
3. Optional: Dynamic offset based on spread (wider spread → larger offset)

**Rationale:**
- Avoids trading in illiquid markets (wide spreads)
- Reduces slippage (skips unfavorable conditions)
- Improves fill quality (dynamic offset)

---

## Optimizer / Self-Improvement Pipeline

### Walk-Forward Optimization (WFO)

**Process:**
1. Split historical data into train/test windows
2. Optimize parameters on training data
3. Validate on test (out-of-sample) data
4. Slide windows forward, repeat

**Rationale:**
- Reduces overfitting (tests parameters on unseen data)
- More robust parameter selection (validated on multiple periods)
- Adapts to market changes (rolling forward)

### Bayesian Optimization (BO)

**Process:**
1. Use Optuna TPE sampler to efficiently explore parameter space
2. Focus on promising regions (learns from previous trials)
3. Find good parameters faster than grid search

**Rationale:**
- Efficient parameter search (fewer backtests than grid search)
- Adapts to parameter space (learns which regions are promising)
- Handles complex search spaces (high-dimensional, non-linear)

### Monte Carlo Stress Testing

**Process:**
1. Bootstrap historical trades (resample with replacement)
2. Perturb costs (slippage, fees, funding)
3. Generate synthetic equity paths
4. Compute tail risk metrics (95th percentile drawdown, etc.)

**Rationale:**
- Assesses tail risk (catastrophic scenarios)
- Tests robustness to cost variations (slippage, fees)
- Identifies parameter sets with high tail risk (before deployment)

### Safe Deployment

**Process:**
1. Compare candidate to baseline (current live config)
2. Check improvement thresholds (Sharpe, CAGR, Max DD)
3. Check safety constraints (MC tail risk, drawdown limits)
4. Deploy new config only if approved (with backup)
5. Enable rollback if live performance degrades

**Rationale:**
- Prevents bad configs from being deployed
- Maintains safety guards (improvement thresholds, tail risk checks)
- Enables quick recovery (rollback to previous config)

See [`../usage/optimizer.md`](../usage/optimizer.md) for detailed optimizer documentation.

---

## Monitoring & Alerting

### Health Checks

**Heartbeat:**
- Writes timestamp to file every cycle (default: hourly)
- External system can check heartbeat freshness
- Fails if bot crashes or hangs

### Discord Notifications

**Optimizer Results:**
- Sent after each optimization run
- Includes baseline vs candidate metrics
- Includes deployment decision

**Daily Reports:**
- Sent daily (if cron/systemd configured)
- Includes daily PnL, trades, win rate
- Includes cumulative metrics (total PnL, max DD)

**Rationale:**
- Keeps operator informed (optimizer results, daily performance)
- Enables quick response (alerts on issues)
- Non-blocking (failures don't crash bot)

See [`../usage/discord_notifications.md`](../usage/discord_notifications.md) for setup.

---

## Data Flow

### Live Trading Loop

```
1. Exchange API → Fetch OHLCV bars
   ↓
2. Signal Generation → Multi-lookback momentum, z-scores, amplification
   ↓
3. Signal Filtering → Regime, ADX, symbol scoring, ToD
   ↓
4. Top-K Selection → Long top K, short bottom K
   ↓
5. Position Sizing → Inverse-vol, caps, vol targeting
   ↓
6. Risk Checks → Daily loss, drawdown limits
   ↓
7. Order Reconciliation → Cancel stale, place new limit orders
   ↓
8. State Persistence → Write state JSON (positions, stats, cooldowns)
```

### Stop-Loss / Take-Profit Loop (Background Thread)

```
FastSLTPThread (runs every 2s):
1. Fetch current positions
   ↓
2. Fetch latest 5m bars
   ↓
3. Check stop-loss triggers (ATR-based, trailing, breakeven)
   ↓
4. Check take-profit triggers (partial TP, full exits)
   ↓
5. Place exit orders if triggered
   ↓
6. Update state (cooldowns, PnL)
```

### Optimizer Flow

```
1. Fetch historical data
   ↓
2. Generate WFO segments (train/OOS windows)
   ↓
3. For each segment:
   a. Run Bayesian Optimization on training data
   b. Select top K parameter sets
   c. Evaluate on OOS window
   d. Run Monte Carlo stress tests
   ↓
4. Aggregate metrics across segments
   ↓
5. Compare to baseline (current live config)
   ├─ Check improvement thresholds
   └─ Check safety constraints
   ↓
6. If approved: Deploy new config (with backup)
   Else: Keep existing config
```

See [`../architecture/high_level_architecture.md`](../architecture/high_level_architecture.md) for detailed data flow.

---

## Module Responsibilities

### Core Modules

- **`main.py`** - Entry point: CLI parsing, dispatches to `live` or `backtest`
- **`config.py`** - Pydantic config schema: type-safe configuration management
- **`exchange.py`** - CCXT wrapper: unified interface for Bybit (fetch OHLCV, orders, equity)
- **`live.py`** - Live trading loop: orchestrates strategy execution, order management, risk checks
- **`backtester.py`** - Backtesting engine: simulates strategy with realistic costs
- **`signals.py`** - Signal generation: momentum, regime filters, ADX, meta-labeler
- **`sizing.py`** - Position sizing: inverse-volatility, Kelly scaling, caps, vol targeting
- **`risk.py`** - Risk management: kill-switch, drawdown tracking, daily loss limits
- **`regime_router.py`** - Regime switching: dynamically chooses XSMOM vs TSMOM
- **`carry.py`** - Carry trading: funding/basis trades with delta-neutral hedging
- **`anti_churn.py`** - Trade throttling: prevents overtrading via cooldowns
- **`utils.py`** - Utilities: JSON I/O, logging setup, health checks

### Optimizer Modules

- **`optimizer/full_cycle.py`** - Full-cycle orchestrator: WFO + BO + MC + deployment
- **`optimizer/walk_forward.py`** - Walk-forward optimization: purged segments with embargo
- **`optimizer/bo_runner.py`** - Bayesian optimization: Optuna TPE sampler
- **`optimizer/monte_carlo.py`** - Monte Carlo stress testing: bootstrap and cost perturbation
- **`optimizer/backtest_runner.py`** - Backtest runner: clean entrypoint with parameter overrides
- **`optimizer/config_manager.py`** - Config manager: versioning, deployment, rollback

See [`../architecture/module_map.md`](../architecture/module_map.md) for complete module map.

---

## Configuration System

### Config File Structure

**`config/config.yaml`** (YAML format):
```yaml
exchange:
  id: bybit
  account_type: swap
  quote: USDT
  max_symbols: 36
  timeframe: 1h

strategy:
  signal_power: 1.35
  lookbacks: [12, 24, 48, 96]
  k_min: 2
  k_max: 6
  gross_leverage: 0.95
  max_weight_per_asset: 0.09

risk:
  max_daily_loss_pct: 5.0
  atr_mult_sl: 2.0
  trail_atr_mult: 1.5
```

### Pydantic Validation

All configs are **validated** via Pydantic schemas in `src/config.py`:
- Type checking (e.g., `signal_power` must be float)
- Range validation (e.g., `gross_leverage` must be 0.0-2.0)
- Default values (safe fallbacks if missing)

**Rationale:**
- Prevents invalid configs (fails fast on startup)
- Type safety (catches errors early)
- Self-documenting (schema defines valid values)

See [`../architecture/config_system.md`](../architecture/config_system.md) for detailed config system.

---

## State Management

### State File

**Location:** `config/paths.state_path` (default: `/opt/xsmom-bot/state.json`)

**Contents:**
```json
{
  "perpos": { ... },              // Per-position state
  "cooldowns": { ... },           // Symbol cooldowns
  "day_start_equity": 10000.0,    // Equity at start of UTC day
  "day_high_equity": 10200.0,     // Highest equity during day
  "sym_stats": { ... },           // Per-symbol trade statistics
  ...
}
```

**Purpose:**
- Crash recovery (reload positions on restart)
- Trade throttling (cooldowns, bans)
- Daily equity tracking (kill-switch thresholds)
- Symbol statistics (performance-based filtering)

### Atomic Writes

State file uses **atomic writes** via `utils.write_json_atomic()`:
1. Write to temporary file
2. Rename temporary file to final path (atomic on Unix)

**Rationale:**
- Prevents corruption (crash during write doesn't corrupt state)
- Crash-safe (previous state preserved if write fails)

---

## Deployment Model

### Development

```bash
# Run backtest
python -m src.main backtest --config config/config.yaml

# Run live (testnet)
python -m src.main live --config config/config.yaml
```

### Production

**systemd Services:**
- `xsmom-bot.service` - Main trading bot (24/7 operation)
- `xsmom-optimizer-full-cycle.service` - Weekly optimizer (systemd timer)
- `xsmom-daily-report.service` - Daily reports (systemd timer)

**Rationale:**
- 24/7 operation (restarts on crash)
- Scheduled tasks (optimizer, reports)
- Logging and monitoring (journalctl integration)

See [`../operations/deployment_ubuntu_systemd.md`](../operations/deployment_ubuntu_systemd.md) for setup.

---

## Framework Evolution

### Recent Changes

See [`change_log_architecture.md`](change_log_architecture.md) for framework-level changes over time.

**Recent improvements:**
- Walk-forward optimizer (reduces overfitting)
- Bayesian optimization (efficient parameter search)
- Monte Carlo stress testing (tail risk assessment)
- Discord notifications (optimizer results, daily reports)
- Atomic state writes (crash-safe)

---

## Design Principles

1. **Robustness over complexity** - Simple, well-tested strategies beat fragile, overfitted ones
2. **Risk management first** - Never risk more than you can afford to lose
3. **Automation everywhere** - Minimize manual intervention
4. **Clear documentation** - Easy to understand, maintain, and extend
5. **Continuous improvement** - Automated optimization, but with safety guards

---

## Next Steps

👉 **Read [`../start_here.md`](../start_here.md)** for your reading path.

👉 **Read [`../architecture/strategy_logic.md`](../architecture/strategy_logic.md)** for detailed strategy logic.

👉 **Read [`../architecture/risk_management.md`](../architecture/risk_management.md)** for detailed risk controls.

---

**Motto: MAKE MONEY** — with a clear, well-understood, and well-documented framework. 📈

