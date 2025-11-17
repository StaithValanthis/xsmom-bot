# MAKE MONEY Implementation Verification Checklist

**Date:** 2025-01-XX  
**Purpose:** Verify that all implemented fixes work correctly

---

## Pre-Verification Setup

1. **Backup current config:**
   ```bash
   cp config/config.yaml config/config.yaml.backup
   ```

2. **Ensure testnet mode for safety:**
   ```yaml
   exchange:
     testnet: true
   ```

3. **Check Python environment:**
   ```bash
   python -m src.config load_config config/config.yaml
   # Should load without errors
   ```

---

## 1. Circuit Breaker Verification

### Test: Simulate API Failures

**Method 1: Mock ExchangeWrapper (recommended for testing)**
```python
# Create test script: test_circuit_breaker.py
from src.exchange import ExchangeWrapper
from src.config import load_config
import time

cfg = load_config("config/config.yaml")
ex = ExchangeWrapper(cfg.exchange, risk_cfg=cfg.risk)

# Simulate errors
for i in range(6):
    ex.circuit_breaker.record_error()
    time.sleep(1)

# Check if tripped
status = ex.circuit_breaker.get_status()
assert status["tripped"] == True, "Circuit breaker should be tripped"
print("✅ Circuit breaker tripped correctly")
```

**Method 2: Lower threshold in config (for live testing)**
```yaml
risk:
  api_circuit_breaker:
    enabled: true
    max_errors: 2  # Lower threshold for testing
    window_seconds: 60  # 1 minute window
    cooldown_seconds: 120  # 2 minute cooldown
```

**Expected Behavior:**
- After 2 API errors in 1 minute, circuit breaker trips
- Bot logs: "API CIRCUIT BREAKER TRIPPED"
- Trading paused for 2 minutes
- After cooldown, circuit breaker resets

**Verification:**
```bash
# Run bot and check logs
python -m src.main live --config config/config.yaml --dry
# Look for: "API CIRCUIT BREAKER TRIPPED" in logs
```

---

## 2. Margin Protection Verification

### Test: Lower Margin Limits

**Config:**
```yaml
risk:
  margin_soft_limit_pct: 50.0  # Very low for testing
  margin_hard_limit_pct: 60.0
  margin_action: "pause"
```

**Expected Behavior:**
- If margin usage > 50%, bot pauses new trades
- If margin usage > 60%, bot closes all positions
- Logs show: "Margin soft limit exceeded" or "MARGIN HARD LIMIT EXCEEDED"

**Verification:**
```bash
# Run bot and check logs
python -m src.main live --config config/config.yaml --dry
# Look for margin warnings in logs
```

**Note:** In testnet, margin usage may be low. For real testing, would need to:
- Use high leverage
- Open large positions
- Or mock `get_margin_ratio()` to return high usage

---

## 3. No-Trade Detection Verification

### Test: Lower Threshold

**Config:**
```yaml
notifications:
  monitoring:
    no_trade:
      enabled: true
      threshold_hours: 0.1  # 6 minutes for testing
```

**Expected Behavior:**
- If no trades for 6 minutes, bot logs warning
- Discord alert sent (if enabled)
- Logs show: "NO TRADE DETECTED"

**Verification:**
```bash
# Run bot with filters that block all trades
# (e.g., very high entry_zscore_min)
# Wait 6 minutes
# Check logs for no-trade alert
```

---

## 4. Position Reconciliation Verification

### Test: Simulate Position Fetch Failure

**Method:** Temporarily break `fetch_positions()` in `exchange.py`:
```python
def fetch_positions(self) -> Dict[str, dict]:
    # Simulate failure
    raise Exception("Simulated API failure")
```

**Expected Behavior:**
- Bot logs: "POSITION RECONCILIATION FAILED"
- Trading paused
- `state["reconciliation_failed"] = True`
- Next successful fetch → reconciliation succeeds, trading resumes

