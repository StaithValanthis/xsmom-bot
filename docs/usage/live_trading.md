# Live Trading

## Overview

This guide covers running xsmom-bot in live trading mode on Bybit USDT-perpetual futures.

**⚠️ Warning:** Always test on testnet first! Use real money only after thorough testing.

---

## Prerequisites

1. **Bybit Account** - Real account (for production) or testnet account (for testing)
2. **API Keys** - API key + secret (create in Bybit account settings)
3. **Config File** - `config/config.yaml` configured
4. **Environment Variables** - `.env` file with API keys

---

## Setup

### 1. Configure Exchange

**Testnet (Recommended for Testing):**
```yaml
# config/config.yaml
exchange:
  testnet: true  # Use Bybit testnet
  id: bybit
  account_type: swap
  quote: USDT
  max_symbols: 36
  timeframe: 1h
```

**Production (Real Money):**
```yaml
# config/config.yaml
exchange:
  testnet: false  # Use real Bybit
  id: bybit
  account_type: swap
  quote: USDT
  max_symbols: 36
  timeframe: 1h
```

### 2. Set API Keys

**`.env` file:**
```bash
BYBIT_API_KEY=your_api_key_here
BYBIT_API_SECRET=your_api_secret_here
```

**Testnet API Keys:**
- Get from: https://testnet.bybit.com/
- Create in Bybit testnet account settings

**Production API Keys:**
- Get from: https://www.bybit.com/
- Create in Bybit account settings
- **⚠️ Use read-only keys if possible** (no withdrawals)

### 3. Review Risk Settings

**Conservative (Recommended for First Run):**
```yaml
# config/config.yaml
risk:
  max_daily_loss_pct: 3.0          # Lower daily loss limit
  atr_mult_sl: 2.5                  # Wider stops
  gross_leverage: 0.75              # Lower leverage
```

**Moderate (Default):**
```yaml
risk:
  max_daily_loss_pct: 5.0          # Standard daily loss limit
  atr_mult_sl: 2.0                  # Standard stops
  gross_leverage: 0.95              # Standard leverage
```

**⚠️ Warning:** Start conservative! You can increase risk after observing live performance.

---

## Running the Bot

### Manual Run

**Testnet:**
```bash
python -m src.main live --config config/config.yaml
```

**Production:**
```bash
# Double-check testnet: false in config!
python -m src.main live --config config/config.yaml
```

**Expected Output:**
```
Starting live loop (mode=LIVE)
Fast SL/TP loop starting: check every 2s on timeframe=5m
=== Cycle start 2025-11-17T10:00:00Z ===
Equity: $1000.00 | Positions: 0
Universe: 36 symbols
```

### Background Run (systemd)

**Setup systemd service:**
```bash
sudo systemctl enable xsmom-bot.service
sudo systemctl start xsmom-bot.service
```

**Check status:**
```bash
sudo systemctl status xsmom-bot.service
```

**View logs:**
```bash
journalctl -u xsmom-bot.service -f
```

See [`../operations/deployment_ubuntu_systemd.md`](../operations/deployment_ubuntu_systemd.md) for detailed setup.

---

## Safety Checks

### Pre-Flight Checklist

**Before going live:**

- [ ] Tested on testnet (at least 24 hours)
- [ ] Config validated (`python -m src.config load_config config/config.yaml`)
- [ ] API keys set (check `.env` file)
- [ ] Risk settings reviewed (daily loss limit, leverage)
- [ ] Universe size reasonable (not too small, not too large)
- [ ] Monitoring setup (Discord notifications, logs)
- [ ] Backup plan (rollback config, stop trading)

### Runtime Safety

**Kill-Switch:**
- Daily loss limit (default: 5% of equity)
- Trailing kill-switch (optional, from day high)
- Automatic pause if limit triggered

**Stop-Losses:**
- ATR-based stops (default: 2.0× ATR)
- Trailing stops (optional)
- Breakeven moves (optional)

**Position Limits:**
- Per-asset caps (default: 9% of portfolio)
- Portfolio gross leverage cap (default: 95%)
- Notional caps (default: $20k per asset)

---

## Monitoring

### Health Checks

**Heartbeat:**
```bash
# Check heartbeat freshness (should be < 2 hours)
cat /opt/xsmom-bot/state.json.heartbeat
```

**Service Status:**
```bash
sudo systemctl status xsmom-bot.service
```

