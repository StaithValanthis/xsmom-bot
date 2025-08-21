# XSMOM Multi-Pair Crypto Bot (Bybit USDT-Perps)
<!-- v1.1.0 – 2025-08-21 -->

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

### Auto-optimization (boot + daily)

A background optimizer sweeps timeframe & regime (EMA/slope), writes improvements to `config/config.yaml`, and restarts the bot.

- Service: `xsmom-opt.service`
- Timer: `xsmom-opt.timer` (runs at boot + daily 00:20 UTC)

Manage:
```bash
sudo systemctl status xsmom-opt.timer
sudo systemctl start  xsmom-opt.service   # run now
journalctl -u xsmom-opt.service -f -o cat
