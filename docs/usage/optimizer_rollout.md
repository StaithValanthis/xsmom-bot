# Optimizer Rollout System

## Overview

The **optimizer rollout system** implements a **survival-of-the-fittest** evolutionary deployment pipeline on top of the optimizer.

**Philosophy:**
- Configs compete for survival
- Only the most robust, consistently outperforming configs graduate from staging to live
- Automated, controlled, and aligned with the overall goal: **MAKE MONEY**

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                  OPTIMIZER ROLLOUT SYSTEM                    │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  ┌──────────────┐      ┌──────────────┐                    │
│  │  Optimizer   │ ───► │   Candidate  │                    │
│  │  (WFO+BO+MC) │      │    Queue     │                    │
│  └──────────────┘      └──────────────┘                    │
│         │                     │                             │
│         │                     ▼                             │
│         │              ┌──────────────┐                    │
│         │              │   Staging    │                    │
│         │              │  (1 active)  │                    │
│         │              └──────────────┘                    │
│         │                     │                             │
│         │                     ▼                             │
│         │              ┌──────────────┐                    │
│         └─────────────►│  Evaluator   │                    │
│                        │ (Promote/Discard)                  │
│                        └──────────────┘                    │
│                             │                               │
│                    ┌────────┴────────┐                     │
│                    ▼                 ▼                     │
│            ┌──────────────┐  ┌──────────────┐             │
│            │   Promoted   │  │  Discarded   │             │
│            │    (Live)    │  │   (Queue)    │             │
│            └──────────────┘  └──────────────┘             │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

---

## Live vs Staging Environment Separation

### Environment Separation

**Two parallel bot instances:**

1. **Live Bot** (`xsmom-bot.service`)
   - Reads from: `config/config.live.yaml`
   - State file: `state/state.live.json` (via `XSMOM_ENV=live`)
   - Environment: `XSMOM_ENV=live`

2. **Staging Bot** (`xsmom-staging.service`)
   - Reads from: `config/config.staging.yaml`
   - State file: `state/state.staging.json` (via `XSMOM_ENV=staging`)
   - Environment: `XSMOM_ENV=staging`

### Process Separation

**systemd Services (recommended):**

```ini
# Live bot: /etc/systemd/system/xsmom-bot.service
[Service]
Environment="XSMOM_ENV=live"
ExecStart=/opt/xsmom-bot/venv/bin/python -m src.main live --config /opt/xsmom-bot/config/config.live.yaml

# Staging bot: /etc/systemd/system/xsmom-staging.service
[Service]
Environment="XSMOM_ENV=staging"
ExecStart=/opt/xsmom-bot/venv/bin/python -m src.main live --config /opt/xsmom-bot/config/config.staging.yaml
```

### Risk Separation

**Important:** Staging bot should run with:
- **Reduced risk** (lower leverage, tighter stops)
- **Separate account** (testnet or sandbox) - **RECOMMENDED**
- **Or same account with lower allocation** (e.g., 20% of capital)

**Recommendation:** Use testnet or separate testnet account for staging.

---

## Candidate Queue & Tiering

### Candidate Ranking

**Candidates are ranked by:**
- **Improvement** = candidate_score - baseline_score (higher is better)
- **Score computation** = `sharpe * 0.5 + annualized * 10.0 * 0.3 + calmar * 0.2`

**Queue rules:**
- Only one candidate staged at a time
- Highest improvement → fastest promotion (tier A)
- Lower improvement → slower promotion (tier B/C)
- New candidates inserted by improvement (highest first)

### Candidate Tiers

**Tier A (High Improvement):**
- Threshold: `improvement >= 0.15`
- Staging requirements:
  - Duration: **3 days**
  - Trades: **100**
- Rationale: High confidence, fast promotion

**Tier B (Medium Improvement):**
- Threshold: `0.05 <= improvement < 0.15`
- Staging requirements:
  - Duration: **7 days**
  - Trades: **300**
- Rationale: Medium confidence, normal promotion

**Tier C (Low Improvement):**
- Threshold: `improvement < 0.05` (but positive)
- Staging requirements:
  - Duration: **14 days**
  - Trades: **500**
- Rationale: Low confidence, slow promotion

### Queue Management

**When new candidates arrive:**
1. Compute improvement (candidate_score - baseline_score)
2. Assign tier (A/B/C) based on improvement
3. Insert into queue sorted by improvement (descending)
4. When staging slot is free, highest-ranked candidate starts

