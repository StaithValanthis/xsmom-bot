# Automated Optimizer - Implementation Summary

## High-Level Design

The full-cycle automated optimizer implements a **robust, self-improving parameter optimization pipeline** that:

1. **Uses Walk-Forward Optimization (WFO)** to reduce overfitting
2. **Leverages Bayesian Optimization (BO)** via Optuna for efficient parameter search
3. **Applies Monte Carlo Stress Testing** to assess tail risk
4. **Safely deploys** new configs with versioning and rollback capability
5. **Requires zero manual intervention** once configured

### Pipeline Flow

```
Historical Data (Bybit OHLCV)
    ↓
Walk-Forward Segments (train/OOS windows)
    ↓
For each segment:
    - Bayesian Optimization on training data
    - Select top K parameter sets
    ↓
Evaluate top candidates on OOS segments
    - Monte Carlo stress tests
    - Aggregate metrics
    ↓
Compare to baseline (current live config)
    - Check improvement thresholds
    - Check safety constraints
    ↓
If approved: Deploy new config (with backup)
Else: Keep existing config
```

## New/Modified Files

### Core Optimizer Modules

1. **`src/optimizer/__init__.py`** - Package initialization
2. **`src/optimizer/backtest_runner.py`** - Clean backtest entrypoint with parameter overrides
3. **`src/optimizer/walk_forward.py`** - Walk-forward optimization pipeline
4. **`src/optimizer/bo_runner.py`** - Bayesian optimization using Optuna TPE
5. **`src/optimizer/monte_carlo.py`** - Monte Carlo stress testing (bootstrap + cost perturbations)
6. **`src/optimizer/config_manager.py`** - Config versioning, deployment, rollback
7. **`src/optimizer/full_cycle.py`** - Full-cycle orchestrator (main entrypoint)
8. **`src/optimizer/rollback_cli.py`** - CLI for rolling back to previous configs

### Scripts & Configuration

9. **`bin/run-optimizer-full-cycle.sh`** - Shell script wrapper for full-cycle optimizer
10. **`requirements.txt`** - Added `optuna==3.6.1` for Bayesian optimization
11. **`docs/optimizer.md`** - Comprehensive documentation

## Key Features

### 1. Walk-Forward Optimization (WFO)

- **Purged WFO** with embargo between train and OOS windows
- **Configurable windows**: train_days, oos_days, embargo_days
- **Multiple segments** tested in parallel or sequentially
- **Aggregate metrics** across all OOS segments (mean, std, stability)

### 2. Bayesian Optimization

- **Optuna TPE sampler** for efficient parameter exploration
- **Custom parameter space** definition (continuous, integer, categorical, log-uniform)
- **Default 18 parameters** optimized (signals, filters, risk, sizing)
- **Configurable trials**: n_trials, n_startup_trials

### 3. Monte Carlo Stress Testing

- **Bootstrap method**: Resample trades with replacement (block bootstrap for autocorrelation)
- **Cost perturbations**: Inject randomness into slippage, fees, funding
- **Tail risk metrics**: p95/p99 max drawdown, worst-case scenarios
- **Catastrophic risk filter**: Reject candidates with >70% tail DD

### 4. Safe Deployment

- **Versioned configs**: `config/optimized/config_YYYYMMDD_HHMMSS.yaml`
- **Metadata tracking**: Performance metrics, parameters, WFO segment info
- **Automatic backups**: Current live config backed up before deployment
- **Rollback capability**: CLI to rollback to any previous version
- **Config validation**: Pydantic validation before deployment

### 5. Safety Guards

- **Improvement thresholds**: Min Sharpe improvement (default: 0.05), min annualized improvement (default: 0.03)
- **Drawdown limits**: Max DD increase allowed (default: 0.05), tail DD limit (default: 0.70)
- **Trade count checks**: Minimum trades required (implicit in backtest)
- **No deployment by default**: Must explicitly pass `--deploy` flag

## Parameter Space

The optimizer optimizes **18 core parameters** (based on PARAMETER_REVIEW.md recommendations):

### Signals (6 params)
- `strategy.signal_power` [1.0, 2.0]
- `strategy.lookbacks[0]` [6, 24] (short lookback, hours)
- `strategy.lookbacks[1]` [12, 48] (medium lookback, hours)
- `strategy.lookbacks[2]` [24, 96] (long lookback, hours)
- `strategy.k_min` [2, 4]
- `strategy.k_max` [4, 8]

### Filters (3 params)
- `strategy.regime_filter.ema_len` [100, 300]
- `strategy.regime_filter.slope_min_bps_per_day` [1.0, 5.0]
- `strategy.entry_zscore_min` [0.0, 1.0]

### Risk (5 params - tight ranges)
- `risk.atr_mult_sl` [1.5, 3.0]
- `risk.trail_atr_mult` [0.5, 1.5]
- `strategy.gross_leverage` [1.0, 2.0]
- `strategy.max_weight_per_asset` [0.10, 0.30]
- `strategy.portfolio_vol_target.target_ann_vol` [0.20, 0.50]

