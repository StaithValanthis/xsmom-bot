# Config System

## Overview

**xsmom-bot** is **entirely config-driven** via `config/config.yaml`. All strategy parameters, risk controls, execution settings, and optimizer settings are defined in the config file and validated via Pydantic schemas.

---

## Config File Structure

### Location

**Primary Config:**
- `config/config.yaml` - Live config (used by bot)

**Example Config:**
- `config/config.yaml.example` - Example config (template)

**Optimized Configs:**
- `config/optimized/config_YYYYMMDD_HHMMSS.yaml` - Versioned optimized configs

### Format

**YAML Format:**
```yaml
exchange:
  id: bybit
  account_type: swap
  quote: USDT
  max_symbols: 36
  timeframe: 1h

strategy:
  signal_power: 1.35
  lookbacks: [12, 24, 48, 96]
  k_min: 2
  k_max: 6
  gross_leverage: 0.95
  max_weight_per_asset: 0.09

risk:
  max_daily_loss_pct: 5.0
  atr_mult_sl: 2.0
  trail_atr_mult: 1.5
```

---

## Config Loading

### Pydantic Validation

**Process:**
1. Load YAML file via `yaml.safe_load()`
2. Merge defaults via `config._merge_defaults()` (fills missing values)
3. Validate via Pydantic schemas in `src/config.py`
4. Return `AppConfig` object (type-safe)

**Implementation:**
- `src/config.py::load_config()` - Main config loader
- `src/config.py::_merge_defaults()` - Default value merger
- `src/config.py::AppConfig` - Pydantic config schema

**Rationale:**
- Type safety (Pydantic validates types at load time)
- Default values (safe fallbacks for missing parameters)
- Fails fast (invalid configs caught on startup, not at runtime)

**Example:**
```python
from src.config import load_config

cfg = load_config("config/config.yaml")
print(cfg.strategy.signal_power)  # Type-safe access
```

---

## Config Schema Structure

### Exchange Configuration

**`ExchangeCfg`:**
- `id`: Exchange identifier (e.g., "bybit")
- `account_type`: Account type ("swap" for futures)
- `quote`: Quote currency (e.g., "USDT")
- `max_symbols`: Maximum symbols in trading universe
- `min_usd_volume_24h`: Minimum 24h volume filter (USD)
- `timeframe`: OHLCV bar timeframe (e.g., "1h")
- `testnet`: Use testnet (default: false)

### Strategy Configuration

**`StrategyCfg`:**
- `signal_power`: Nonlinear z-score amplification exponent (default: 1.35)
- `lookbacks`: Momentum lookback periods (default: [12, 24, 48, 96])
- `lookback_weights`: Weights for each lookback (default: [0.4, 0.3, 0.2, 0.1])
- `k_min` / `k_max`: Top-K selection bounds (default: 2, 4)
- `gross_leverage`: Portfolio gross leverage cap (default: 0.95)
- `max_weight_per_asset`: Per-asset weight cap (default: 0.09)
- `market_neutral`: Market neutrality (default: true)
- `entry_zscore_min`: Minimum entry z-score threshold (default: 0.0)

**Nested Configs:**
- `regime_filter`: Regime filter (EMA slope)
- `adx_filter`: ADX filter
- `symbol_filter`: Symbol scoring
- `time_of_day_whitelist`: Time-of-day filter
- `portfolio_vol_target`: Volatility targeting
- `kelly`: Kelly scaling

### Risk Configuration

**`RiskCfg`:**
- `max_daily_loss_pct`: Daily loss limit (default: 5.0%)
- `atr_mult_sl`: Stop-loss multiplier (default: 2.0)
- `trail_atr_mult`: Trailing stop multiplier (default: 0.0)
- `atr_len`: ATR period (default: 28)
- `trailing_enabled`: Enable trailing stops (default: false)
- `breakeven_after_r`: Breakeven threshold (default: 0.0)
- `partial_tp_enabled`: Enable partial TP (default: false)
- `partial_tp_r`: Partial TP threshold (default: 0.0)
- `partial_tp_size`: Portion to exit (default: 0.0)

### Execution Configuration

**`ExecutionCfg`:**
- `rebalance_minute`: Minute of hour to rebalance (default: 1)
- `poll_seconds`: Poll interval for main loop (default: 10)
- `order_type`: Order type ("limit" or "market", default: "limit")
- `post_only`: Post-only orders (default: true)
- `price_offset_bps`: Price offset (basis points, default: 0.0)
- `min_notional_per_order_usdt`: Minimum order notional (default: 5.0)
- `min_rebalance_delta_bps`: Minimum rebalance delta (default: 1.0)

**Nested Configs:**
- `spread_guard`: Spread guard (max spread check)
- `dynamic_offset`: Dynamic offset based on spread
- `microstructure`: Order book imbalance check
- `stale_orders`: Stale order cleanup

### Optimizer Configuration

**`OptimizerCfg`:**
- `walk_forward.n_splits`: Number of WFO splits (default: 6)
- `walk_forward.embargo_frac`: Embargo fraction (default: 0.02)
- `walk_forward.objective`: Objective metric ("sharpe", "calmar", "annualized")
- `walk_forward.max_params`: Maximum parameters to optimize (default: 256)

### Notifications Configuration

**`NotificationsCfg`:**
- `discord.enabled`: Enable Discord notifications (default: false)
- `discord.send_optimizer_results`: Send optimizer results (default: true)
- `discord.send_daily_report`: Send daily reports (default: true)
- `discord.webhook_url`: Fallback webhook URL (if env var not set)

