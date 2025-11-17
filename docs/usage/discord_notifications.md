# Discord Notifications

## Overview

The xsmom-bot Discord notification system sends automated alerts for:

1. **Optimizer Results** - Detailed summaries after each optimization run
2. **Daily Reports** - Performance and equity growth summaries

All notifications use Discord webhooks and are designed to be **non-blocking** - failures to send notifications will not crash the bot or optimizer.

## Setup

### 1. Create Discord Webhook

1. Open your Discord server
2. Go to **Server Settings** → **Integrations** → **Webhooks**
3. Click **New Webhook**
4. Configure:
   - **Name**: e.g., "xsmom-bot"
   - **Channel**: Select channel for notifications
5. Click **Copy Webhook URL**

### 2. Configure Webhook URL

You have **two options**:

#### Option A: Environment Variable (Recommended)

Set the `DISCORD_WEBHOOK_URL` environment variable:

```bash
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
```

This is the **primary** method - it takes precedence over config file.

**For systemd services:**

Edit `/opt/xsmom-bot/.env` or the systemd service file:

```ini
[Service]
Environment="DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/..."
```

#### Option B: Config File (Fallback)

Edit `config/config.yaml`:

```yaml
notifications:
  discord:
    enabled: true
    send_optimizer_results: true
    send_daily_report: true
    # Fallback webhook if DISCORD_WEBHOOK_URL env var is not set.
    # NOTE: This is the actual webhook; do NOT commit this to a public repo.
    webhook_url: "https://discord.com/api/webhooks/..."
```

**⚠️ SECURITY WARNING:** Do **NOT** commit real webhook URLs to public repositories. The config file fallback is intended for local/private deployments.

### 3. Enable Notifications

By default, notifications are **disabled**. Enable them in `config/config.yaml`:

```yaml
notifications:
  discord:
    enabled: true
    send_optimizer_results: true  # Send optimizer result notifications
    send_daily_report: true       # Send daily performance reports
```

## Notification Types

### Optimizer Results

Sent automatically after each full-cycle optimizer run (`src/optimizer/full_cycle.py`).

**Includes:**
- Deployment status (✅ Deployed / ⚠️ No Change / ❌ Failed)
- Baseline metrics (current live config)
- Candidate metrics (optimized config)
- Parameter highlights (key changed parameters)
- Run info (WFO segments, BO trials, MC runs)

**Example:**
```
Title: XSMOM Optimizer Run – ✅ Deployed New Config

Run Info:
- WFO segments: 8
- BO trials/segment: 100
- MC runs: 1000
- Candidates: 3

Baseline (Live):
- Sharpe: 1.23
- CAGR: 45.2%
- Max DD: -15.0%

Candidate:
- Sharpe: 1.45 (+0.22)
- CAGR: 48.2% (+3.0%)
- MC 99% DD: -18.0%

Parameter Highlights:
- signal_power: 1.45
- gross_leverage: 1.5
- k_min: 2
- k_max: 6

Decision:
✅ Deployed: config_20251117_023000.yaml
```

### Daily Report

Sent manually via CLI or scheduled via cron/systemd timer.

**Includes:**
- Today's PnL (absolute and %)
- Trade count and win rate
- Largest win/loss
- Intraday drawdown
- Cumulative metrics (total PnL, equity growth, max DD)

**Example:**
```
Title: XSMOM Daily Report – 2025-11-17

Description: Today: +1.23% | Trades: 42 | Win rate: 57.1%

Today:
- PnL: $+123.45 (+1.23%)
- Trades: 42
- Win rate: 57.1%
- Largest win: $45.67
- Largest loss: $12.34
- Max intraday DD: -0.45%

Since Start:
- Total PnL: $+1234.56 (+12.35%)
- Equity: $11234.56 (start: $10000.00)
- Max DD: -5.67%

Equity:
- Day start: $10000.00
- Current: $10123.45
- Day high: $10150.00
```

## Usage

### Optimizer Notifications

Notifications are sent **automatically** when running the full-cycle optimizer:

```bash
# With Discord notifications (if enabled in config)
python -m src.optimizer.full_cycle \
  --base-config config/config.yaml \
  --deploy
```