**Queue behavior:**
- New candidates with higher improvement → jump ahead in queue
- New candidates with lower improvement → go to end of queue
- When staging frees up, **fittest wins** (highest improvement)

---

## Staging Evaluation

### Eligibility Requirements

**Basic eligibility:**
1. Staging duration >= `staging_required_min_duration_days`
2. Staging trade count >= `staging_required_min_trades`

**If not eligible:** Continue staging

### Performance Comparison

**Promotion score computation:**
```
promotion_score = α * (CAGR_stage - CAGR_live)
                + β * (Sharpe_stage - Sharpe_live)
                - γ * (DD_stage - DD_live)
```

**Default weights:**
- α = 0.4 (CAGR improvement)
- β = 0.3 (Sharpe improvement)
- γ = 0.3 (Drawdown penalty)

**Comparison window:**
- Start: `candidate.staging_started_at`
- End: Now (evaluation time)
- Both live and staging metrics computed over same window

### Promotion Rules

**Promote if:**
1. Eligibility requirements met AND
2. `promotion_score > threshold` (default: 0.0) AND
3. Staging drawdown not "significantly worse" than live (default: max 5% worse)

**Discard if:**
1. Eligibility requirements met AND
2. (`promotion_score <= threshold` OR staging drawdown significantly worse than live)

**Continue if:**
- Eligibility requirements not met yet
- Performance inconclusive (but not terrible)

---

## Promotion & Discard Logic

### Promotion

**When candidate is promoted:**
1. Copy staging config to `config/config.live.yaml`
2. Update rollout state:
   - `live_config_id = candidate_id`
   - Candidate status → `promoted`
   - `staging_config_id = null`
3. Log promotion message
4. Instructions to restart live bot (manual or automated)

**After promotion:**
- Staging slot is now free
- Next candidate in queue automatically starts staging (if any)

### Discard

**When candidate is discarded:**
1. Update rollout state:
   - Candidate status → `discarded`
   - `staging_config_id = null`
   - Record discard reason
2. Log discard message

**After discard:**
- Staging slot is now free
- Next candidate in queue automatically starts staging (if any)

---

## Rollout Supervisor

### Supervisor Responsibilities

The **rollout supervisor** is the main orchestrator that manages the lifecycle:

**Per run (every hour or daily):**
1. Load rollout state
2. If staging active:
   - Evaluate staging candidate
   - Promote or discard based on evaluation
3. If staging free:
   - Start next candidate from queue (if any)

### Running the Supervisor

**Manual run:**
```bash
python -m src.rollout.supervisor \
  --state data/config_rollout_state.json \
  --live-state state/state.live.json \
  --staging-state state/state.staging.json
```

**Scheduled run (systemd timer or cron):**
```bash
# Run every hour
0 * * * * /opt/xsmom-bot/venv/bin/python -m src.rollout.supervisor \
  --state /opt/xsmom-bot/data/config_rollout_state.json \
  >> /opt/xsmom-bot/logs/rollout_supervisor.log 2>&1
```

**systemd timer example:**
```ini
# /etc/systemd/system/xsmom-rollout-supervisor.service
[Unit]
Description=xsmom-bot Rollout Supervisor
After=network-online.target

[Service]
Type=oneshot
User=xsmom
WorkingDirectory=/opt/xsmom-bot
Environment="PYTHONPATH=/opt/xsmom-bot"
ExecStart=/opt/xsmom-bot/venv/bin/python -m src.rollout.supervisor \
  --state /opt/xsmom-bot/data/config_rollout_state.json \
  --live-state /opt/xsmom-bot/state/state.live.json \
  --staging-state /opt/xsmom-bot/state/state.staging.json

# /etc/systemd/system/xsmom-rollout-supervisor.timer
[Unit]
Description=Run rollout supervisor every hour
Requires=xsmom-rollout-supervisor.service

[Timer]
OnCalendar=*-*-* *:00:00
AccuracySec=1m
Persistent=true

[Install]
WantedBy=timers.target
```

**Enable timer:**
```bash
sudo systemctl daemon-reload
sudo systemctl enable xsmom-rollout-supervisor.timer
sudo systemctl start xsmom-rollout-supervisor.timer
```

### Supervisor Safety

**Idempotent:**
- Safe to run multiple times
- Won't duplicate actions
- Can be run manually or scheduled

