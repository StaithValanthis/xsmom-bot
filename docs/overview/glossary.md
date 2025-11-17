# Glossary

Key terms and concepts used in xsmom-bot documentation.

---

## Trading Concepts

### Cross-Sectional Momentum (XSMOM)

A momentum strategy that **ranks assets relative to each other** at a given point in time:
- Computes momentum signals for all assets
- Normalizes to cross-sectional z-scores
- Goes **long** top K assets (strongest momentum)
- Goes **short** bottom K assets (weakest momentum)
- Maintains **market neutrality** (long/short balance)

**Contrast with Time-Series Momentum (TSMOM):**
- TSMOM: Trade based on asset's own historical momentum
- XSMOM: Trade based on asset's momentum **relative to other assets**

### Inverse-Volatility Sizing

Position sizing method that weights positions **inversely** to their volatility:
- Higher volatility → smaller position size
- Lower volatility → larger position size
- Goal: Equal risk contribution per asset (risk parity)

**Formula:** `weight_i = (1 / volatility_i) / sum(1 / volatility_j)`

### Market Neutral

Strategy that maintains **zero net exposure**:
- Long positions ≈ Short positions (in dollar terms)
- Portfolio beta ≈ 0
- Profit comes from relative performance, not market direction

### Top-K Selection

Selecting the top K assets by some metric (e.g., momentum z-score):
- **K**: Number of positions (e.g., K=6 means 6 longs + 6 shorts = 12 total)
- **Dynamic K**: K adapts to market conditions (dispersion, volatility)
- **Fixed K**: K is constant (simpler, less adaptive)

---

## Risk Management

### Sharpe Ratio

Risk-adjusted return metric:
- **Formula**: `(Return - RiskFreeRate) / Volatility`
- **Interpretation**: 
  - Sharpe > 1.0: Good
  - Sharpe > 1.5: Very good
  - Sharpe > 2.0: Excellent

### Calmar Ratio

Return-to-drawdown ratio:
- **Formula**: `AnnualizedReturn / MaxDrawdown`
- **Interpretation**: 
  - Calmar > 1.0: Returns exceed max DD
  - Calmar > 2.0: Strong risk-adjusted returns

### Max Drawdown (MDD)

Maximum peak-to-trough decline in equity:
- **Example**: Equity goes $1000 → $1200 → $900
- **Max DD**: ($900 - $1200) / $1200 = -25%
- **Importance**: Measures worst-case loss scenario

### ATR (Average True Range)

Volatility measure using price range:
- **True Range**: `max(high - low, |high - prev_close|, |low - prev_close|)`
- **ATR**: Average of true range over N periods (default: 28)
- **Usage**: Stop-loss distances (e.g., 2× ATR below entry)

### Stop-Loss (SL)

Automatic exit order to limit losses:
- **ATR-based**: Stop at entry_price ± (ATR_mult × ATR)
- **Trailing stop**: Stop moves up as price moves favorably
- **Breakeven**: Move stop to entry price after certain profit threshold

### Take-Profit (TP)

Automatic exit order to lock in profits:
- **Partial TP**: Exit portion of position at target (e.g., 45% at 0.75×R)
- **Ladder TP**: Multiple TP levels (e.g., 30% at 0.8×R, 15% at 1.2×R)

### Kill-Switch

Emergency stop mechanism:
- **Daily loss limit**: Stop trading if daily PnL < threshold (e.g., -5%)
- **Trailing kill-switch**: Stop if loss from daily high > threshold
- **Purpose**: Prevent catastrophic losses

---

## Strategy Concepts

### Lookback Period

Time window for computing momentum signals:
- **Example**: 24-hour lookback = compute returns over last 24 hours
- **Multi-lookback**: Use multiple periods (e.g., [12h, 24h, 48h]) and weight them
- **Weights**: Longer lookbacks typically get lower weights

### Z-Score

Normalized metric (standard deviations from mean):
- **Cross-sectional z-score**: `(value - mean) / std` across all assets
- **Interpretation**: 
  - z > 1.0: Strong signal
  - z < -1.0: Weak signal (for shorts)
- **Usage**: Ranking assets, filtering entries

### Signal Power

Nonlinear amplification of z-scores:
- **Formula**: `sign(z) * |z|^power`
- **Default**: `power = 1.35`
- **Effect**: Amplifies strong signals, suppresses weak ones
- **Rationale**: Strong momentum persists; weak momentum is noise

### Regime Filter

Market condition filter:
- **EMA Slope**: Only trade when EMA slope indicates trend
- **Majors Gate**: Check BTC/ETH trend before trading
- **Purpose**: Avoid trading in choppy/trendless markets

### Dynamic K

Adaptive position count based on market conditions:
- **High dispersion** → Wider K (more positions)
- **Low dispersion** → Tighter K (fewer positions)
- **Purpose**: Adapt to market regime (trending vs choppy)

---

## Position Sizing

### Kelly Criterion

Optimal position sizing based on win rate and win/loss ratio:
- **Formula**: `f = (p * win - (1-p) * loss) / win`
- **Fractional Kelly**: Use fraction of Kelly (e.g., 0.5× = half-Kelly)
- **Usage**: Scale positions by conviction (higher win rate → larger position)

### Gross Leverage

