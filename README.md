# XSMOM Multi-Pair Crypto Bot (Bybit USDT-Perps)

Hourly cross-sectional momentum with inverse-vol sizing and portfolio caps. Includes a vectorized backtest and a live loop. Deployed as a `systemd` service on Ubuntu.

## 0) Prereqs

```bash
sudo apt update
sudo apt install -y python3 python3-venv rsync
