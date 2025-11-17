# Discord Notifications - Implementation Summary

## Design Overview

The Discord notification system provides automated alerts for:
1. **Optimizer Results** - Detailed summaries after each full-cycle optimization run
2. **Daily Reports** - Performance summaries including PnL, trades, win rate, equity growth

### Key Features

- **Dual Webhook Support**: Environment variable (`DISCORD_WEBHOOK_URL`) takes precedence over config file
- **Non-Blocking**: Notification failures never crash the bot or optimizer
- **Rich Embeds**: Discord embeds with color coding (green/orange/red) based on performance
- **Safe by Default**: Notifications disabled until explicitly enabled

### Webhook Resolution Order

1. `DISCORD_WEBHOOK_URL` environment variable (primary)
2. `notifications.discord.webhook_url` from config.yaml (fallback)
3. If neither exists, notification is skipped (logged as warning)

## New/Modified Files

### Core Notification Modules

1. **`src/notifications/__init__.py`** - Package initialization
2. **`src/notifications/discord_notifier.py`** - Discord webhook client with embed support
   - Handles rate limiting with automatic retry
   - Safe error handling (never crashes caller)
   - Support for simple messages and rich embeds

3. **`src/notifications/optimizer_notifications.py`** - Optimizer result formatter
   - Formats optimization results as Discord embeds
   - Includes baseline vs candidate metrics
   - Parameter highlights
   - Deployment decision status

### Daily Report Module

4. **`src/reports/__init__.py`** - Package initialization
5. **`src/reports/daily_report.py`** - Daily performance report generator
   - Aggregates daily PnL from state file
   - Computes cumulative metrics (total PnL, max DD)
   - Fetches current equity from exchange
   - Sends formatted Discord notification

### Configuration

6. **`src/config.py`** - Added `NotificationsCfg` and `DiscordCfg` classes
   - Integrated into `AppConfig`
   - Default values set in `_merge_defaults()`

7. **`config/config.yaml.example`** - Added notifications section with example
   - Includes security warning about webhook URLs
   - Shows all available settings

### Optimizer Integration

8. **`src/optimizer/full_cycle.py`** - Integrated Discord notifications
   - Sends notification after optimization completes
   - Includes run metadata in result dict for notifications

### Dependencies

9. **`requirements.txt`** - Added `requests==2.31.0` for HTTP requests

### Documentation

10. **`docs/discord_notifications.md`** - Comprehensive documentation
    - Setup instructions
    - Configuration examples
    - Usage examples
    - Troubleshooting guide

## Usage Examples

### Optimizer with Notifications

```bash
# Set webhook via environment variable (recommended)
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."

# Run optimizer (notification sent automatically if enabled)
python -m src.optimizer.full_cycle \
  --base-config config/config.yaml \
  --live-config config/config.yaml \
  --train-days 120 \
  --oos-days 30 \
  --bo-evals 100 \
  --mc-runs 1000 \
  --deploy
```

**Or using config file:**

```yaml
# config/config.yaml
notifications:
  discord:
    enabled: true
    send_optimizer_results: true
    webhook_url: "https://discord.com/api/webhooks/..."
```

```bash
python -m src.optimizer.full_cycle \
  --base-config config/config.yaml \
  --deploy
```

### Daily Report Manual Run

```bash
# With Discord notification
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
python -m src.reports.daily_report --config config/config.yaml

# Without Discord notification (print only)
python -m src.reports.daily_report \
  --config config/config.yaml \
  --no-notify

# For specific date
python -m src.reports.daily_report \
  --config config/config.yaml \
  --date 2025-11-16
```

### Scheduled Daily Reports (Cron)

```bash
# Add to crontab: run daily at 00:05 UTC
5 0 * * * DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..." \
  /opt/xsmom-bot/venv/bin/python -m src.reports.daily_report \
  --config /opt/xsmom-bot/config/config.yaml \
  >> /opt/xsmom-bot/logs/daily_report.log 2>&1
```

### Scheduled Daily Reports (systemd Timer)

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
```

## Configuration

### Config File Example

```yaml
notifications:
  discord:
    enabled: true                      # Master enable/disable
    send_optimizer_results: true       # Send optimizer notifications
    send_daily_report: true            # Send daily reports
    # Fallback webhook if DISCORD_WEBHOOK_URL env var is not set.
    # NOTE: This is the actual webhook; do NOT commit this to a public repo.
    webhook_url: "https://discord.com/api/webhooks/..."
