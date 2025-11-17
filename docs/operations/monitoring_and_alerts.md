# Monitoring and Alerts

## Overview

**xsmom-bot** includes multiple monitoring and alerting mechanisms to keep operators informed of bot status, performance, and issues.

---

## Health Checks

### Heartbeat System

**Mechanism:**
- Bot writes timestamp to file every cycle (default: hourly)
- External system can check heartbeat freshness
- Fails if bot crashes or hangs

**Location:**
- `config/paths.state_path` + `.heartbeat` suffix (default: `/opt/xsmom-bot/state.json.heartbeat`)

**Check freshness:**
```bash
# Check heartbeat
sudo -u xsmom cat /opt/xsmom-bot/state.json.heartbeat

# Check age (should be < 2 hours)
python -c "from datetime import datetime, timezone; from src.utils import read_heartbeat; import sys; result = read_heartbeat('/opt/xsmom-bot/state.json.heartbeat', max_age_hours=2); print('OK' if result else 'STALE'); sys.exit(0 if result else 1)"
```

**Rationale:**
- Detects silent failures (bot crashed but systemd didn't notice)
- Enables external monitoring (can check freshness via cron)
- Simple and reliable (just a timestamp file)

---

## Logging

### Log Format

**Console + File:**
- **Format**: `%(asctime)s | %(levelname)s | %(name)s | %(message)s`
- **Level**: Configurable via `config.logging.level` (default: INFO)
- **Rotation**: Daily rotating files (default: 20 MB max, 5 backups)

### Log Locations

**Systemd Logs:**
```bash
# Bot logs
journalctl -u xsmom-bot.service -f

# Optimizer logs
journalctl -u xsmom-optimizer-full-cycle.service -f

# Daily report logs
journalctl -u xsmom-daily-report.service -f
```

**File Logs:**
- `config/paths.logs_dir` (default: `/opt/xsmom-bot/logs`)
- `logs/optimizer/full_cycle_YYYYMMDD_HHMMSS.log`
- `logs/daily_report_YYYYMMDD_HHMMSS.log`

---

## Discord Notifications

### Optimizer Results

**Sent:** After each optimization run

**Includes:**
- Deployment status (✅ Deployed / ⚠️ No Change / ❌ Failed)
- Baseline vs candidate metrics
- Parameter highlights
- Run info (WFO segments, BO trials, MC runs)

**Configuration:**
- `notifications.discord.enabled`: Enable Discord notifications (default: false)
- `notifications.discord.send_optimizer_results`: Send optimizer results (default: true)
- `notifications.discord.webhook_url`: Fallback webhook URL (if env var not set)

See [`../usage/discord_notifications.md`](../usage/discord_notifications.md) for setup.

### Daily Reports

**Sent:** Daily (if cron/systemd configured)

**Includes:**
- Daily PnL (absolute and %)
- Trade count, win rate
- Cumulative metrics (total PnL, max DD)
- Equity growth

**Configuration:**
- `notifications.discord.send_daily_report`: Send daily reports (default: true)

See [`../usage/discord_notifications.md`](../usage/discord_notifications.md) for setup.

---

## Monitoring Setup

### External Monitoring (Cron)

**Heartbeat check:**
```bash
# Add to crontab: check heartbeat every 5 minutes
*/5 * * * * /opt/xsmom-bot/venv/bin/python -c "from src.utils import read_heartbeat; import sys; sys.exit(0 if read_heartbeat('/opt/xsmom-bot/state.json.heartbeat', max_age_hours=2) else 1)" && echo "Bot heartbeat OK" || echo "Bot heartbeat STALE" | mail -s "xsmom-bot Alert" operator@example.com
```

**Service status check:**
```bash
# Add to crontab: check service status every 5 minutes
*/5 * * * * systemctl is-active --quiet xsmom-bot.service || echo "Bot service inactive" | mail -s "xsmom-bot Alert" operator@example.com
```

### systemd Monitoring

**Service status:**
```bash
# Check service status
systemctl status xsmom-bot.service

# Check restart count
systemctl status xsmom-bot.service | grep "Active:"
```

**Timer status:**
```bash
# Check timer status
systemctl status xsmom-optimizer-full-cycle.timer

# Check next run
systemctl list-timers xsmom-optimizer-full-cycle.timer
```

---

## Alert Conditions

### Critical Alerts

**Conditions:**
1. **Bot service inactive** - Service stopped/crashed
2. **Heartbeat stale** - No heartbeat for > 2 hours
3. **Daily loss limit triggered** - Bot paused due to daily loss
4. **Optimizer deployment failed** - Config deployment failed

**Actions:**
- Send Discord notification (if configured)
- Send email alert (if configured)
- Log error (journalctl)

### Warning Alerts

**Conditions:**
1. **No trades for > 4 hours** - Bot running but no activity
2. **High memory usage** - Memory > 500 MB
3. **Disk space low** - Disk < 10% free
4. **Optimizer no improvement** - No better config found

**Actions:**
- Log warning (journalctl)
- Send Discord notification (if configured)

---

## Performance Monitoring

### Daily Metrics

**Tracked:**
- Daily PnL (absolute and %)
- Trade count, win rate
- Cumulative metrics (total PnL, max DD)
- Equity growth

**Source:**
- State file (`state.json`)
- Daily report module (`src/reports/daily_report.py`)

**Access:**
```bash
# View state
sudo -u xsmom cat /opt/xsmom-bot/state.json | jq '.'

# Run daily report
python -m src.reports.daily_report --config config/config.yaml
```

### Optimizer Metrics

**Tracked:**
- Baseline vs candidate metrics
- Parameter changes
- Deployment decisions
- WFO segment results

**Source:**
- Optimizer results (`logs/optimizer/`)
- Config metadata (`config/optimized/metadata_*.json`)

**Access:**
```bash
# View optimizer results
cat logs/optimizer/full_cycle_*.log | tail -100

# View config metadata
cat config/optimized/metadata_*.json | jq '.'
```

---

## Next Steps

- **Discord Notifications**: [`../usage/discord_notifications.md`](../usage/discord_notifications.md) - Setup alerts
- **Troubleshooting**: [`troubleshooting.md`](troubleshooting.md) - Common issues
- **Deployment**: [`deployment_ubuntu_systemd.md`](deployment_ubuntu_systemd.md) - Setup guide

---

**Motto: MAKE MONEY** — but with clear visibility into bot status and performance. 📈

