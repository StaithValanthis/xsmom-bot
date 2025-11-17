# Start Here — Documentation Reading Guide

**Welcome to xsmom-bot!** This guide tells you exactly what to read, and in what order, based on your goals.

---

## 🎯 Choose Your Path

### Path 1: "I want to run the bot quickly" (5-10 minutes)

**Goal:** Get the bot running on testnet or paper trading as fast as possible.

**Reading Order:**

1. **[`overview/quickstart.md`](overview/quickstart.md)** ⏱️ 5 min
   - Installation
   - Basic config setup
   - Run your first backtest
   - Run the bot on testnet

2. **[`usage/live_trading.md`](usage/live_trading.md)** ⏱️ 5 min
   - Live trading setup
   - Safety checks
   - Monitoring basics

**You're done!** You can now run the bot. Come back later for deeper understanding.

---

### Path 2: "I want to understand how it works under the hood" (30-60 minutes)

**Goal:** Deep understanding of the strategy, architecture, and codebase.

**Reading Order:**

1. **[`overview/project_overview.md`](overview/project_overview.md)** ⏱️ 10 min
   - What the bot does
   - Core concepts (XSMOM, inverse-vol sizing, etc.)
   - Key terminology

2. **[`architecture/high_level_architecture.md`](architecture/high_level_architecture.md)** ⏱️ 15 min
   - System overview
   - Data flow: Exchange → Signals → Orders
   - Where each piece lives in `src/`

3. **[`architecture/strategy_logic.md`](architecture/strategy_logic.md)** ⏱️ 20 min
   - Signal generation (momentum, regime filters)
   - Entry/exit logic
   - Position sizing (inverse-vol, Kelly, caps)

4. **[`architecture/risk_management.md`](architecture/risk_management.md)** ⏱️ 15 min
   - Risk limits (daily loss, drawdown)
   - Stop-loss / take-profit logic
   - Position-level risk controls

5. **[`kb/framework_overview.md`](kb/framework_overview.md)** ⏱️ 20 min
   - Complete framework map
   - Design decisions and rationale
   - Cross-links to all components

**Optional deep dives:**
- **[`architecture/module_map.md`](architecture/module_map.md)** - Module-by-module breakdown
- **[`architecture/data_flow.md`](architecture/data_flow.md)** - Detailed data flow diagrams
- Code in `src/` (now you have the context!)

---

### Path 3: "I want to modify configs/strategy" (20-30 minutes)

**Goal:** Tune parameters, adjust strategy, optimize performance.

**Reading Order:**

1. **[`reference/config_reference.md`](reference/config_reference.md)** ⏱️ 15 min
   - All parameters explained
   - Defaults and ranges
   - Optimization recommendations

2. **[`architecture/config_system.md`](architecture/config_system.md)** ⏱️ 10 min
   - How `config.yaml` maps to code
   - Pydantic validation
   - Parameter overrides

3. **[`kb/framework_overview.md`](kb/framework_overview.md)** ⏱️ 10 min (skim)
   - Strategy framework context
   - How parameters affect behavior

**For advanced tuning:**
- **[`usage/optimizer.md`](usage/optimizer.md)** - Automated parameter optimization
- **[`kb/change_log_architecture.md`](kb/change_log_architecture.md)** - Historical parameter changes

---

### Path 4: "I want to deploy to production" (20-30 minutes)

**Goal:** Deploy the bot on a remote Ubuntu server, run 24/7.

**Reading Order:**

1. **[`operations/deployment_ubuntu_systemd.md`](operations/deployment_ubuntu_systemd.md)** ⏱️ 20 min
   - Server setup
   - systemd service configuration
   - Auto-restart, logging

2. **[`operations/monitoring_and_alerts.md`](operations/monitoring_and_alerts.md)** ⏱️ 10 min
   - Health checks
   - Discord notifications
   - Performance monitoring

3. **[`operations/troubleshooting.md`](operations/troubleshooting.md)** ⏱️ 10 min (reference)
   - Common issues
   - Debug procedures
   - Recovery steps

**Before going live:**
- **[`usage/live_trading.md`](usage/live_trading.md)** - Safety checks, testnet testing
- **[`operations/faq.md`](operations/faq.md)** - Production FAQs

---

### Path 5: "I want to use the optimizer" (15-20 minutes)

**Goal:** Set up automated parameter optimization.

**Reading Order:**

1. **[`usage/optimizer.md`](usage/optimizer.md)** ⏱️ 15 min
   - Walk-forward optimization
   - Bayesian optimization
   - Monte Carlo stress testing
   - Config deployment

2. **[`reference/config_reference.md`](reference/config_reference.md)** ⏱️ 5 min (skim)
   - Which parameters to optimize
   - Safe ranges

**Integration:**
- **[`operations/deployment_ubuntu_systemd.md`](operations/deployment_ubuntu_systemd.md)** - systemd timer setup

---

## 📋 Essential Concepts (Quick Reference)

Before diving in, here are the key concepts you'll encounter:

- **XSMOM (Cross-Sectional Momentum)**: Ranks assets by momentum, goes long top K, short bottom K
- **Inverse-Volatility Sizing**: Weights positions inversely to volatility (risk parity style)
- **Dynamic K Selection**: Adapts number of positions based on market dispersion
- **Regime Filtering**: Only trades when market shows clear trend (EMA slope)
- **Walk-Forward Optimization**: Tests parameters on out-of-sample data to reduce overfitting
- **Bayesian Optimization**: Efficiently explores parameter space using Optuna TPE
- **Monte Carlo Stress Testing**: Assesses tail risk via bootstrapping

See **[`overview/glossary.md`](overview/glossary.md)** for complete definitions.

---

## 🔄 Maintenance & Updates

### When the Framework Changes

If you modify the bot's architecture, strategy, or config system:

1. **Update KB:**
   - Add entry to [`kb/change_log_architecture.md`](kb/change_log_architecture.md)
   - Update [`kb/framework_overview.md`](kb/framework_overview.md) if needed

2. **Regenerate Auto-Generated Docs:**
   ```bash
   python -m tools.update_kb
   ```

3. **Update Relevant Architecture Docs:**
   - [`architecture/strategy_logic.md`](architecture/strategy_logic.md) - Strategy changes
   - [`architecture/risk_management.md`](architecture/risk_management.md) - Risk changes
   - [`architecture/config_system.md`](architecture/config_system.md) - Config changes

See [`meta/style_guide.md`](meta/style_guide.md) for documentation conventions.

---

## ❓ Still Lost?

1. **Check [`operations/faq.md`](operations/faq.md)** — Common questions answered
2. **Check [`operations/troubleshooting.md`](operations/troubleshooting.md)** — Problem solving
3. **Review [`overview/glossary.md`](overview/glossary.md)** — Terminology clarifications
4. **Read [`kb/framework_overview.md`](kb/framework_overview.md)** — Complete framework map

---

## 🎯 Remember the Motto

**MAKE MONEY** — but with:
- ✅ Robust, risk-managed strategies
- ✅ Clear, maintainable code
- ✅ Well-documented framework
- ✅ Automated self-improvement

Happy trading! 📈