Total long + short exposure as fraction of equity:
- **Example**: $1000 equity, $600 longs, $400 shorts → gross leverage = 1.0
- **Default**: 0.95 (95% gross exposure)
- **Cap**: Limits total risk (both long and short)

### Portfolio Volatility Target

Scale entire portfolio to target annualized volatility:
- **Example**: Target 30% annualized vol
- **Mechanism**: Measure recent portfolio vol, scale all positions up/down
- **Purpose**: Maintain consistent risk level across regimes

### Per-Asset Cap

Maximum weight for single asset:
- **Example**: `max_weight_per_asset = 0.10` (10% of portfolio per asset)
- **Purpose**: Prevent over-concentration in one asset

### Notional Cap

Maximum dollar exposure per asset:
- **Example**: `notional_cap_usdt = 20000` (max $20k per asset)
- **Purpose**: Absolute dollar limit (complements percentage caps)

---

## Optimization

### Walk-Forward Optimization (WFO)

Parameter optimization method that reduces overfitting:
- **Process**: 
  1. Split data into train/test windows
  2. Optimize on training data
  3. Validate on test (out-of-sample) data
  4. Slide windows forward, repeat
- **Purpose**: Ensure parameters work on unseen data

### Bayesian Optimization (BO)

Efficient parameter search using probabilistic models:
- **Method**: Tree-structured Parzen Estimator (TPE)
- **Library**: Optuna
- **Advantage**: Finds good parameters faster than grid search
- **Usage**: Optimize 18 core parameters in full-cycle optimizer

### Monte Carlo Stress Testing

Tail risk assessment via simulation:
- **Bootstrap**: Resample trades with replacement to create synthetic equity paths
- **Cost Perturbation**: Inject randomness into slippage, fees, funding
- **Output**: Distribution of returns and drawdowns (p95, p99)
- **Purpose**: Catch catastrophic scenarios before they happen

### Overfitting

When parameters work well on training data but fail on live data:
- **Cause**: Too many parameters, narrow ranges, multiple thresholds
- **Symptoms**: Great backtest, poor live performance
- **Prevention**: Walk-forward optimization, fewer parameters, wider ranges

---

## Technical Terms

### OHLCV

Candlestick/bar data:
- **O**: Open price
- **H**: High price
- **L**: Low price
- **C**: Close price
- **V**: Volume

### Timeframe

Bar interval:
- **Examples**: `1h` (1-hour bars), `5m` (5-minute bars)
- **Default**: `1h` (hourly rebalancing)
- **Usage**: Strategy timeframe (signals, rebalancing)

### Symbol

Trading pair identifier:
- **Format**: `BTC/USDT` (CCXT format) or `BTCUSDT` (exchange format)
- **Universe**: All symbols eligible for trading (filtered by volume/price)

### Equity

Current account value:
- **Formula**: `Cash + Positions_Value`
- **Usage**: Daily PnL tracking, kill-switch thresholds

### PnL (Profit and Loss)

Realized or unrealized profit/loss:
- **Realized PnL**: From closed trades
- **Unrealized PnL**: From open positions (marked-to-market)
- **Daily PnL**: Change in equity over 24 hours

### Maker/Taker Fees

Exchange trading fees:
- **Maker**: Fee for limit orders that add liquidity (typically lower, e.g., -0.01%)
- **Taker**: Fee for market orders that remove liquidity (typically higher, e.g., 0.05%)
- **Post-only**: Ensures order is maker (better fee, may not fill)

### Slippage

Difference between expected and actual execution price:
- **Cause**: Market impact, order book depth
- **Model**: Configurable slippage in backtests (default: 2 bps)

---

## System Terms

### FastSLTPThread

Background thread for stop-loss/take-profit monitoring:
- **Interval**: Every 2 seconds (configurable)
- **Timeframe**: 5-minute bars (for faster response)
- **Purpose**: Exit positions quickly when stop/TP triggers

### State File

JSON file storing runtime state:
- **Path**: `config/paths.state_path` (default: `/opt/xsmom-bot/state.json`)
- **Contents**: 
  - Open positions
  - Cooldowns (symbol bans, trade throttling)
  - Daily equity tracking
  - Symbol statistics
- **Purpose**: Crash recovery, state persistence

### Config Versioning

Automatic versioning of optimized configs:
- **Format**: `config_YYYYMMDD_HHMMSS.yaml`
- **Metadata**: Performance metrics, parameters, WFO info
- **Purpose**: Track optimization history, enable rollback

### Walk-Forward Segment

Single train/test window pair in WFO:
- **Train window**: Data used for optimization (e.g., 120 days)
- **Test window**: Data used for validation (e.g., 30 days)
- **Embargo**: Gap between train and test (prevents data leakage)

---

## Abbreviations

- **XSMOM**: Cross-Sectional Momentum
- **TSMOM**: Time-Series Momentum
- **ATR**: Average True Range
- **EMA**: Exponential Moving Average
- **ADX**: Average Directional Index
- **WFO**: Walk-Forward Optimization
- **BO**: Bayesian Optimization
- **MC**: Monte Carlo
- **PnL**: Profit and Loss
- **DD**: Drawdown
- **SL**: Stop-Loss
- **TP**: Take-Profit
- **OHLCV**: Open/High/Low/Close/Volume
- **USDT**: Tether (stablecoin)
- **UTC**: Coordinated Universal Time

---

**Questions?** See [`operations/faq.md`](../operations/faq.md) or check the relevant architecture doc.