**Non-blocking:**
- Failures don't crash the supervisor
- Logs errors but continues
- Won't interrupt live/staging bots

---

## Integration with Optimizer

### Automatic Integration

**When optimizer completes:**
- If `best_candidate` found and `deploy=False`:
  - Automatically adds candidate to rollout queue
  - Does NOT start staging (supervisor handles that)

**When optimizer completes with `deploy=True`:**
- Directly deploys to live (bypasses rollout queue)
- Use with caution (only for manual deployments)

### Manual Integration

**Add candidate manually:**
```bash
python -c "
from src.rollout.integration import add_optimizer_candidate
import json

# Load optimizer result
with open('logs/optimizer_full_cycle_20251117_020000.json') as f:
    result = json.load(f)

# Add to rollout queue
candidate_id = add_optimizer_candidate(result)
print(f'Added candidate {candidate_id} to queue')
"
```

---

## Workflow Example

### Complete Lifecycle

**1. Optimizer runs (weekly):**
```
Optimizer → Finds best candidate → Saves config + metadata
          → Adds to rollout queue (if deploy=False)
```

**2. Supervisor runs (hourly):**
```
Supervisor → Checks staging slot
           → If free: Starts next candidate from queue
           → If busy: Evaluates current staging candidate
                      → Promotes if outperforms
                      → Discards if underperforms
```

**3. Staging runs (parallel to live):**
```
Staging bot → Runs with staging config
            → Tracks performance in state.staging.json
            → Supervisor evaluates after min duration/trades
```

**4. Promotion:**
```
Supervisor → Evaluates staging vs live
           → If staging outperforms: Promotes to live
           → Updates config.live.yaml
           → Restarts live bot (manual or automated)
           → Staging slot freed → Next candidate starts
```

---

## Configuration

### Rollout State File

**Location:** `data/config_rollout_state.json`

**Structure:**
```json
{
  "live_config_id": "20251117_020000",
  "staging_config_id": "20251117_030000",
  "candidates": {
    "20251117_020000": {
      "id": "20251117_020000",
      "config_path": "config/optimized/config_20251117_020000.yaml",
      "metadata_path": "config/optimized/metadata_20251117_020000.json",
      "score": 0.8923,
      "baseline_score": 0.7834,
      "improvement": 0.1089,
      "tier": "B",
      "status": "promoted",
      "created_at": "2025-11-17T02:00:00Z",
      "staging_started_at": "2025-11-17T03:00:00Z",
      "staging_required_min_duration_days": 7.0,
      "staging_required_min_trades": 300,
      "promoted_at": "2025-11-24T12:00:00Z",
      ...
    },
    ...
  },
  "queue": ["20251117_040000", "20251117_030000"],
  "updated_at": "2025-11-17T12:00:00Z"
}
```

### Config Paths

**Live config:** `config/config.live.yaml`
**Staging config:** `config/config.staging.yaml`
**Optimized configs:** `config/optimized/config_YYYYMMDD_HHMMSS.yaml`

### State Paths

**Live state:** `state/state.live.json` (via `XSMOM_ENV=live`)
**Staging state:** `state/state.staging.json` (via `XSMOM_ENV=staging`)

**How it works:**
- Bot reads `cfg.paths.state_path` (e.g., `state/state.json`)
- If `XSMOM_ENV=staging`, uses `state/state.staging.json`
- If `XSMOM_ENV=live` (default), uses `state/state.json` or `state/state.live.json`

---

## Metrics Comparison

### Metrics Tracked

**Live and staging both track (in state files):**
- Daily PnL (absolute and %)
- Cumulative PnL (absolute and %)
- Max drawdown
- Sharpe ratio (approximate)
- Sortino ratio (approximate)
- Calmar ratio
- Trade count, win rate
- Equity growth

**Comparison window:**
- Starts: `candidate.staging_started_at`
- Ends: Now (evaluation time)
- Both live and staging metrics computed over same window

---

## Safety & Guardrails

### Promotion Guards

**Minimum requirements:**
- Staging duration >= tier requirement
- Staging trades >= tier requirement
- Promotion score > threshold (default: 0.0)
- Drawdown increase < tolerance (default: 5%)

**Rationale:**
- Prevents premature promotion (insufficient data)
- Prevents risk degradation (drawdown check)
- Requires consistent outperformance (score check)

