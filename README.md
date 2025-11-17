# XSMOM Multi-Pair Crypto Bot (Bybit USDT-Perps)

**Production-ready cross-sectional momentum bot** with inverse-volatility sizing, automated optimization, and robust risk controls.

**Motto:** **MAKE MONEY** — maximize robust, risk-adjusted profitability with full automation and clear documentation.

---

## 🚀 Quick Start

```bash
# Install dependencies
python3 -m venv venv
./venv/bin/pip install -r requirements.txt

# Configure
cp config/config.yaml.example config/config.yaml
cp .env.example .env
nano .env        # add BYBIT API keys
nano config/config.yaml  # tune strategy parameters

# Run backtest
python -m src.main backtest --config config/config.yaml

# Run live (testnet)
python -m src.main live --config config/config.yaml
```

---

## 📚 Documentation

**👉 Start here:** [`docs/start_here.md`](docs/start_here.md)

Complete documentation available in [`docs/`](docs/):

- **[`docs/overview/quickstart.md`](docs/overview/quickstart.md)** - Get running in 5-10 minutes
- **[`docs/architecture/`](docs/architecture/)** - System architecture, strategy logic, risk management
- **[`docs/usage/`](docs/usage/)** - Live trading, backtesting, optimizer, Discord notifications
- **[`docs/operations/`](docs/operations/)** - Deployment, monitoring, troubleshooting
- **[`docs/kb/`](docs/kb/)** - Knowledge Base (framework overview, change log)

---

## 🎯 Features

- **Cross-Sectional Momentum (XSMOM)** - Market-neutral momentum strategy
- **Automated Optimization** - Walk-forward + Bayesian + Monte Carlo
- **Robust Risk Management** - Daily loss limits, ATR-based stops, kill-switches
- **Production-Ready** - systemd integration, health checks, Discord notifications
- **Well-Documented** - Comprehensive docs, KB, auto-generated references

---

## 🛠️ Auto-Optimization

A background optimizer sweeps parameters weekly using walk-forward optimization, Bayesian optimization, and Monte Carlo stress testing.

**Service:** `systemd/xsmom-optimizer-full-cycle.service`
**Timer:** `systemd/xsmom-optimizer-full-cycle.timer` (weekly)

**Manage:**
```bash
sudo systemctl status xsmom-optimizer-full-cycle.timer
sudo systemctl start xsmom-optimizer-full-cycle.service   # run now
journalctl -u xsmom-optimizer-full-cycle.service -f