**Logs:**
```bash
journalctl -u xsmom-bot.service -f
```

### Performance Monitoring

**Daily Metrics:**
- Daily PnL (absolute and %)
- Trade count, win rate
- Cumulative PnL, max DD
- Equity growth

**Discord Notifications:**
- Daily reports (if configured)
- Optimizer results (if configured)

**State File:**
```bash
# View current state
cat /opt/xsmom-bot/state.json | jq '.'
```

See [`../operations/monitoring_and_alerts.md`](../operations/monitoring_and_alerts.md) for detailed setup.

---

## Common Issues

### No Trades / Positions

**Causes:**
- Universe filter too restrictive → Relax `exchange.max_symbols`, `exchange.min_usd_volume_24h`
- Regime filter → Market not trending (EMA slope < threshold)
- Entry threshold too high → Lower `strategy.entry_zscore_min`
- Daily loss limit triggered → Bot paused (check `disable_until_ts` in state)

**Fix:**
```bash
# Check state for pause
cat /opt/xsmom-bot/state.json | jq '.disable_until_ts'

# Relax filters
nano config/config.yaml
# - Increase exchange.max_symbols
# - Decrease exchange.min_usd_volume_24h
# - Lower strategy.entry_zscore_min
# - Disable regime_filter.enabled (if too restrictive)
```

### Bot Crashing

**Causes:**
- Exchange API failures → Check network/API status
- State file corruption → Restore from backup
- Config errors → Validate config file

**Fix:**
```bash
# Check logs
journalctl -u xsmom-bot.service -n 100 | grep -i error

# Restore state from backup
cp /opt/xsmom-bot/state.json.backup /opt/xsmom-bot/state.json

# Validate config
python -m src.config load_config config/config.yaml
```

See [`../operations/troubleshooting.md`](../operations/troubleshooting.md) for more issues.

---

## Stopping the Bot

### Graceful Shutdown

**Manual Run:**
- Press `Ctrl+C` (bot will clean up gracefully)

**systemd:**
```bash
sudo systemctl stop xsmom-bot.service
```

**Bot will:**
- Cancel open orders (if configured)
- Save state file
- Exit cleanly

### Emergency Stop

**Kill Process:**
```bash
sudo systemctl kill xsmom-bot.service
```

**Cancel All Orders:**
```bash
# Connect to exchange and cancel orders manually
# Or use exchange API
```

**⚠️ Warning:** Emergency stop may leave open orders. Check exchange positions after restart.

---

## Restarting After Changes

### Config Changes

**After config changes:**
```bash
# Restart bot
sudo systemctl restart xsmom-bot.service

# Check logs for errors
journalctl -u xsmom-bot.service -f
```

**Bot will:**
- Reload config on startup
- Validate config (fails fast on errors)
- Reconcile positions from exchange

### State Recovery

**After crash:**
- Bot reconciles positions from exchange on startup
- Reloads state file (if exists)
- Reconstructs stop-loss state from positions

---

## Best Practices

### Start Small

1. **Start with testnet** - Test thoroughly before production
2. **Start with small positions** - Lower `strategy.gross_leverage` (e.g., 0.5)
3. **Start with conservative risk** - Lower `risk.max_daily_loss_pct` (e.g., 3.0)
4. **Monitor closely** - Check logs and Discord notifications frequently

### Scale Gradually

1. **Observe live performance** - Run for at least 1 week before scaling
2. **Compare to backtest** - Verify live performance matches backtest
3. **Increase risk slowly** - Gradually increase leverage and position sizes
4. **Monitor drawdowns** - Watch for unexpected drawdowns

### Maintain Regularly

1. **Review logs daily** - Check for errors and issues
2. **Monitor performance** - Track daily PnL, trades, win rate
3. **Update configs** - Let optimizer improve parameters (if enabled)
4. **Backup state** - Regularly backup state file and configs

---

## Next Steps

- **Optimizer**: [`optimizer.md`](optimizer.md) - Automated parameter optimization
- **Discord Notifications**: [`discord_notifications.md`](discord_notifications.md) - Setup alerts
- **Deployment**: [`../operations/deployment_ubuntu_systemd.md`](../operations/deployment_ubuntu_systemd.md) - Production deployment
- **Troubleshooting**: [`../operations/troubleshooting.md`](../operations/troubleshooting.md) - Common issues

---

**Motto: MAKE MONEY** — but start small, test thoroughly, and scale gradually. 🚀

