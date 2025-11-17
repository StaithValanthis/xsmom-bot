# Deployment on Ubuntu with systemd

## Overview

This guide covers deploying xsmom-bot to a remote Ubuntu server with 24/7 unattended operation via systemd.

**Recommended:** Use the **one-shot installer** (`install.sh`) for automated setup. See [`installation.md`](installation.md) for details.

**Alternative:** Manual installation steps are provided below for advanced users.

---

## Prerequisites

- **OS**: Ubuntu 20.04+ (or similar Linux distribution)
- **Python**: 3.10+ with `venv` support
- **User Account**: Non-root user with sudo access (e.g., `ubuntu`)
- **Bybit API Keys**: API key + secret (create in Bybit account settings)
- **Discord Webhook** (optional): For notifications

---

## Installation: One-Shot Installer (Recommended)

**See:** [`installation.md`](installation.md) for complete installation guide.

**Quick start:**
```bash
git clone <repository-url>
cd xsmom-bot
chmod +x install.sh
./install.sh
```

**What it does:**
- ✅ Installs system dependencies (Python, pip, git)
- ✅ Creates virtual environment and installs Python packages
- ✅ Creates required directories (`logs/`, `state/`, `data/`, etc.)
- ✅ Prompts for secrets (API keys, Discord webhook)
- ✅ Installs and enables all systemd services and timers
- ✅ Validates installation

**Result:** Fully configured bot ready to run.

---

## Installation: Manual Setup (Alternative)

If you prefer manual setup or need custom configuration:

### 1. Create User Account

```bash
sudo useradd -m -s /bin/bash xsmom
sudo mkdir -p /opt/xsmom-bot
sudo chown xsmom:xsmom /opt/xsmom-bot
```

### 2. Clone Repository

```bash
sudo -u xsmom git clone <repository-url> /opt/xsmom-bot
cd /opt/xsmom-bot
```

### 3. Create Virtual Environment

```bash
sudo -u xsmom python3 -m venv venv
sudo -u xsmom ./venv/bin/pip install --upgrade pip
sudo -u xsmom ./venv/bin/pip install -r requirements.txt
```

### 4. Configure Environment

```bash
sudo -u xsmom cp .env.example .env
sudo -u xsmom cp config/config.yaml.example config/config.yaml

# Edit .env and add API keys
sudo -u xsmom nano .env
```

**`.env` file:**
```bash
BYBIT_API_KEY=your_api_key_here
BYBIT_API_SECRET=your_api_secret_here
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...  # Optional
```

**Set permissions:**
```bash
sudo chmod 600 /opt/xsmom-bot/.env
```

### 5. Configure Bot

```bash
# Edit config and adjust parameters
sudo -u xsmom nano config/config.yaml
```

**Minimum config changes:**
- Set `exchange.testnet: false` for production
- Review `risk.max_daily_loss_pct` (default: 5.0%)
- Adjust `strategy.gross_leverage` if needed (default: 0.95)

---

## systemd Service Setup

### 1. Main Bot Service

**File:** `/etc/systemd/system/xsmom-bot.service`

```ini
[Unit]
Description=xsmom-bot Trading Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=xsmom
WorkingDirectory=/opt/xsmom-bot
Environment="PYTHON_BIN=/opt/xsmom-bot/venv/bin/python"
Environment="PYTHONPATH=/opt/xsmom-bot"
EnvironmentFile=/opt/xsmom-bot/.env
ExecStart=/opt/xsmom-bot/venv/bin/python -m src.main live --config /opt/xsmom-bot/config/config.yaml
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=xsmom-bot

[Install]
WantedBy=multi-user.target
```

**Key settings:**
- `Restart=always` - Restart on crash
- `RestartSec=10` - Wait 10s before restart
- `EnvironmentFile` - Loads `.env` file
- `User=xsmom` - Runs as non-root user

### 2. Optimizer Service

**File:** `/etc/systemd/system/xsmom-optimizer-full-cycle.service`

