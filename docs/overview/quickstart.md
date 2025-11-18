# Quick Start Guide

Get xsmom-bot running in **5-10 minutes** with the one-shot installer.

---

## Prerequisites

- **OS**: Ubuntu 20.04+ (or similar Linux distribution)
- **User**: Non-root user with sudo access (e.g., `ubuntu`)
- **Exchange Account**: Bybit account (testnet OK for testing)
- **API Keys**: Bybit API key + secret (create in Bybit account settings)
- **Discord Webhook** (optional): For notifications

---

## One-Shot Installation (Recommended)

### 1. Clone Repository

```bash
git clone <repository-url>
cd xsmom-bot
```

### 2. Run Installer

```bash
chmod +x install.sh
./install.sh
```

**What the installer does:**
- ✅ Installs missing system packages (Python, pip, git)
- ✅ Creates virtual environment and installs dependencies
- ✅ Creates required directories (`logs/`, `state/`, `data/`, etc.)
- ✅ Copies example config to `config/config.yaml`
- ✅ **Prompts you for:**
  - Bybit API key
  - Bybit API secret
  - Discord webhook URL (optional)
- ✅ Stores secrets in `.env` file (secure, mode 600)
- ✅ Installs and enables systemd services:
  - `xsmom-bot.service` (main trading bot)
  - `xsmom-optimizer.service + timer` (nightly optimization)
  - `xsmom-meta-trainer.service + timer` (daily meta-label training)
  - `xsmom-daily-report.service + timer` (daily performance reports)
  - `xsmom-rollout-supervisor.service + timer` (staging/promotion lifecycle)
- ✅ Validates installation (smoke tests)

**During installation, you'll be prompted:**

```
[?] Bybit API Key (required): <enter your API key>
[?] Bybit API Secret (required, hidden): <enter your secret>
[?] Discord Webhook URL (optional, press Enter to skip): <enter webhook or skip>
[?] Start service now? (y/N): <y to start immediately, N to start later>
```

**Installation location:** `/opt/xsmom-bot` (or `$HOME/xsmom-bot-app` if `/opt` unavailable)

### 3. Verify Installation

```bash
# Check services are installed
systemctl list-units | grep xsmom

# Check timers are active
systemctl list-timers | grep xsmom

# Check bot service status
sudo systemctl status xsmom-bot.service

# View bot logs
journalctl -u xsmom-bot.service -f
```

### 4. Configure for Testnet (Recommended First Step)

```bash
# Edit config
sudo nano /opt/xsmom-bot/config/config.yaml
```

**Set testnet mode:**
```yaml
exchange:
  testnet: true  # Use Bybit testnet (fake money)
```

**Use testnet API keys:**
- Get testnet keys from: https://testnet.bybit.com/
- Update `.env`:
```bash
sudo nano /opt/xsmom-bot/.env
# Update BYBIT_API_KEY and BYBIT_API_SECRET with testnet keys
```

### 5. Start Bot

```bash
# Start bot service
sudo systemctl start xsmom-bot.service

# Enable auto-start on boot
sudo systemctl enable xsmom-bot.service

# Check status
sudo systemctl status xsmom-bot.service
```

---

## Manual Installation (Alternative)

If you prefer manual setup or are on macOS/Windows:

### 1. Clone Repository

```bash
git clone <repository-url>
cd xsmom-bot
```

### 2. Create Virtual Environment

```bash
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

### 3. Install Dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

**Dependencies:**
- `ccxt==4.4.80` - Exchange API
- `pandas==2.2.2` - Data manipulation
- `numpy==1.26.4` - Numerical operations
- `pydantic==2.8.2` - Config validation
- `optuna==3.6.1` - Bayesian optimization
- `requests==2.31.0` - HTTP (Discord notifications)

### 4. Configure Environment

```bash
# Copy example .env file
cp .env.example .env

# Edit .env and add your Bybit API keys
nano .env
```

**`.env` file:**
```bash
BYBIT_API_KEY=your_api_key_here
BYBIT_API_SECRET=your_api_secret_here
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...  # Optional
```

### 5. Create Config File

```bash
# Copy example config
cp config/config.yaml.example config/config.yaml

# Edit config (optional - defaults are reasonable)
nano config/config.yaml
```

**Minimum config changes:**
- Set `exchange.testnet: true` for testing (recommended!)
- Review `risk.max_daily_loss_pct` (default: 5.0%)
- Adjust `strategy.k_min` / `strategy.k_max` if needed

---

## Your First Backtest

Test the strategy on historical data:

```bash
python -m src.main backtest --config config/config.yaml
```

**Expected output:**
```
=== BACKTEST (cost-aware) ===
Samples: 1440 bars  |  Universe size: 36
Total Return: 15.23% | Annualized: 42.15% | Sharpe: 1.45
Max Drawdown: -12.34% | Calmar: 3.41
```

**What this means:**
- Strategy made 15.23% total return
- Annualized: 42.15% (if run for a full year)
- Sharpe: 1.45 (risk-adjusted returns)
- Max drawdown: -12.34% (worst peak-to-trough decline)

---

## Run on Testnet

**Before going live, always test on testnet!**

### 1. Enable Testnet

```yaml
# config/config.yaml
exchange:
  testnet: true  # Use Bybit testnet