```

### Environment Variables

- `DISCORD_WEBHOOK_URL` - Primary webhook URL (takes precedence)

## Notification Content

### Optimizer Results Embed

**Fields:**
- **Run Info**: WFO segments, BO trials, MC runs, candidates evaluated
- **Baseline (Live)**: OOS Sharpe, CAGR, Max DD, Calmar
- **Candidate**: Same metrics with improvements highlighted
- **Parameter Highlights**: Key changed parameters (signal_power, leverage, k_min/k_max, etc.)
- **Decision**: Deployment status and config path

**Colors:**
- 🟢 Green: New config deployed
- 🟠 Orange: Optimization successful but no deployment
- 🔴 Red: No valid candidate found

### Daily Report Embed

**Fields:**
- **Today**: PnL ($ and %), trades, win rate, largest win/loss, intraday DD
- **Since Start**: Total PnL, equity growth, max drawdown
- **Equity**: Day start, current, day high

**Colors:**
- 🟢 Green: PnL > 2%
- 🔵 Blue: PnL > 0%
- 🟠 Orange: PnL > -2%
- 🔴 Red: PnL < -2%

## Data Sources

### Optimizer Results

- **Source**: `run_full_cycle()` return dict
- **Metadata**: Included in result dict (wfo_segments, bo_trials_per_segment, mc_runs)
- **Metrics**: Baseline and candidate aggregated metrics from WFO segments

### Daily Report

- **State File**: `cfg.paths.state_path` (default: `/opt/xsmom-bot/state.json`)
  - `day_start_equity`: Equity at start of UTC day
  - `day_high_equity`: Highest equity during day
  - `sym_stats`: Per-symbol trade statistics
  - `start_equity`: Starting equity (for cumulative metrics)
  - `peak_equity`: Peak equity (for max DD calculation)
  - `max_drawdown_pct`: Maximum drawdown %

- **Exchange**: `ex.get_equity_usdt()` - Current equity

## Error Handling

All notification functions are **non-blocking**:

- Failures are logged as warnings (not errors)
- Caller continues execution even if notification fails
- Rate limiting handled with automatic retry (once)
- Missing webhook URL logged as warning, not error

**Example:**
```python
# In optimizer or daily report
try:
    send_notification(...)
except Exception as e:
    log.warning(f"Notification failed: {e}")  # Non-fatal
# Bot/optimizer continues normally
```

## Testing

### Test Discord Notifier Directly

```python
from src.notifications.discord_notifier import DiscordNotifier

notifier = DiscordNotifier(webhook_url="https://discord.com/api/webhooks/...")
notifier.send_message("Test message")
notifier.send_embed(
    title="Test",
    description="Testing Discord notifications",
    fields=[{"name": "Field", "value": "Value", "inline": False}],
    color=DiscordNotifier.COLOR_BLUE,
)
```

### Test Daily Report (No Notification)

```bash
python -m src.reports.daily_report \
  --config config/config.yaml \
  --no-notify
```

## Security Considerations

1. **Never commit webhook URLs** to public repositories
2. **Use environment variables** for production deployments
3. **Rotate webhook URLs** periodically
4. **Restrict Discord channel** permissions (read-only for others)
5. **Monitor webhook usage** in Discord server settings

## Troubleshooting

### Notifications Not Sending

1. **Check config**: `notifications.discord.enabled = true`
2. **Check webhook URL**: Verify in Discord server settings
3. **Check logs**: Look for "Discord webhook URL not available"
4. **Test directly**: Use `send_message()` function directly

### Rate Limiting

- Discord limits: 30 requests/minute per webhook
- Notifier automatically retries once with backoff
- If retry fails, notification is skipped (non-fatal)
- Consider reducing notification frequency if rate-limited frequently

### Missing Data in Reports

- **Daily report**: Ensure state file exists and contains valid data
- **Equity fetch**: Check exchange API keys and connection
- **Trade stats**: Verify `sym_stats` in state file

---

**Motto: MAKE MONEY** — stay informed via Discord notifications on optimizer results and daily performance.