---

## Parameter Overrides

### Runtime Overrides

**Process:**
1. Load base config via `load_config()`
2. Apply parameter overrides via `patch_config()`
3. Validate overridden config via Pydantic

**Implementation:**
- `src/optimizer/backtest_runner.py::patch_config()` - Applies parameter overrides
- `src/optimizer/backtest_runner.py::_deep_set()` - Deep nested dict updates

**Rationale:**
- Optimizer needs to override parameters for backtests
- Parameter overrides use dot notation (e.g., "strategy.signal_power")
- Supports array indices (e.g., "strategy.lookbacks[0]")

**Example:**
```python
from src.config import load_config
from src.optimizer.backtest_runner import patch_config

base_cfg = load_config("config/config.yaml")
overrides = {
    "strategy.signal_power": 1.5,
    "strategy.lookbacks[0]": 18,
}
patched_cfg = patch_config(base_cfg, overrides)
```

---

## Config Validation

### Type Checking

**Pydantic validates:**
- Types (e.g., `signal_power` must be float)
- Ranges (e.g., `gross_leverage` must be 0.0-2.0)
- Required fields (e.g., `exchange.id` is required)
- Nested structures (e.g., `strategy.regime_filter.ema_len`)

**Errors:**
- Invalid types → `ValidationError` on startup
- Missing required fields → `ValidationError` on startup
- Invalid ranges → `ValidationError` on startup

**Rationale:**
- Fails fast (catches errors on startup, not at runtime)
- Type safety (prevents type errors at runtime)
- Self-documenting (schema defines valid values)

---

## Default Values

### Default Merging

**Process:**
1. Load YAML file (raw dict)
2. Merge defaults via `_merge_defaults()` (fills missing values)
3. Validate via Pydantic (ensures structure)

**Implementation:**
- `src/config.py::_merge_defaults()` - Merges default values
- Defaults defined in `_merge_defaults()` and Pydantic model defaults

**Rationale:**
- Safe fallbacks (missing parameters use defaults)
- Easier config management (only specify non-defaults)
- Backward compatibility (new parameters don't break old configs)

**Example:**
```yaml
# Minimal config (only specify non-defaults)
exchange:
  id: bybit
  account_type: swap
  quote: USDT

# All other parameters use defaults
```

---

## Config Versioning

### Versioned Configs

**Process:**
1. Optimizer generates new optimized config
2. Save to `config/optimized/config_YYYYMMDD_HHMMSS.yaml`
3. Save metadata to `config/optimized/metadata_YYYYMMDD_HHMMSS.json`
4. Deploy to `config/config.yaml` (with backup)

**Implementation:**
- `src/optimizer/config_manager.py::save_versioned_config()` - Saves versioned config
- `src/optimizer/config_manager.py::deploy_config()` - Deploys config (with backup)

**Rationale:**
- Track optimization history (versioned configs)
- Enable rollback (restore previous configs)
- Audit trail (metadata tracks optimization decisions)

**Example:**
```bash
# List optimized configs
python -m src.optimizer.rollback_cli --list

# Rollback to previous config
python -m src.optimizer.rollback_cli --to latest
```

---

## Config Reference

### Auto-Generated Reference

**Location:**
- `docs/reference/config_reference.md` - Parameter-by-parameter reference (auto-generated)

**Generation:**
```bash
python -m tools.update_kb
```

**Contents:**
- All parameters with types, defaults, descriptions
- Optimization recommendations (which params to optimize)
- Safety warnings (which params not to optimize)

**Rationale:**
- Up-to-date (auto-generated from code/config)
- Complete (all parameters documented)
- Maintainable (regenerates when config changes)

---

## Config Best Practices

### Do's

1. **Use Example Config** - Start from `config/config.yaml.example`
2. **Validate Early** - Test config on startup (check for validation errors)
3. **Version Control** - Keep config in git (with secrets in `.env`)
4. **Test Changes** - Test config changes on testnet before production
5. **Document Changes** - Document parameter changes in `change_log_architecture.md`

### Don'ts

1. **Don't Hardcode Secrets** - Use `.env` file for API keys
2. **Don't Skip Validation** - Always use `load_config()` (don't parse YAML manually)
3. **Don't Overfit** - Keep parameter count low (use optimizer for tuning)
4. **Don't Ignore Defaults** - Understand default values before changing

---

## Config Troubleshooting

### Common Issues

**"ValidationError: field required"**
- **Cause**: Missing required field in config
- **Fix**: Add missing field or check default value

**"ValidationError: value is not a valid float"**
- **Cause**: Invalid type (e.g., string instead of float)
- **Fix**: Check parameter type in config

**"Config file not found"**
- **Cause**: Config path incorrect or file missing
- **Fix**: Verify config path and file exists

**"Config loaded but behavior unexpected"**
- **Cause**: Default values or parameter interaction
- **Fix**: Check default values, review parameter dependencies

---

## Next Steps

- **Config Reference**: [`../reference/config_reference.md`](../reference/config_reference.md) - Complete parameter list
- **Strategy Logic**: [`strategy_logic.md`](strategy_logic.md) - How parameters control strategy
- **Knowledge Base**: [`../kb/framework_overview.md`](../kb/framework_overview.md) - Framework map

---

**Motto: MAKE MONEY** — with clear, well-documented, and type-safe configuration. 📈