```

### 2. Use Testnet API Keys

Get testnet API keys from: https://testnet.bybit.com/

Update `.env`:
```bash
BYBIT_API_KEY=testnet_api_key
BYBIT_API_SECRET=testnet_api_secret
```

### 3. Start Live Bot

```bash
python -m src.main live --config config/config.yaml
```

**Expected output:**
```
Starting live loop (mode=LIVE)
Fast SL/TP loop starting: check every 2s on timeframe=5m
=== Cycle start 2025-11-17T10:00:00Z ===
Equity: $1000.00 | Positions: 0
Universe: 36 symbols
```

The bot will:
- Fetch OHLCV data every hour (at minute 1)
- Compute signals and size positions
- Place limit orders
- Monitor positions with stop-loss/take-profit

**Stop the bot:** Press `Ctrl+C` (it will clean up gracefully)

---

## Run on Production (Real Money)

⚠️ **Warning:** Only proceed after thorough testing on testnet!

### 1. Disable Testnet

```yaml
# config/config.yaml
exchange:
  testnet: false  # Use real Bybit
```

### 2. Use Production API Keys

Update `.env` with real Bybit API keys:
```bash
BYBIT_API_KEY=production_api_key
BYBIT_API_SECRET=production_api_secret
```

### 3. Review Risk Settings

```yaml
# config/config.yaml
risk:
  max_daily_loss_pct: 5.0  # Stop trading if daily loss > 5%
  atr_mult_sl: 2.0          # Stop loss at 2× ATR
```

### 4. Start Bot

```bash
python -m src.main live --config config/config.yaml
```

---

## Next Steps

1. **Understand the Strategy:**
   - Read [`architecture/strategy_logic.md`](../architecture/strategy_logic.md)

2. **Tune Parameters:**
   - Read [`reference/config_reference.md`](../reference/config_reference.md)
   - Adjust `strategy.signal_power`, `strategy.k_min/k_max`, etc.

3. **Set Up Automated Optimization:**
   - Read [`usage/optimizer.md`](../usage/optimizer.md) for the full-cycle optimizer
   - Read [`usage/optimizer_service.md`](../usage/optimizer_service.md) for the database-backed optimizer service
   - **Quick start with optimizer service:**
     ```bash
     # Run once
     python -m src.optimizer.service run-once --trials 25
     
     # Or run continuously (watch mode)
     python -m src.optimizer.service watch --trials-per-iter 10 --sleep-seconds 1800
     
     # Or use systemd service
     sudo cp systemd/xsmom-optimizer-service.service /etc/systemd/system/
     sudo systemctl enable xsmom-optimizer-service
     sudo systemctl start xsmom-optimizer-service
     ```
   - **Query historical results:**
     ```bash
     python -m src.optimizer.query list-studies
     python -m src.optimizer.query top-trials --study-name xsmom_wfo_v1_... --limit 20
     ```

4. **Deploy to Production:**
   - Read [`operations/deployment_ubuntu_systemd.md`](../operations/deployment_ubuntu_systemd.md)
   - Set up systemd service for 24/7 operation

5. **Set Up Monitoring:**
   - Read [`operations/monitoring_and_alerts.md`](../operations/monitoring_and_alerts.md)
   - Configure Discord notifications

---

## Common Issues

### "No symbols after filters"

**Cause:** Universe filter too restrictive (volume/price thresholds).

**Fix:** Adjust `config/config.yaml`:
```yaml
exchange:
  max_symbols: 50              # Increase
  min_usd_volume_24h: 50000000  # Decrease (50M instead of 100M)
```

### "API keys missing"

**Cause:** `.env` file not loaded or keys not set.

**Fix:**
```bash
# Check .env exists and has keys
cat .env

# Or set environment variables directly
export BYBIT_API_KEY=your_key
export BYBIT_API_SECRET=your_secret
```

### "Backtest returns NaN/zero"

**Cause:** Not enough historical data or invalid config.

**Fix:**
- Check `exchange.candles_limit` is large enough (default: 1500)
- Verify `exchange.timeframe` matches your data
- Check logs for errors

### "Bot crashes on startup"

**Cause:** Exchange connection issue or invalid config.

**Fix:**
- Test exchange connection: `python -c "from src.exchange import ExchangeWrapper; from src.config import load_config; ex = ExchangeWrapper(load_config('config/config.yaml').exchange); print(ex.fetch_markets_filtered()[:5]); ex.close()"`
- Validate config: Check for typos in `config/config.yaml`
- Enable testnet to isolate issues

---

## Safety Reminders

1. **Always test on testnet first** - Use `exchange.testnet: true`
2. **Start with small position sizes** - Lower `strategy.gross_leverage` (e.g., 0.5)
3. **Set conservative daily loss limits** - `risk.max_daily_loss_pct: 3.0` (instead of 5.0)
4. **Monitor closely for first 24-48 hours** - Check logs, Discord alerts
5. **Have rollback plan** - Keep previous config backed up

---

## What's Next?

👉 **For deep understanding:** [`start_here.md`](../start_here.md) → **Deep Understanding** path

👉 **For production deployment:** [`operations/deployment_ubuntu_systemd.md`](../operations/deployment_ubuntu_systemd.md)

👉 **For strategy tuning:** [`reference/config_reference.md`](../reference/config_reference.md)

---

**Remember:** **MAKE MONEY** — but start small, test thoroughly, and scale up gradually. 🚀