**Verification:**
```bash
# Run bot with broken fetch_positions
# Check logs for reconciliation failure
# Check state.json for reconciliation_failed flag
```

---

## 5. Funding Cost Tracking Verification

### Test: Check State File

**Expected Behavior:**
- `state["funding_costs"]` contains per-symbol cumulative costs
- `state["total_funding_cost"]` contains total cumulative cost
- Costs are negative (paid) or positive (received)

**Verification:**
```bash
# Run bot for a few cycles with positions
# Check state.json:
cat state.json | jq '.funding_costs'
cat state.json | jq '.total_funding_cost'
```

**Expected Output:**
```json
{
  "funding_costs": {
    "BTC/USDT:USDT": -12.50,
    "ETH/USDT:USDT": -8.30
  },
  "total_funding_cost": -20.80
}
```

---

## 6. Optimizer Parameter Space Verification

### Test: Check Parameter Count

**Verification:**
```python
from src.optimizer.bo_runner import define_parameter_space

space = define_parameter_space()
param_count = len(space.space)
print(f"Parameter count: {param_count}")
assert param_count == 15, f"Expected 15 params, got {param_count}"
print("✅ Optimizer uses 15 core parameters")
```

**Expected:** 15 parameters

---

## 7. Config Loading Verification

### Test: Load Config with New Keys

**Verification:**
```bash
python -c "
from src.config import load_config
cfg = load_config('config/config.yaml')
print('✅ Config loaded successfully')
print(f'Circuit breaker enabled: {cfg.risk.api_circuit_breaker.get(\"enabled\", False)}')
print(f'Margin soft limit: {cfg.risk.margin_soft_limit_pct}')
print(f'No-trade threshold: {cfg.notifications.monitoring.no_trade.get(\"threshold_hours\", 0)}')
"
```

**Expected:** No errors, config loads with new keys

---

## 8. Integration Test: Full Cycle

### Test: Run Bot in Dry Mode

**Command:**
```bash
python -m src.main live --config config/config.yaml --dry
```

**Expected Behavior:**
1. Bot starts without errors
2. Circuit breaker initialized
3. Margin protection checks run
4. Position reconciliation runs
5. No-trade detection active
6. Funding cost tracking active
7. Bot runs for at least 1 cycle without crashing

**Verification:**
```bash
# Check logs for:
# - "API CIRCUIT BREAKER" (initialization)
# - "Margin" (margin checks)
# - "Position reconciliation" (reconciliation)
# - "NO TRADE DETECTED" (if no trades)
# - "funding_costs" (in state updates)
```

---

## 9. Rollback Logic Verification

### Test: Check Rollback Function Exists

**Verification:**
```python
from src.rollout.evaluator import check_live_rollback
from src.rollout.state import RolloutState

# Function should exist
assert callable(check_live_rollback)
print("✅ Rollback function exists")
```

**Note:** Full rollback testing requires live metrics integration (future work).

---

## 10. Paper Trading Stage Verification

### Test: Check Paper Status Exists

**Verification:**
```python
from src.rollout.state import CandidateStatus

# Paper status should exist
assert CandidateStatus.PAPER in CandidateStatus
print("✅ Paper trading stage exists")
```

**Note:** Full paper trading testing requires testnet integration (future work).

---

## Summary

**Quick Verification (5 minutes):**
1. ✅ Config loads with new keys
2. ✅ Optimizer uses 15 parameters
3. ✅ Circuit breaker class exists
4. ✅ Margin protection functions exist
5. ✅ Rollback/paper frameworks exist

**Full Verification (30 minutes):**
1. ✅ Circuit breaker trips on errors
2. ✅ Margin protection closes positions
3. ✅ No-trade alert triggers
4. ✅ Position reconciliation pauses on errors
5. ✅ Funding costs tracked in state
6. ✅ Bot runs in dry mode without errors

---

**Last Updated:** 2025-01-XX

