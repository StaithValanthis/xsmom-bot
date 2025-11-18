# Optimizer Service

The optimizer service provides database-backed, continuous parameter optimization with historical lookup and bad-combo filtering.

## Overview

The optimizer service extends the full-cycle optimizer with:

- **SQLite database persistence**: All trials and studies are stored in a SQLite database
- **Optuna warm-start**: Studies persist across runs, enabling continuous optimization
- **Historical lookup**: Skips already-tested parameter combinations
- **Bad-combo filtering**: Automatically avoids known poor parameter regions
- **Query interface**: Inspect historical results and identify best/worst parameter regions

## Architecture

### Database Schema

The optimizer uses SQLite with three main tables:

1. **`studies`**: Optuna study metadata
   - `id`, `name`, `description`, `config_hash`, `created_at`, `updated_at`

2. **`trials`**: Individual trial results
   - `id`, `study_id`, `optuna_trial_number`, `status`, `params_json`, `params_hash`, `metrics_json`, `score`, `created_at`, `updated_at`

3. **`bad_combinations`**: Marked bad parameter combinations
   - `id`, `study_id`, `params_hash`, `reason`, `score`, `created_at`

### Study Naming

Studies are named deterministically based on:
- Symbol universe
- Timeframe
- Strategy version/schema

Example: `xsmom_wfo_v1_BTCUSDT_ETHUSDT_1h_a1b2c3d4`

This ensures the same study is resumed across runs for the same configuration.

## Usage

### Run Once

Run a single optimization cycle:

```bash
python -m src.optimizer.service run-once \
  --base-config config/config.yaml \
  --live-config config/config.yaml \
  --db-path data/optimizer.db \
  --trials 25
```

### Watch Mode (Continuous)

Run continuously with periodic optimization cycles:

```bash
python -m src.optimizer.service watch \
  --base-config config/config.yaml \
  --live-config config/config.yaml \
  --db-path data/optimizer.db \
  --trials-per-iter 10 \
  --sleep-seconds 1800
```

This runs 10 trials every 30 minutes (1800 seconds).

### Systemd Service

For production, use the systemd service:

```bash
# Copy service file
sudo cp systemd/xsmom-optimizer-service.service /etc/systemd/system/

# Edit paths if needed (default: /opt/xsmom-bot)
sudo nano /etc/systemd/system/xsmom-optimizer-service.service

# Reload systemd
sudo systemctl daemon-reload

# Enable and start
sudo systemctl enable xsmom-optimizer-service
sudo systemctl start xsmom-optimizer-service

# Check status
sudo systemctl status xsmom-optimizer-service

# View logs
sudo journalctl -u xsmom-optimizer-service -f
```

## Query Interface

### List Studies

```bash
python -m src.optimizer.query list-studies --db-path data/optimizer.db
```

### Top Trials

```bash
# By study name
python -m src.optimizer.query top-trials \
  --study-name xsmom_wfo_v1_BTCUSDT_1h_a1b2c3d4 \
  --limit 20

# By study ID
python -m src.optimizer.query top-trials \
  --study-id 1 \
  --limit 20
```

### Show Trial Details

```bash
python -m src.optimizer.query show-trial \
  --study-id 1 \
  --trial-number 42
```

### Bad Regions

```bash
# Worst trials across all studies
python -m src.optimizer.query bad-regions --limit 20

# Worst trials for a specific study
python -m src.optimizer.query bad-regions --study-id 1 --limit 20
```

## Configuration

See `config/config.yaml.example` for optimizer service settings:

```yaml
optimizer:
  # Database and persistence
  db_path: "data/optimizer.db"
  study_name_prefix: "xsmom_wfo"
  
  # Historical lookup and filtering
  skip_known_params: true
  enable_bad_combo_filter: true
  bad_combo_min_score: -1.0
  bad_combo_dd_threshold: 0.3
```

### Configuration Options

- **`db_path`**: Path to SQLite database file (default: `data/optimizer.db`)
- **`study_name_prefix`**: Prefix for Optuna study names (default: `xsmom_wfo`)
- **`skip_known_params`**: Skip already-tested parameter combinations (default: `true`)
- **`enable_bad_combo_filter`**: Enable bad combination filtering (default: `true`)
- **`bad_combo_min_score`**: Below this score = bad combo (default: `-1.0`)
- **`bad_combo_dd_threshold`**: Max drawdown threshold (default: `0.3` = 30%)

## How It Works

### Historical Lookup

Before running a trial, the optimizer:

1. Computes a deterministic hash of the parameter combination
2. Queries the database for existing trials with the same hash
3. If found:
   - Reuses the existing score (if available)
   - Skips the backtest entirely
   - Logs the skip reason

This prevents re-running identical parameter combinations.

### Bad-Combo Filtering

The optimizer automatically marks parameter combinations as "bad" if:

- Score < `bad_combo_min_score`
- Max drawdown > `bad_combo_dd_threshold`
- Sharpe < -0.5 (hardcoded threshold)

Bad combinations are:
- Stored in the `bad_combinations` table
- Checked before running new trials
- Pruned immediately if detected

### Optuna Warm-Start

Optuna studies are persisted to SQLite using:

```python
storage_url = f"sqlite:///{db_path}"
study = optuna.create_study(
    study_name=study_name,
    storage=storage_url,
    load_if_exists=True,
)
```

This enables:
- Resuming optimization across runs
- Bayesian optimizer learning from past trials
- No wasted compute on duplicate trials

## Benefits

1. **Faster convergence**: Bayesian optimizer learns from historical trials
2. **No wasted compute**: Skips duplicate parameter combinations
3. **Better decisions**: Identifies consistently poor parameter regions
4. **Analytics**: Query historical results to understand parameter performance
5. **Continuous optimization**: Run as a background service

## Troubleshooting

### Database Locked

If you see "database is locked" errors:

- Ensure only one optimizer service instance is running
- Check for stale processes: `ps aux | grep optimizer`
- Consider using a file-based lock if running multiple instances

### Study Not Found

If a study is not found:

- Check study name computation (symbols, timeframe, config hash)
- Verify database path is correct
- List studies: `python -m src.optimizer.query list-studies`

### Too Many Skipped Trials

If many trials are skipped:

- Review `skip_known_params` setting
- Check database for existing trials: `python -m src.optimizer.query top-trials`
- Consider clearing old studies if needed (backup first!)

## Integration with Full-Cycle Optimizer

The optimizer service integrates seamlessly with the existing full-cycle optimizer:

- Uses the same WFO + BO + MC pipeline
- Respects all existing configuration options
- Adds database persistence and filtering on top

The service is backward compatible: if `db`, `study_id`, etc. are not provided, it falls back to in-memory operation (no persistence).