```ini
[Unit]
Description=xsmom-bot Full-Cycle Optimizer
After=network-online.target

[Service]
Type=oneshot
User=xsmom
WorkingDirectory=/opt/xsmom-bot
Environment="PYTHON_BIN=/opt/xsmom-bot/venv/bin/python"
Environment="PYTHONPATH=/opt/xsmom-bot"
EnvironmentFile=/opt/xsmom-bot/.env
Environment="DEPLOY=true"
ExecStart=/opt/xsmom-bot/bin/run-optimizer-full-cycle.sh
TimeoutStartSec=7200
StandardOutput=journal
StandardError=journal
SyslogIdentifier=xsmom-optimizer

[Install]
WantedBy=multi-user.target
```

### 3. Optimizer Timer

**File:** `/etc/systemd/system/xsmom-optimizer-full-cycle.timer`

```ini
[Unit]
Description=Run xsmom-bot full-cycle optimizer weekly
Requires=xsmom-optimizer-full-cycle.service

[Timer]
OnCalendar=weekly
# Run every Sunday at 02:00 UTC
OnCalendar=*-*-Sun 02:00:00 UTC
AccuracySec=1m
Persistent=true

[Install]
WantedBy=timers.target
```

**Timer settings:**
- `OnCalendar=*-*-Sun 02:00:00 UTC` - Weekly on Sunday at 02:00 UTC
- `Persistent=true` - Run if missed (e.g., server was off)
- `AccuracySec=1m` - Accuracy within 1 minute

### 4. Daily Report Service

**File:** `/etc/systemd/system/xsmom-daily-report.service`

```ini
[Unit]
Description=xsmom-bot Daily Report
After=network-online.target

[Service]
Type=oneshot
User=xsmom
WorkingDirectory=/opt/xsmom-bot
Environment="PYTHON_BIN=/opt/xsmom-bot/venv/bin/python"
Environment="PYTHONPATH=/opt/xsmom-bot"
EnvironmentFile=/opt/xsmom-bot/.env
ExecStart=/opt/xsmom-bot/venv/bin/python -m src.reports.daily_report --config /opt/xsmom-bot/config/config.yaml
StandardOutput=journal
StandardError=journal
SyslogIdentifier=xsmom-daily-report

[Install]
WantedBy=multi-user.target
```

### 5. Daily Report Timer

**File:** `/etc/systemd/system/xsmom-daily-report.timer`

```ini
[Unit]
Description=Run daily report at 00:05 UTC
Requires=xsmom-daily-report.service

[Timer]
OnCalendar=*-*-* 00:05:00
AccuracySec=1m
Persistent=true

[Install]
WantedBy=timers.target
```

**Timer settings:**
- `OnCalendar=*-*-* 00:05:00` - Daily at 00:05 UTC
- `Persistent=true` - Run if missed

---

## Enable and Start Services

### 1. Reload systemd

```bash
sudo systemctl daemon-reload
```

### 2. Enable and Start Bot

```bash
sudo systemctl enable xsmom-bot.service
sudo systemctl start xsmom-bot.service
sudo systemctl status xsmom-bot.service
```

### 3. Enable and Start Optimizer Timer

```bash
sudo systemctl enable xsmom-optimizer-full-cycle.timer
sudo systemctl start xsmom-optimizer-full-cycle.timer
sudo systemctl status xsmom-optimizer-full-cycle.timer
```

### 4. Enable and Start Daily Report Timer

```bash
sudo systemctl enable xsmom-daily-report.timer
sudo systemctl start xsmom-daily-report.timer
sudo systemctl status xsmom-daily-report.timer
```

---

## Monitoring

### View Logs

**Bot logs:**
```bash
# Follow bot logs
journalctl -u xsmom-bot.service -f

# View recent logs
journalctl -u xsmom-bot.service -n 100

# View logs since boot
journalctl -u xsmom-bot.service -b
```

**Optimizer logs:**
```bash
# Follow optimizer logs
journalctl -u xsmom-optimizer-full-cycle.service -f

# View recent logs
journalctl -u xsmom-optimizer-full-cycle.service -n 100
```

**Daily report logs:**
```bash
# Follow daily report logs
journalctl -u xsmom-daily-report.service -f
```

### Check Service Status