### Discard Guards

**Automatic discard if:**
- Eligibility met AND promotion score <= threshold
- Eligibility met AND drawdown significantly worse than live

**Rationale:**
- Removes underperforming candidates quickly
- Prevents accumulation of bad configs in queue

---

## Best Practices

### Staging Setup

1. **Use separate account** (testnet or sandbox) for staging - **STRONGLY RECOMMENDED**
2. **Or use reduced risk** (lower leverage, tighter stops) if same account
3. **Monitor closely** (check logs, Discord notifications)
4. **Don't skip evaluation** (let supervisor run regularly)

### Supervisor Scheduling

1. **Run frequently** (hourly or every 2 hours)
2. **Check logs** (review promotion/discard decisions)
3. **Review metrics** (ensure comparisons are fair)
4. **Adjust thresholds** (if too aggressive or too conservative)

### Queue Management

1. **Monitor queue size** (should be small, 1-5 candidates)
2. **Review tier distribution** (should be mostly A/B, few C)
3. **Clear old candidates** (remove stale queued candidates)
4. **Don't manually promote** (let supervisor handle it)

---

## Troubleshooting

### "Staging slot always busy"

**Check:**
- Is staging bot actually running?
- Is staging candidate stuck in "staging" status?
- Check supervisor logs for evaluation failures

**Fix:**
- Manually evaluate and promote/discard: `python -m src.rollout.supervisor --verbose`

### "No candidates in queue"

**Check:**
- Is optimizer running? (`systemctl status xsmom-optimizer-full-cycle.timer`)
- Is optimizer adding candidates? (check optimizer logs)
- Check rollout state: `cat data/config_rollout_state.json | jq '.queue'`

**Fix:**
- Run optimizer manually: `python -m src.optimizer.full_cycle ...`
- Check optimizer result has `best_candidate`
- Verify `deploy=False` (or integration won't add to queue)

### "Promotion not happening"

**Check:**
- Is supervisor running? (`systemctl status xsmom-rollout-supervisor.timer`)
- Check evaluation logs (verbose mode)
- Verify staging metrics are being tracked

**Fix:**
- Run supervisor manually with verbose: `python -m src.rollout.supervisor --verbose`
- Check staging state file exists and has metrics
- Adjust promotion thresholds if too strict

### "Staging underperforming but not discarded"

**Check:**
- Eligibility requirements met? (duration, trades)
- Promotion score threshold too low?
- Drawdown tolerance too high?

**Fix:**
- Adjust thresholds: `--promotion-score-threshold 0.1 --max-dd-increase-tolerance 0.03`
- Manually discard if needed: Update rollout state directly (not recommended)

---

## Example Commands

### Run Supervisor Manually

```bash
# Run supervisor with default paths
python -m src.rollout.supervisor

# Run with custom paths
python -m src.rollout.supervisor \
  --state data/config_rollout_state.json \
  --live-state state/state.live.json \
  --staging-state state/state.staging.json \
  --live-config config/config.live.yaml \
  --staging-config config/config.staging.yaml

# Run with verbose logging
python -m src.rollout.supervisor --verbose

# Run with custom thresholds
python -m src.rollout.supervisor \
  --promotion-score-threshold 0.1 \
  --max-dd-increase-tolerance 0.03
```

### Add Candidate Manually

```bash
# After optimizer run
python -c "
from src.rollout.integration import add_optimizer_candidate
import json

with open('logs/optimizer_full_cycle_20251117_020000.json') as f:
    result = json.load(f)

candidate_id = add_optimizer_candidate(result)
print(f'Added: {candidate_id}')
"
```

### Check Rollout State

```bash
# View rollout state
cat data/config_rollout_state.json | jq '.'

# View queue
cat data/config_rollout_state.json | jq '.queue'

# View staging candidate
cat data/config_rollout_state.json | jq '.candidates[.staging_config_id]'
```

---

## Next Steps

- **Optimizer**: [`optimizer.md`](optimizer.md) - Full-cycle optimizer details
- **Deployment**: [`../operations/deployment_ubuntu_systemd.md`](../operations/deployment_ubuntu_systemd.md) - Production deployment
- **Monitoring**: [`../operations/monitoring_and_alerts.md`](../operations/monitoring_and_alerts.md) - Health checks

---

**Motto: MAKE MONEY** — via survival-of-the-fittest configuration evolution. 📈
