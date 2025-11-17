# Automated Optimizer Documentation

## Overview

The xsmom-bot automated optimizer implements a **full-cycle optimization pipeline** that combines:

1. **Walk-Forward Optimization (WFO)** — Reduces overfitting by testing parameters on out-of-sample data
2. **Bayesian Optimization (BO)** — Efficiently explores parameter space using Optuna TPE sampler
3. **Monte Carlo Stress Testing (MC)** — Assesses tail risk via bootstrapping and cost perturbations
4. **Safe Deployment** — Versioned configs with rollback capability

### Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Full-Cycle Optimizer                      │
├─────────────────────────────────────────────────────────────┤
│                                                               │
│  1. Fetch Historical Data (Bybit OHLCV)                      │
│     ↓                                                         │
│  2. Generate WFO Segments (train/OOS windows)                │
│     ↓                                                         │
│  3. For each segment:                                         │
│     a. Run Bayesian Optimization on training data            │
│     b. Select top K parameter sets                          │
│     ↓                                                         │
│  4. Evaluate top candidates on OOS segments                  │
│     a. Run Monte Carlo stress tests                          │
│     b. Aggregate metrics across segments                     │
│     ↓                                                         │
│  5. Compare to baseline (current live config)                │
│     a. Check improvement thresholds                          │
│     b. Check safety constraints                              │
│     ↓                                                         │
│  6. If approved: Deploy new config                           │
│     Else: Keep existing config                               │
│                                                               │
└─────────────────────────────────────────────────────────────┘
```

## Quick Start

### One-Time Setup

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```
   (Adds `optuna==3.6.1` for Bayesian optimization)

2. **Ensure config directory exists:**
   ```bash
   mkdir -p config/optimized
   ```

### Running the Optimizer

#### Manual Run (No Deployment)

```bash
python -m src.optimizer.full_cycle \
  --base-config config/config.yaml \
  --live-config config/config.yaml \
  --train-days 120 \
  --oos-days 30 \
  --bo-evals 100 \
  --mc-runs 1000
```

#### Manual Run with Deployment

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

#### Using Shell Script

```bash
# No deployment
bash bin/run-optimizer-full-cycle.sh

# With deployment
DEPLOY=true bash bin/run-optimizer-full-cycle.sh
# OR
bash bin/run-optimizer-full-cycle.sh --deploy
```

#### Debug Run (Faster, Fewer Evaluations)

```bash
python -m src.optimizer.full_cycle \
  --base-config config/config.yaml \
  --train-days 60 \
  --oos-days 15 \
  --bo-evals 30 \
  --mc-runs 200 \
  --bo-startup 5
```

### Listing Config Versions

```bash
python -m src.optimizer.rollback_cli --list
```

### Rolling Back to Previous Config

```bash
# Rollback to latest
python -m src.optimizer.rollback_cli --to latest

# Rollback to specific version
python -m src.optimizer.rollback_cli --to 20251114_235900

# Rollback without backup
python -m src.optimizer.rollback_cli --to latest --no-backup
```

## Configuration

### Environment Variables

The optimizer can be configured via environment variables (used by `run-optimizer-full-cycle.sh`):

```bash
export TRAIN_DAYS=120           # Training window (days)
export OOS_DAYS=30              # OOS window (days)
export EMBARGO_DAYS=2           # Embargo between train/OOS (days)
export BO_EVALS=100             # BO trials per segment
export BO_STARTUP=10            # Random trials before BO
export MC_RUNS=1000             # Monte Carlo runs
export MIN_IMPROVE_SHARPE=0.05  # Min Sharpe improvement
export MIN_IMPROVE_ANN=0.03     # Min annualized return improvement
export MAX_DD_INCREASE=0.05     # Max drawdown increase allowed
export TAIL_DD_LIMIT=0.70       # Catastrophic DD threshold
export SEED=42                  # Random seed
export DEPLOY=false             # Auto-deploy (true/false)
```

### Parameter Space

The optimizer optimizes a **core set of 18 parameters** (see `PARAMETER_REVIEW.md`):

- **Signals (6 params):** `signal_power`, `lookbacks[0-2]`, `k_min`, `k_max`
- **Filters (3 params):** `regime_filter.ema_len`, `regime_filter.slope_min_bps_per_day`, `entry_zscore_min`
- **Risk (5 params):** `atr_mult_sl`, `trail_atr_mult`, `gross_leverage`, `max_weight_per_asset`, `portfolio_vol_target.target_ann_vol`
- **Enable/Disable (4 params):** `regime_filter.enabled`, `adx_filter.enabled`, `vol_target_enabled`, `diversify_enabled`

