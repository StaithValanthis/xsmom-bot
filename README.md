# XSMOM Multi-Pair Crypto Bot (Bybit USDT-Perps)

Production-ready cross-sectional momentum bot with inverse-volatility sizing, liquidity-aware caps, cost-aware backtests, and strong risk controls. Modularized for maintainability. Deploys via `systemd`.

## Quick Start

```bash
sudo apt update && sudo apt install -y python3 python3-venv rsync
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

cp .env.example .env
cp config/config.yaml.example config/config.yaml
nano .env        # add BYBIT API keys
nano config/config.yaml  # tune strategy parameters

# Backtest
./venv/bin/python -m src.main backtest --config config/config.yaml

# Live (dry-run)
./venv/bin/python -m src.main live --config config/config.yaml --dry

# Live (real)
./venv/bin/python -m src.main live --config config/config.yaml
