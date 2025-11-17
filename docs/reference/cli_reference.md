# CLI Reference

## Main Entry Point

### `src.main`

**Command:**
```bash
python -m src.main [mode] [options]
```

**Modes:**
- `live` - Run live trading loop
- `backtest` - Run backtest

---

## Live Trading

### Command

```bash
python -m src.main live --config config/config.yaml [options]
```

### Options

- `--config` (required) - Path to config file (default: `config/config.yaml`)
- `--dry` (optional) - Dry run mode (no actual orders, defaults to false)

### Examples

```bash
# Run live trading
python -m src.main live --config config/config.yaml

# Dry run (no orders)
python -m src.main live --config config/config.yaml --dry
```

### Output

**Console:**
```
Starting live loop (mode=LIVE)
Fast SL/TP loop starting: check every 2s on timeframe=5m
=== Cycle start 2025-11-17T10:00:00Z ===
Equity: $1000.00 | Positions: 0
Universe: 36 symbols
```

**Logs:**
- Console output (stdout/stderr)
- File logs (`config/paths.logs_dir`, default: `/opt/xsmom-bot/logs`)
- Systemd logs (`journalctl -u xsmom-bot.service`)

---

## Backtesting

### Command

```bash
python -m src.main backtest --config config/config.yaml [options]
```

### Options

- `--config` (required) - Path to config file (default: `config/config.yaml`)
- Additional options (if supported by backtest CLI)

### Examples

```bash
# Run backtest
python -m src.main backtest --config config/config.yaml
```

### Output

**Console:**
```
=== BACKTEST (cost-aware) ===
Samples: 1440 bars  |  Universe size: 36
Total Return: 15.23% | Annualized: 42.15% | Sharpe: 1.45
Max Drawdown: -12.34% | Calmar: 3.41
```

**Logs:**
- Console output (stdout/stderr)
- File logs (`config/paths.logs_dir`)

---

## Optimizer CLI

### Full-Cycle Optimizer

**Command:**
```bash
python -m src.optimizer.full_cycle [options]
```

**Options:**
- `--base-config` - Path to base config (default: `config/config.yaml`)
- `--live-config` - Path to live config (default: `config/config.yaml`)
- `--symbol-universe` - Comma-separated symbol list (if None, fetched from exchange)
- `--train-days` - Training window size (default: 120 days)
- `--oos-days` - Out-of-sample window size (default: 30 days)
- `--embargo-days` - Embargo between train and OOS (default: 2 days)
- `--bo-evals` - Number of BO trials per segment (default: 100)
- `--bo-startup` - Random trials before BO (default: 10)
- `--mc-runs` - Number of MC runs (default: 1000)
- `--min-improve-sharpe` - Minimum Sharpe improvement (default: 0.05)
- `--min-improve-annualized` - Minimum annualized return improvement (default: 0.03)
- `--max-dd-increase` - Maximum drawdown increase tolerance (default: 0.05)
- `--tail-dd-limit` - Catastrophic DD threshold (default: 0.70)
- `--deploy` - Deploy new config if approved (default: false)
- `--seed` - Random seed (optional)
- `--output` - Output JSON file (optional)

**Examples:**

```bash
# Run optimizer (no deployment)
python -m src.optimizer.full_cycle \
  --base-config config/config.yaml \
  --train-days 120 \
  --oos-days 30 \
  --bo-evals 100 \
  --mc-runs 1000

# Run optimizer (with deployment)
python -m src.optimizer.full_cycle \
  --base-config config/config.yaml \
  --train-days 120 \
  --oos-days 30 \
  --bo-evals 100 \
  --mc-runs 1000 \
  --deploy
```

**Output:**
- Console output (progress, results)
- Log file (`logs/optimizer/full_cycle_YYYYMMDD_HHMMSS.log`)
- Config file (`config/optimized/config_YYYYMMDD_HHMMSS.yaml`)
- Metadata file (`config/optimized/metadata_YYYYMMDD_HHMMSS.json`)

See [`../usage/optimizer.md`](../usage/optimizer.md) for detailed documentation.

---

## Rollback CLI

### Rollback Config

**Command:**
```bash
python -m src.optimizer.rollback_cli [options]
```

**Options:**
- `--live-config` - Path to live config (default: `config/config.yaml`)
- `--to` - Timestamp or 'latest' to roll back to
- `--list` - List all available optimized configs

**Examples:**

```bash
# List available configs
python -m src.optimizer.rollback_cli --list

# Rollback to latest
python -m src.optimizer.rollback_cli --to latest

# Rollback to specific timestamp
python -m src.optimizer.rollback_cli --to 20251117_023000
```

**Output:**
- Console output (list of configs, rollback status)
- Live config file updated (with backup created)

---

## Daily Report CLI

### Daily Report

**Command:**
```bash
python -m src.reports.daily_report [options]
```

**Options:**
- `--config` - Path to config file (default: `config/config.yaml`)
- `--date` - Report date (YYYY-MM-DD, defaults to today UTC)
- `--no-notify` - Don't send Discord notification

**Examples:**

```bash
# Generate daily report (with Discord notification)
python -m src.reports.daily_report --config config/config.yaml

# Generate daily report (no notification)
python -m src.reports.daily_report --config config/config.yaml --no-notify

# Generate report for specific date
python -m src.reports.daily_report --config config/config.yaml --date 2025-11-16
```

**Output:**
- Console output (daily metrics summary)
- Discord notification (if enabled and `--no-notify` not set)
- Log file (if configured)

See [`../usage/discord_notifications.md`](../usage/discord_notifications.md) for Discord setup.

---

## KB Update Tool

### Update KB

**Command:**
```bash
python -m tools.update_kb [options]
```

**Options:**
- `--repo-root` - Repository root directory (default: `.`)
- `--skip-module-map` - Skip module map generation
- `--skip-config-ref` - Skip config reference generation

**Examples:**

```bash
# Update all KB docs
python -m tools.update_kb

# Only update module map
python -m tools.update_kb --skip-config-ref

# Only update config reference
python -m tools.update_kb --skip-module-map
```

**Output:**
- `docs/architecture/module_map.md` - Module tree (auto-generated)
- `docs/reference/config_reference.md` - Config parameters (auto-generated)
- `docs/kb/knowledge_base.md` - KB timestamp updated

---

## Environment Variables

### Exchange API Keys

- `BYBIT_API_KEY` - Bybit API key
- `BYBIT_API_SECRET` - Bybit API secret
- `API_KEY` - Alternative name for API key
- `API_SECRET` - Alternative name for API secret

### Discord Notifications

- `DISCORD_WEBHOOK_URL` - Discord webhook URL (primary, takes precedence over config)

### Python Environment

- `PYTHONPATH` - Python path (default: repository root)
- `PYTHONUNBUFFERED` - Unbuffered output (recommended: `1`)

---

## Exit Codes

### Success

- `0` - Success

### Errors

- `1` - General error
- `2` - Invalid arguments
- `3` - Config validation error
- `4` - Exchange API error

---

## Next Steps

- **Config Reference**: [`config_reference.md`](config_reference.md) - Complete parameter list
- **Usage Guides**: [`../usage/`](../usage/) - Detailed usage documentation
- **Operations**: [`../operations/`](../operations/) - Deployment and troubleshooting

---

**Motto: MAKE MONEY** — with clear, easy-to-use command-line interfaces. 📈

