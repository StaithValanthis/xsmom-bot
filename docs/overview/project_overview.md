# Project Overview

## What is xsmom-bot?

**xsmom-bot** is a fully automated, multi-pair crypto futures trading bot that implements a **cross-sectional momentum (XSMOM)** strategy on Bybit USDT-perpetual futures.

### Core Strategy

The bot ranks cryptocurrencies by their momentum (weighted returns over multiple lookback periods) and:
- **Goes long** the top K assets (strongest momentum)
- **Goes short** the bottom K assets (weakest momentum)
- **Sizes positions** using inverse-volatility weighting (risk parity style)
- **Maintains market neutrality** by ensuring long/short balance

### Key Features

1. **Cross-Sectional Momentum (XSMOM)**
   - Multi-lookback momentum signals (default: 12h, 24h, 48h, 96h)
   - Dynamic K selection (adapts to market dispersion)
   - Market-neutral (long/short balance)

2. **Robust Risk Management**
   - Inverse-volatility position sizing
   - Per-asset and portfolio-level caps
   - Daily loss kill-switches
   - ATR-based stop-loss and trailing stops
   - Portfolio volatility targeting

3. **Regime Awareness**
   - EMA slope filters (only trade in trending markets)
   - Majors gate (BTC/ETH trend check)
   - Regime switching (XSMOM vs TSMOM based on market conditions)

4. **Automated Self-Improvement**
   - Walk-forward optimization (reduces overfitting)
   - Bayesian optimization (efficient parameter search)
   - Monte Carlo stress testing (tail risk assessment)
   - Automatic config deployment with rollback

5. **Production-Ready**
   - systemd integration
   - Health checks and heartbeats
   - Discord notifications
   - Crash recovery (position reconciliation)

---

## High-Level Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    XSMOM-BOT SYSTEM                         │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  ┌──────────┐      ┌──────────┐      ┌──────────┐         │
│  │ Exchange │ ───► │  Signals │ ───► │  Sizing  │         │
│  │  (Bybit) │      │ Generator│      │  Engine  │         │
│  └──────────┘      └──────────┘      └──────────┘         │
│       │                  │                  │               │
│       │                  ▼                  ▼               │
│       │          ┌──────────────┐   ┌──────────────┐      │
│       │          │   Filters    │   │    Risk      │      │
│       │          │ (Regime, ADX)│   │  Management  │      │
│       │          └──────────────┘   └──────────────┘      │
│       │                  │                  │               │
│       │                  ▼                  ▼               │
│       │          ┌─────────────────────────────────┐      │
│       └─────────►│    Order Management            │      │
│                  │  (Reconcile, Place, Monitor)   │      │
│                  └─────────────────────────────────┘      │
│                                                              │
│  ┌──────────────────────────────────────────────────────┐  │
│  │              Background Threads                      │  │
│  │  • FastSLTPThread (stop-loss/take-profit)           │  │
│  │  • Position reconciliation                           │  │
│  └──────────────────────────────────────────────────────┘  │
│                                                              │
│  ┌──────────────────────────────────────────────────────┐  │
│  │            Optimizer (Separate Process)              │  │
│  │  Walk-Forward + Bayesian + Monte Carlo              │  │
│  └──────────────────────────────────────────────────────┘  │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

---

## Strategy Flow (Simplified)

```
1. Fetch OHLCV data for universe (filtered by volume/price)
   ↓
2. Compute momentum signals (multi-lookback, cross-sectional z-scores)
   ↓
3. Apply regime filters (EMA slope, majors trend)
   ↓
4. Apply signal filters (ADX, symbol scoring, time-of-day)
   ↓
5. Select top K longs, bottom K shorts
   ↓
6. Size positions (inverse-vol, caps, vol targeting, Kelly)
   ↓
7. Apply risk checks (daily loss, drawdown limits)
   ↓
8. Place/reconcile orders (limit orders, post-only)
   ↓
9. Monitor positions (stop-loss, trailing stops, partial TP)
```

For detailed flow, see [`architecture/strategy_logic.md`](../architecture/strategy_logic.md).

---

## Key Modules

### Core Trading