To customize the parameter space, edit `src/optimizer/bo_runner.py::define_parameter_space()`.

## Integration with systemd

### Option 1: Replace Existing Optimizer Service

Update `systemd/xsmom-optimizer.service`:

```ini
[Unit]
Description=xsmom-bot Full-Cycle Optimizer
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=xsmom
WorkingDirectory=/opt/xsmom-bot
Environment="PYTHON_BIN=/opt/xsmom-bot/venv/bin/python"
Environment="PYTHONPATH=/opt/xsmom-bot"
EnvironmentFile=/opt/xsmom-bot/systemd/xsmom-optimizer.env.example

# Optional: Enable auto-deployment
# Environment="DEPLOY=true"

ExecStart=/opt/xsmom-bot/bin/run-optimizer-full-cycle.sh

# Logging
StandardOutput=journal
StandardError=journal
SyslogIdentifier=xsmom-optimizer

# Safety
TimeoutStartSec=7200  # 2 hours (optimizer can take a while)

[Install]
WantedBy=multi-user.target
```

### Option 2: Separate Service (Recommended)

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

# Auto-deploy if config passes checks
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
Description=Run full-cycle optimizer daily
Requires=xsmom-optimizer-full-cycle.service

[Timer]
OnBootSec=1h
OnCalendar=daily
# Run at 02:00 UTC (after market hours)
OnCalendar=*-*-* 02:00:00
AccuracySec=5m