The optimizer will send a notification **regardless** of whether a new config was deployed or not.

### Daily Reports

Run manually:

```bash
# With Discord notification
DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..." \
python -m src.reports.daily_report --config config/config.yaml

# Without Discord notification (print only)
python -m src.reports.daily_report --config config/config.yaml --no-notify
```

### Scheduled Daily Reports

**Via cron:**

```bash
# Run daily at 00:05 UTC
5 0 * * * DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..." \
  /opt/xsmom-bot/venv/bin/python -m src.reports.daily_report \
  --config /opt/xsmom-bot/config/config.yaml \
  >> /opt/xsmom-bot/logs/daily_report.log 2>&1
```

**Via systemd timer:**

Create `systemd/xsmom-daily-report.service`:

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

ExecStart=/opt/xsmom-bot/venv/bin/python -m src.reports.daily_report \
  --config /opt/xsmom-bot/config/config.yaml

StandardOutput=journal
StandardError=journal
SyslogIdentifier=xsmom-daily-report
```

And `systemd/xsmom-daily-report.timer`:

```ini
[Unit]
Description=Run daily report at 00:05 UTC
Requires=xsmom-daily-report.service

[Timer]
OnCalendar=*-*-* 00:05:00
AccuracySec=1m

[Install]
WantedBy=timers.target
```

Enable and start:

```bash
sudo systemctl enable xsmom-daily-report.timer
sudo systemctl start xsmom-daily-report.timer
sudo systemctl status xsmom-daily-report.timer
```

## Configuration

### Notification Settings

```yaml
notifications:
  discord:
    enabled: true                    # Master enable/disable
    send_optimizer_results: true     # Send optimizer notifications
    send_daily_report: true          # Send daily reports
    webhook_url: null                # Fallback (only if env var not set)
```

### Environment Variables

- `DISCORD_WEBHOOK_URL` - Primary webhook URL (takes precedence over config)

## Troubleshooting

### "Discord webhook URL not available"

**Cause:** No webhook URL found in environment or config.

**Fix:**
1. Set `DISCORD_WEBHOOK_URL` environment variable, OR
2. Set `notifications.discord.webhook_url` in config.yaml

### "Discord notifications disabled"

**Cause:** `notifications.discord.enabled` is `false` in config.

**Fix:** Set `enabled: true` in `config/config.yaml`.

### Notifications not sending

**Possible causes:**
1. Webhook URL invalid or expired
2. Rate limiting (Discord limits: 30 requests/minute per webhook)
3. Network/firewall blocking Discord API

**Check logs:**
```bash
journalctl -u xsmom-optimizer-full-cycle -f  # For optimizer
journalctl -u xsmom-daily-report -f          # For daily reports
```

The bot/optimizer will **log warnings** but **not crash** if Discord notifications fail.

### Rate Limiting

If you see "Discord rate limited" in logs:

- The notifier automatically retries after the specified delay
- If retry fails, notification is skipped (non-fatal)
- Consider reducing notification frequency if rate-limited frequently

## Security Best Practices

1. **Never commit webhook URLs** to public repositories
2. **Use environment variables** for webhook URLs in production
3. **Rotate webhook URLs** periodically (delete old, create new)
4. **Restrict Discord channel permissions** (read-only for others)
5. **Monitor webhook usage** in Discord server settings

## Examples

### Example: Optimizer with Notifications

```bash
# Set webhook via environment
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."

# Run optimizer (will send notification automatically if enabled)
python -m src.optimizer.full_cycle \
  --base-config config/config.yaml \
  --live-config config/config.yaml \
  --train-days 120 \
  --oos-days 30 \
  --bo-evals 100 \
  --mc-runs 1000 \
  --deploy
```

### Example: Daily Report Manual Run

```bash
# Set webhook via environment
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."

# Run daily report
python -m src.reports.daily_report --config config/config.yaml
```

### Example: Daily Report with Custom Date

```bash
# Report for specific date
python -m src.reports.daily_report \
  --config config/config.yaml \
  --date 2025-11-16
```

---

**Motto: MAKE MONEY** — stay informed via Discord notifications on optimizer results and daily performance.

