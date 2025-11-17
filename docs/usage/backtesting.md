# Backtesting

## Overview

**xsmom-bot** includes a cost-aware backtesting engine that simulates strategy performance with realistic costs (fees, slippage, funding).

---

## Running Backtests

### Basic Backtest

**Command:**
```bash
python -m src.main backtest --config config/config.yaml
```

**Output:**
```
=== BACKTEST (cost-aware) ===
Samples: 1440 bars  |  Universe size: 36
Total Return: 15.23% | Annualized: 42.15% | Sharpe: 1.45
Max Drawdown: -12.34% | Calmar: 3.41
Turnover: 234.5x/year
```

### Advanced Options

**If supported by backtest CLI:**
```bash
python -m src.main backtest \
  --config config/config.yaml \
  --start-date "2024-01-01" \
  --end-date "2024-12-31" \
  --symbols "BTC/USDT,ETH/USDT,SOL/USDT"
```

---

## Backtest Flow

**Process:**
1. Fetch historical OHLCV data for symbols
2. Warmup period (max(lookbacks) + vol_lookback + 5 bars)
3. For each bar:
   a. Compute targets (same as live trading loop)
   b. Portfolio return = (targets × returns).sum()
   c. Costs = fees + slippage + funding
   d. Equity += pnl + costs
4. Compute performance metrics:
   - Total return, annualized return
   - Sharpe ratio, Calmar ratio
   - Max drawdown
   - Turnover statistics

**Costs Included:**
- Maker/taker fees (configurable via `costs.maker_fee_bps`, `costs.taker_fee_bps`)
- Slippage (configurable via `costs.slippage_bps`)
- Funding costs (if enabled, via funding rates)

---

## Interpreting Results

### Key Metrics

**Total Return:**
- Cumulative return over backtest period
- Example: 15.23% total return

**Annualized Return:**
- Annualized return (if backtest < 1 year, extrapolated)
- Example: 42.15% annualized

**Sharpe Ratio:**
- Risk-adjusted return (annualized return / annualized volatility)
- Good: > 1.0, Very good: > 1.5, Excellent: > 2.0

**Calmar Ratio:**
- Return-to-drawdown ratio (annualized return / max drawdown)
- Good: > 1.0, Very good: > 2.0

**Max Drawdown:**
- Maximum peak-to-trough decline (worst-case loss)
- Example: -12.34% max drawdown

**Turnover:**
- Trading frequency (notional traded / equity, per year)
- Example: 234.5×/year (high turnover = more costs)

### What Good Results Look Like

**Ideal Metrics:**
- Sharpe > 1.5 (good risk-adjusted returns)
- Calmar > 2.0 (good return-to-drawdown ratio)
- Max DD < 20% (acceptable worst-case loss)
- Turnover < 500×/year (reasonable trading frequency)

**Warning Signs:**
- Sharpe < 1.0 (poor risk-adjusted returns)
- Calmar < 1.0 (poor return-to-drawdown ratio)
- Max DD > 30% (excessive worst-case loss)
- Turnover > 1000×/year (excessive trading, high costs)

---

## Comparing Configs

### A/B Testing

**Compare two configs:**
```bash
# Run backtest with config 1
python -m src.main backtest --config config/config1.yaml > results1.txt

# Run backtest with config 2
python -m src.main backtest --config config/config2.yaml > results2.txt

# Compare results
diff results1.txt results2.txt
```

**Compare metrics:**
- Sharpe (higher is better)
- Calmar (higher is better)
- Max DD (lower is better, in absolute terms)
- Annualized return (higher is better, but consider risk)

---

## Optimizer Backtests

### Full-Cycle Optimizer

**The optimizer uses backtests internally:**
- Walk-forward optimization (multiple train/test windows)
- Bayesian optimization (many parameter combinations)
- Monte Carlo stress testing (bootstrap and cost perturbations)

**Results:**
- Best parameter sets for each WFO segment
- OOS performance metrics
- MC tail risk metrics

See [`optimizer.md`](optimizer.md) for detailed documentation.

---

## Limitations

### Backtest vs Live Differences

**Backtests assume:**
- Perfect fills (limit orders fill at target price)
- Fixed slippage (may underestimate real slippage)
- Historical funding rates (may differ from live)

**Live trading reality:**
- Partial fills (orders may not fill completely)
- Variable slippage (depends on order size, liquidity)
- Real-time funding rates (may differ from historical)

**Mitigations:**
- Use conservative slippage estimates (`costs.slippage_bps`)
- Use realistic fill ratios (`costs.maker_fill_ratio`)
- Test on testnet before production

---

## Next Steps

- **Optimizer**: [`optimizer.md`](optimizer.md) - Automated parameter optimization
- **Live Trading**: [`live_trading.md`](live_trading.md) - Running live bot
- **Architecture**: [`../architecture/strategy_logic.md`](../architecture/strategy_logic.md) - How strategy works

---

**Motto: MAKE MONEY** — but validate strategies with realistic backtests first. 📈