[Install]
WantedBy=timers.target
```

Enable and start:

```bash
sudo systemctl enable xsmom-optimizer-full-cycle.timer
sudo systemctl start xsmom-optimizer-full-cycle.timer
sudo systemctl status xsmom-optimizer-full-cycle.timer
```

## How It Works

### Walk-Forward Optimization (WFO)

1. **Data Windowing:**
   - Splits historical data into overlapping train/OOS windows
   - Embargo period prevents leakage between train and OOS
   - Example: 120-day train, 2-day embargo, 30-day OOS

2. **Per-Segment Optimization:**
   - Runs Bayesian Optimization on training window
   - Selects top K parameter sets
   - Evaluates on out-of-sample window

3. **Aggregation:**
   - Aggregates metrics across all OOS segments
   - Computes stability metrics (variance of Sharpe, consistency)

### Bayesian Optimization (BO)

- **Method:** Tree-structured Parzen Estimator (TPE) via Optuna
- **Efficiency:** Explores parameter space more efficiently than grid search
- **Configuration:** 
  - `n_startup_trials`: Random exploration before BO starts
  - `n_trials`: Total trials per segment

### Monte Carlo Stress Testing (MC)

1. **Bootstrap Method:**
   - Resamples trade returns with replacement
   - Block bootstrapping maintains autocorrelation
   - Creates synthetic equity curves

2. **Cost Perturbation Method:**
   - Injects randomness into slippage, fees, funding
   - Draws from normal distributions with realistic std devs

3. **Tail Risk Assessment:**
   - Computes percentiles of max drawdown (p95, p99)
   - Rejects candidates with catastrophic tail risk (>70% DD)

### Deployment Criteria

A new config is deployed only if:

1. **Improvement Thresholds:**
   - `oos_sharpe - baseline_sharpe >= min_improve_sharpe` (default: 0.05)
   - `oos_annualized - baseline_annualized >= min_improve_ann` (default: 0.03)

2. **Safety Constraints:**
   - `max_dd_increase <= max_dd_increase` (default: 0.05)
   - `p99_max_drawdown <= tail_dd_limit` (default: 0.70)

3. **Config Validation:**
   - Config passes Pydantic validation
   - All required parameters present

### Config Versioning

- **Storage:** `config/optimized/config_YYYYMMDD_HHMMSS.yaml`
- **Metadata:** `config/optimized/metadata_YYYYMMDD_HHMMSS.json`
- **Backups:** `config/optimized/backup_YYYYMMDD_HHMMSS.yaml`
- **Pointer:** `config/optimized/current_live_config.json`

## Outputs

### Console Output

```
=== FULL CYCLE OPTIMIZER ===
Fetching historical data...
Fetched data for 36 symbols, 1500 bars
Generated 8 WFO segments
Evaluating baseline (current live config)...
Baseline OOS: Sharpe=1.23, Annualized=45.2%
Running BO on segment 0 (2024-01-01 to 2024-04-30)...
...
Segment 0 BO complete: best_score=0.8923
...
Best candidate: 0, OOS Sharpe=1.45
Saved versioned config: config/optimized/config_20251114_023000.yaml
Deployed new config to live location
```

### JSON Output

Saved to `logs/optimizer_full_cycle_YYYYMMDD_HHMMSS.json`:

```json
{
  "timestamp": "2025-11-14T02:30:00",
  "baseline_metrics": {
    "oos_sharpe_mean": 1.23,
    "oos_annualized_mean": 0.452,
    "oos_max_drawdown_mean": -0.15
  },
  "candidates_evaluated": 3,
  "best_candidate": {
    "params": {
      "strategy.signal_power": 1.45,
      "strategy.k_min": 2,
      "strategy.k_max": 6,
      ...
    },
    "metrics": {
      "oos_sharpe_mean": 1.45,
      "oos_annualized_mean": 0.482,
      "mean_p99_dd": -0.18
    },
    "config_path": "config/optimized/config_20251114_023000.yaml"
  },
  "deployed": true
}
```

## Troubleshooting

### "Optuna not installed"

```bash
pip install optuna==3.6.1
```

### "No WFO segments generated"

- Check that you have enough historical data (need > train_days + oos_days + embargo_days)
- Verify `exchange.candles_limit` in config is large enough
- Check exchange connection and API keys

### "No valid candidate evaluations"

- Check that baseline config produces valid metrics
- Verify parameter space bounds are reasonable
- Check logs for backtest errors

### "Config validation failed"

- Check that parameter paths use correct dot notation
- Verify parameter types match config schema
- Check Pydantic validation errors in logs

### Optimizer Takes Too Long

- Reduce `--bo-evals` (e.g., 50 instead of 100)
- Reduce `--mc-runs` (e.g., 500 instead of 1000)
- Reduce `--train-days` and `--oos-days`
- Use smaller symbol universe

## Safety & Best Practices

1. **Start Without Deployment:**
   - Run optimizer without `--deploy` first
   - Review outputs and verify improvements
   - Manually deploy if satisfied

2. **Monitor After Deployment:**
   - Watch live performance for 24-48 hours
   - Use rollback if live performance degrades
   - Track divergence between backtest and live Sharpe

3. **Regular Rollbacks:**
   - Keep last 20 config versions
   - Periodically review version history
   - Document which configs performed well live

4. **Gradual Optimization:**
   - Start with tight parameter ranges
   - Widen ranges as you gain confidence
   - Avoid optimizing safety limits heavily

## Advanced Usage

### Custom Parameter Space

Edit `src/optimizer/bo_runner.py`:

```python
def define_parameter_space(...):
    return ParameterSpace(space={
        "strategy.signal_power": {
            "type": "float",
            "low": 1.2,
            "high": 1.8,
        },
        # ... add more parameters
    })
```

### Custom Objective Function

Edit `src/optimizer/full_cycle.py::compute_objective_score()`:

```python
def compute_objective_score(metrics, weights=None):
    if weights is None:
        weights = {
            "sharpe": 0.50,      # Higher weight on Sharpe
            "annualized": 0.20,
            "calmar": 0.20,
            "turnover": -0.10,   # Higher turnover penalty
        }
    # ... compute score
```

### Integration with Existing Optimizer

The full-cycle optimizer can run alongside the existing `optimizer_runner.py`:

- Full-cycle runs weekly/monthly (slow, thorough)
- `optimizer_runner` runs daily (fast, incremental)

Use separate systemd timers for each.

## FAQ

**Q: How often should I run the optimizer?**
A: Weekly or bi-weekly is sufficient. Daily is overkill and risks overfitting to recent data.

**Q: Should I enable auto-deployment?**
A: Only after thorough testing. Start without `--deploy`, review results, then enable.

**Q: What if the optimizer finds no improvements?**
A: This is normal and safe. The existing config is kept. Review parameter bounds or market conditions.

**Q: How do I know if parameters are overfit?**
A: Monitor `oos_sharpe_stability` and `oos_sharpe_consistency` in metadata. Low stability = overfitting risk.

**Q: Can I optimize different parameters?**
A: Yes, edit `define_parameter_space()` in `src/optimizer/bo_runner.py`.

---

**Motto: MAKE MONEY** — with robust, risk-managed, walk-forward & Monte Carlo validated parameters.