- **`src/main.py`** - Entry point (CLI: `live` / `backtest`)
- **`src/live.py`** - Main live trading loop (~2200 lines)
- **`src/backtester.py`** - Cost-aware backtesting engine
- **`src/exchange.py`** - CCXT wrapper for Bybit

### Strategy

- **`src/signals.py`** - Signal generation (momentum, regime, ADX)
- **`src/sizing.py`** - Position sizing (inverse-vol, Kelly, caps)
- **`src/regime_router.py`** - Regime switching (XSMOM vs TSMOM)

### Risk & Execution

- **`src/risk.py`** - Risk management (kill-switch, drawdown tracking)
- **`src/anti_churn.py`** - Trade throttling (cooldowns, streak tracking)
- **`src/carry.py`** - Carry trading (funding/basis sleeve)

### Optimization

- **`src/optimizer/full_cycle.py`** - Full-cycle optimizer (WFO + BO + MC)
- **`src/optimizer/walk_forward.py`** - Walk-forward optimization
- **`src/optimizer/bo_runner.py`** - Bayesian optimization (Optuna)
- **`src/optimizer/monte_carlo.py`** - Monte Carlo stress testing

### Infrastructure

- **`src/config.py`** - Pydantic config schema (type-safe)
- **`src/utils.py`** - JSON I/O, logging, health checks
- **`src/notifications/discord_notifier.py`** - Discord notifications
- **`src/reports/daily_report.py`** - Daily performance reports

For complete module map, see [`architecture/module_map.md`](../architecture/module_map.md).

---

## Configuration

The bot is **entirely config-driven** via `config/config.yaml`:

- **Exchange settings**: Universe filters, timeframe
- **Strategy parameters**: Lookbacks, K selection, signal power
- **Risk controls**: Daily loss limits, stop-loss multipliers
- **Execution**: Rebalance timing, order placement
- **Optimizer**: WFO/BO/MC settings

All parameters are validated via Pydantic schemas in `src/config.py`.

See [`reference/config_reference.md`](../reference/config_reference.md) for complete parameter list.

---

## Deployment

### Development

```bash
# Run backtest
python -m src.main backtest --config config/config.yaml

# Run live (testnet)
python -m src.main live --config config/config.yaml
```

### Production

- **Service**: `systemd/xsmom-bot.service`
- **Optimizer Timer**: `systemd/xsmom-optimizer-full-cycle.timer` (weekly)
- **Daily Report Timer**: `systemd/xsmom-daily-report.timer` (daily at 00:05 UTC)

See [`operations/deployment_ubuntu_systemd.md`](../operations/deployment_ubuntu_systemd.md) for setup.

---

## Performance Goals

**Primary Objectives:**

1. **Risk-Adjusted Returns** - Maximize Sharpe ratio, minimize drawdown
2. **Consistency** - Stable performance across market regimes
3. **Automation** - 24/7 unattended operation
4. **Robustness** - Survive market crashes, exchange issues, network failures

**Success Metrics:**

- **Sharpe Ratio** > 1.5 (annualized)
- **Max Drawdown** < 20%
- **Calmar Ratio** > 2.0
- **Win Rate** > 50%
- **Uptime** > 99%

---

## Getting Started

1. **Quick Start**: [`overview/quickstart.md`](quickstart.md) (5-10 minutes)
2. **Architecture**: [`architecture/high_level_architecture.md`](../architecture/high_level_architecture.md)
3. **Strategy Logic**: [`architecture/strategy_logic.md`](../architecture/strategy_logic.md)
4. **Knowledge Base**: [`kb/framework_overview.md`](../kb/framework_overview.md)

---

## Philosophy

**MAKE MONEY** — but with:

- ✅ **Robustness over complexity** - Simple, well-tested strategies beat fragile, overfitted ones
- ✅ **Risk management first** - Never risk more than you can afford to lose
- ✅ **Automation everywhere** - Minimize manual intervention
- ✅ **Clear documentation** - Easy to understand, maintain, and extend
- ✅ **Continuous improvement** - Automated optimization, but with safety guards

---

**Next Steps:** See [`start_here.md`](../start_here.md) for your reading path.