### Enable/Disable (4 params - binary)
- `filters.regime_filter.enabled` [true, false]
- `filters.adx_filter.enabled` [true, false]
- `sizing.vol_target_enabled` [true, false]
- `sizing.diversify_enabled` [true, false]

## Usage Examples

### Manual Run (No Deployment)

```bash
python -m src.optimizer.full_cycle \
  --base-config config/config.yaml \
  --live-config config/config.yaml \
  --train-days 120 \
  --oos-days 30 \
  --bo-evals 100 \
  --mc-runs 1000
```

### Manual Run with Deployment

```bash
python -m src.optimizer.full_cycle \
  --base-config config/config.yaml \
  --live-config config/config.yaml \
  --deploy \
  --train-days 120 \
  --oos-days 30 \
  --bo-evals 100 \
  --mc-runs 1000
```

### Debug Run (Faster)

```bash
python -m src.optimizer.full_cycle \
  --base-config config/config.yaml \
  --train-days 60 \
  --oos-days 15 \
  --bo-evals 30 \
  --mc-runs 200 \
  --bo-startup 5
```

### List Versions

```bash
python -m src.optimizer.rollback_cli --list
```

### Rollback

```bash
# Rollback to latest
python -m src.optimizer.rollback_cli --to latest

# Rollback to specific version
python -m src.optimizer.rollback_cli --to 20251114_235900
```

### Using Shell Script

```bash
# No deployment
bash bin/run-optimizer-full-cycle.sh

# With deployment
DEPLOY=true bash bin/run-optimizer-full-cycle.sh
```

## systemd Integration

### Recommended: Separate Service

Create `systemd/xsmom-optimizer-full-cycle.service`:

```ini
[Unit]
Description=xsmom-bot Full-Cycle Optimizer (WFO + BO + MC)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=xsmom
WorkingDirectory=/opt/xsmom-bot
Environment="PYTHON_BIN=/opt/xsmom-bot/venv/bin/python"
Environment="PYTHONPATH=/opt/xsmom-bot"
EnvironmentFile=/opt/xsmom-bot/systemd/xsmom-optimizer.env.example
Environment="DEPLOY=true"

ExecStart=/opt/xsmom-bot/bin/run-optimizer-full-cycle.sh

StandardOutput=journal
StandardError=journal
SyslogIdentifier=xsmom-optimizer-full-cycle

TimeoutStartSec=7200

[Install]
WantedBy=multi-user.target
```

And `systemd/xsmom-optimizer-full-cycle.timer`:

```ini
[Unit]
Description=Run full-cycle optimizer weekly
Requires=xsmom-optimizer-full-cycle.service

[Timer]
OnBootSec=1h
OnCalendar=weekly
OnCalendar=*-*-* 02:00:00
AccuracySec=5m

[Install]
WantedBy=timers.target
```

Enable and start:

```bash
sudo systemctl enable xsmom-optimizer-full-cycle.timer
sudo systemctl start xsmom-optimizer-full-cycle.timer
```

## Outputs

### Console Output

Shows progress through WFO segments, BO optimization, MC stress tests, and deployment decision.

### JSON Output

Saved to `logs/optimizer_full_cycle_YYYYMMDD_HHMMSS.json`:

```json
{
  "timestamp": "2025-11-14T02:30:00",
  "baseline_metrics": {...},
  "candidates_evaluated": 3,
  "best_candidate": {
    "params": {...},
    "metrics": {...},
    "config_path": "config/optimized/config_20251114_023000.yaml"
  },
  "deployed": true
}
```

### Versioned Configs

- `config/optimized/config_YYYYMMDD_HHMMSS.yaml` - Optimized config
- `config/optimized/metadata_YYYYMMDD_HHMMSS.json` - Performance metadata
- `config/optimized/backup_YYYYMMDD_HHMMSS.yaml` - Live config backup
- `config/optimized/current_live_config.json` - Pointer to current live config

## Safety & Best Practices

1. **Start without deployment**: Run first without `--deploy`, review results
2. **Monitor after deployment**: Watch live performance for 24-48 hours
3. **Regular rollbacks**: Keep last 20 config versions, review history
4. **Gradual optimization**: Start with tight parameter ranges, widen as confidence grows
5. **Avoid optimizing safety limits**: Don't heavily optimize max_daily_loss_pct, etc.

## How This Helps MAKE MONEY

1. **Reduced Overfitting**: WFO ensures parameters work on unseen data
2. **Tail Risk Management**: MC stress tests catch catastrophic scenarios
3. **Efficient Search**: BO finds better parameters faster than grid search
4. **Automatic Improvement**: No manual intervention needed, continuously improves
5. **Safe Deployment**: Versioning and rollback prevent costly mistakes

## Next Steps

1. **Install dependencies**: `pip install -r requirements.txt`
2. **Test run**: Run optimizer without `--deploy` first
3. **Review results**: Check JSON output and config versions
4. **Enable auto-deployment**: Set `DEPLOY=true` in systemd service after testing
5. **Monitor live performance**: Track divergence between backtest and live Sharpe

---

**Motto: MAKE MONEY** — with robust, risk-managed, walk-forward & Monte Carlo validated parameters.