```bash
# Bot status
sudo systemctl status xsmom-bot.service

# Optimizer timer status
sudo systemctl status xsmom-optimizer-full-cycle.timer

# Daily report timer status
sudo systemctl status xsmom-daily-report.timer

# List all timers
systemctl list-timers --all
```

### Check Bot Health

**Heartbeat:**
```bash
# Check heartbeat freshness (should be < 2 hours)
sudo -u xsmom cat /opt/xsmom-bot/state.json.heartbeat
```

**State file:**
```bash
# View current state
sudo -u xsmom cat /opt/xsmom-bot/state.json | jq '.'
```

---

## Service Management

### Restart Bot

```bash
sudo systemctl restart xsmom-bot.service
```

### Stop Bot

```bash
sudo systemctl stop xsmom-bot.service
```

### Start Bot

```bash
sudo systemctl start xsmom-bot.service
```

### Disable Auto-Start

```bash
sudo systemctl disable xsmom-bot.service
```

### Run Optimizer Manually

```bash
sudo systemctl start xsmom-optimizer-full-cycle.service
```

### Check Timer Next Run

```bash
systemctl list-timers xsmom-optimizer-full-cycle.timer
systemctl list-timers xsmom-daily-report.timer
```

---

## Troubleshooting

### Bot Not Starting

**Check logs:**
```bash
journalctl -u xsmom-bot.service -n 50
```

**Common issues:**
- API keys missing → Check `.env` file
- Config invalid → Check `config/config.yaml` for validation errors
- Permissions → Check file ownership (`sudo chown -R xsmom:xsmom /opt/xsmom-bot`)

### Bot Crashing

**Check restart count:**
```bash
systemctl status xsmom-bot.service | grep "Active:"
```

**Check logs for errors:**
```bash
journalctl -u xsmom-bot.service -n 100 | grep -i error
```

**Common causes:**
- Exchange API failures → Check network/API status
- State file corruption → Restore from backup or clear state
- Config errors → Validate config file

### Optimizer Not Running

**Check timer status:**
```bash
systemctl status xsmom-optimizer-full-cycle.timer
```

**Check next run:**
```bash
systemctl list-timers xsmom-optimizer-full-cycle.timer
```

**Run manually:**
```bash
sudo systemctl start xsmom-optimizer-full-cycle.service
journalctl -u xsmom-optimizer-full-cycle.service -f
```

---

## File Permissions

### Required Permissions

```bash
# Repository files
sudo chown -R xsmom:xsmom /opt/xsmom-bot

# State file (must be writable)
sudo chmod 644 /opt/xsmom-bot/state.json

# Logs directory (must be writable)
sudo chmod 755 /opt/xsmom-bot/logs

# Config directory (must be readable)
sudo chmod 755 /opt/xsmom-bot/config
```

### Security Best Practices

1. **Don't run as root** - Use non-root user (`xsmom`)
2. **Protect `.env` file** - `chmod 600 /opt/xsmom-bot/.env`
3. **Protect API keys** - Never commit `.env` to git
4. **Limit file access** - Restrict permissions to user only

---

## Backup and Recovery

### Backup State File

```bash
# Backup state file before config changes
sudo -u xsmom cp /opt/xsmom-bot/state.json /opt/xsmom-bot/state.json.backup
```

### Backup Config

```bash
# Backup live config before optimizer deployment
sudo -u xsmom cp /opt/xsmom-bot/config/config.yaml /opt/xsmom-bot/config/config.yaml.backup
```

### Rollback Config

```bash
# Rollback to previous config
python -m src.optimizer.rollback_cli --to latest

# Or restore from backup
sudo -u xsmom cp /opt/xsmom-bot/config/config.yaml.backup /opt/xsmom-bot/config/config.yaml
sudo systemctl restart xsmom-bot.service
```

---

## Next Steps

- **Monitoring**: [`monitoring_and_alerts.md`](monitoring_and_alerts.md) - Health checks, alerts
- **Troubleshooting**: [`troubleshooting.md`](troubleshooting.md) - Common issues
- **Discord Notifications**: [`../usage/discord_notifications.md`](../usage/discord_notifications.md) - Setup alerts

---

**Motto: MAKE MONEY** — with reliable 24/7 unattended operation. 📈

