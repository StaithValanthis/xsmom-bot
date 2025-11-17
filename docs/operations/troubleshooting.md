# Troubleshooting

## Common Issues

### Bot Not Starting

**Symptoms:**
- `systemctl status xsmom-bot.service` shows `Failed`
- Logs show errors on startup

**Check logs:**
```bash
journalctl -u xsmom-bot.service -n 50
```

**Common causes:**
- API keys missing → Check `.env` file exists and has `BYBIT_API_KEY` and `BYBIT_API_SECRET`
- Config invalid → Check `config/config.yaml` for YAML syntax errors or validation errors
- Permissions → Check file ownership (`sudo chown -R xsmom:xsmom /opt/xsmom-bot`)
- Python environment → Check `venv/bin/python` exists and dependencies installed

**Fix:**
```bash
# Check .env file
cat /opt/xsmom-bot/.env

# Validate config
python -m src.config load_config /opt/xsmom-bot/config/config.yaml

# Fix permissions
sudo chown -R xsmom:xsmom /opt/xsmom-bot
```

---

### Bot Crashing / Restarting

**Symptoms:**
- `systemctl status xsmom-bot.service` shows `restarting`
- Logs show repeated crashes

**Check restart count:**
```bash
systemctl status xsmom-bot.service | grep "Active:"
```

**Check logs for errors:**
```bash
journalctl -u xsmom-bot.service -n 100 | grep -i error
```

**Common causes:**
- Exchange API failures → Check network/API status, rate limits
- State file corruption → Restore from backup or clear state
- Config errors → Validate config file
- Out of memory → Check memory usage (`free -h`)
- Disk full → Check disk space (`df -h`)

**Fix:**
```bash
# Check exchange API status
curl https://api.bybit.com/v5/market/instruments-info?category=linear

# Restore state from backup
sudo -u xsmom cp /opt/xsmom-bot/state.json.backup /opt/xsmom-bot/state.json

# Or clear state (will restart from exchange positions)
sudo -u xsmom rm /opt/xsmom-bot/state.json

# Check memory/disk
free -h
df -h
```

---

### No Trades / Positions

**Symptoms:**
- Bot running but no orders placed
- No positions opened

**Check logs:**
```bash
journalctl -u xsmom-bot.service -f | grep -i "trade\|position\|order"
```

**Common causes:**
- Universe filter too restrictive → No symbols passing filters
- Regime filter → Market not trending (EMA slope < threshold)
- Entry threshold too high → No signals passing `entry_zscore_min`
- Daily loss limit triggered → Bot paused (check `disable_until_ts` in state)
- Cooldowns → Symbols in cooldown (check `cooldowns` in state)

**Fix:**
```bash
# Check state for pause
sudo -u xsmom cat /opt/xsmom-bot/state.json | jq '.disable_until_ts'

# Check universe size
# (Look for "Universe: N symbols" in logs)

# Relax filters in config
nano /opt/xsmom-bot/config/config.yaml
# - Increase exchange.max_symbols
# - Decrease exchange.min_usd_volume_24h
# - Lower strategy.entry_zscore_min
# - Disable regime_filter.enabled (if too restrictive)
```

---

### Exchange API Errors

**Symptoms:**
- `ConnectionError`, `RateLimitExceeded`, `InvalidAPIKey`
- Logs show API errors

**Check logs:**
```bash
journalctl -u xsmom-bot.service -n 100 | grep -i "api\|exchange\|connection"
```

**Common causes:**
- API keys invalid → Check `.env` file, verify keys in Bybit account
- Rate limiting → Exchange rate limits exceeded (wait or reduce request frequency)
- Network issues → Check network connectivity (`ping api.bybit.com`)
- Exchange downtime → Check Bybit status page

**Fix:**
```bash
# Test API keys
python -c "from src.exchange import ExchangeWrapper; from src.config import load_config; ex = ExchangeWrapper(load_config('config/config.yaml').exchange); print(ex.fetch_markets_filtered()[:5]); ex.close()"

# Check network
ping api.bybit.com

# Wait for rate limit (usually resets after 1 minute)
```

---

### State File Corruption

**Symptoms:**
- Bot crashes on startup
- `JSONDecodeError` in logs
- State file contains invalid JSON

**Check state file:**
```bash
sudo -u xsmom cat /opt/xsmom-bot/state.json | jq '.'
```

**Fix:**
```bash
# Restore from backup
sudo -u xsmom cp /opt/xsmom-bot/state.json.backup /opt/xsmom-bot/state.json

# Or clear state (will restart from exchange positions)
sudo -u xsmom rm /opt/xsmom-bot/state.json

# Restart bot
sudo systemctl restart xsmom-bot.service
```

**Note:** Clearing state will lose:
- Cooldowns (will reset)
- Symbol statistics (will reset)
- Daily equity tracking (will reset)

Bot will reload positions from exchange on restart.

---

### Optimizer Not Running

**Symptoms:**
- Timer shows `active (waiting)`
- Optimizer never executes

**Check timer status:**
```bash
systemctl status xsmom-optimizer-full-cycle.timer
```

**Check next run:**
```bash
systemctl list-timers xsmom-optimizer-full-cycle.timer
```

**Common causes:**
- Timer not enabled → Enable timer
- Timer missed (system was off) → Check `Persistent=true` in timer file
- Service file missing → Create service file

**Fix:**
```bash
# Enable and start timer
sudo systemctl enable xsmom-optimizer-full-cycle.timer
sudo systemctl start xsmom-optimizer-full-cycle.timer

# Run manually
sudo systemctl start xsmom-optimizer-full-cycle.service

# Check logs
journalctl -u xsmom-optimizer-full-cycle.service -f
```

---

### Optimizer Failing

**Symptoms:**
- Optimizer runs but exits with errors
- No new configs deployed

**Check logs:**
```bash
journalctl -u xsmom-optimizer-full-cycle.service -n 100
```

**Common causes:**
- Historical data unavailable → Check exchange API, data range
- Backtest failures → Check config validity, parameter ranges
- Disk full → Check disk space
- Memory issues → Check memory usage

**Fix:**
```bash
# Check disk space
df -h

# Check memory
free -h

# Run optimizer with verbose logging
python -m src.optimizer.full_cycle --base-config config/config.yaml --verbose
```

---

### Discord Notifications Not Sending

**Symptoms:**
- No Discord messages received
- Logs show "Discord webhook URL not available"

**Check logs:**
```bash
journalctl -u xsmom-bot.service -n 100 | grep -i discord
```

**Common causes:**
- Webhook URL not set → Check `DISCORD_WEBHOOK_URL` env var or `config.yaml`
- Notifications disabled → Check `notifications.discord.enabled = true`
- Webhook invalid/expired → Verify webhook URL in Discord server settings
- Rate limiting → Discord rate limits (30 requests/minute)

**Fix:**
```bash
# Check .env file
cat /opt/xsmom-bot/.env | grep DISCORD_WEBHOOK_URL

# Check config
cat /opt/xsmom-bot/config/config.yaml | grep -A 5 notifications

# Test webhook
curl -X POST $DISCORD_WEBHOOK_URL -H "Content-Type: application/json" -d '{"content": "Test"}'
```

---

### High Memory Usage

**Symptoms:**
- Bot using > 500 MB memory
- System slowing down

**Check memory:**
```bash
ps aux | grep python | grep xsmom
free -h
```

**Common causes:**
- Large universe → Many symbols in trading universe
- Long backtests → Optimizer using lots of memory
- Memory leaks → Check for memory leaks in code

**Fix:**
```bash
# Reduce universe size
nano /opt/xsmom-bot/config/config.yaml
# - Decrease exchange.max_symbols
# - Increase exchange.min_usd_volume_24h

# Restart bot
sudo systemctl restart xsmom-bot.service
```

---

### Slow Performance

**Symptoms:**
- Bot taking > 10 seconds per cycle
- Logs show slow API calls

**Check logs:**
```bash
journalctl -u xsmom-bot.service -n 100 | grep "Cycle start\|Cycle end"
```

**Common causes:**
- Network latency → Check network connectivity
- Exchange API slow → Check Bybit API status
- Large universe → Many API calls per cycle
- Disk I/O → Check disk I/O (`iotop`)

**Fix:**
```bash
# Check network latency
ping api.bybit.com

# Reduce universe size
nano /opt/xsmom-bot/config/config.yaml
# - Decrease exchange.max_symbols

# Check disk I/O
sudo iotop
```

---

## Debug Mode

### Enable Debug Logging

**Config:**
```yaml
# config/config.yaml
logging:
  level: DEBUG  # Change from INFO to DEBUG
```

**Restart bot:**
```bash
sudo systemctl restart xsmom-bot.service
```

**View debug logs:**
```bash
journalctl -u xsmom-bot.service -f
```

### Run Bot Manually (Non-systemd)

**For debugging:**
```bash
cd /opt/xsmom-bot
source venv/bin/activate
python -m src.main live --config config/config.yaml
```

**Advantages:**
- See all output in terminal
- Easy to interrupt (`Ctrl+C`)
- Can use debugger (`pdb`)

---

## Getting Help

### Check Documentation

1. **[`start_here.md`](../start_here.md)** - Reading guide
2. **[`operations/faq.md`](faq.md)** - Frequently asked questions
3. **[`reference/config_reference.md`](../reference/config_reference.md)** - Config parameters
4. **[`architecture/`](../architecture/)** - Architecture docs

### Check Logs

```bash
# Bot logs
journalctl -u xsmom-bot.service -n 200

# Optimizer logs
journalctl -u xsmom-optimizer-full-cycle.service -n 200

# All logs
journalctl -u xsmom-* -n 200
```

### Check State

```bash
# View state file
sudo -u xsmom cat /opt/xsmom-bot/state.json | jq '.'

# Check heartbeat
sudo -u xsmom cat /opt/xsmom-bot/state.json.heartbeat
```

---

## Next Steps

- **FAQ**: [`faq.md`](faq.md) - More common questions
- **Monitoring**: [`monitoring_and_alerts.md`](monitoring_and_alerts.md) - Health checks
- **Deployment**: [`deployment_ubuntu_systemd.md`](deployment_ubuntu_systemd.md) - Setup guide

---

**Motto: MAKE MONEY** — but with robust troubleshooting that keeps the bot running. 📈

