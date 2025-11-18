# Config Parameter Reference

> **Partially auto-generated** by `tools/update_kb.py`

This document lists all configuration parameters with their types, defaults, and descriptions.

**Legend:**
- ⚙️ = Optimizable (good for optimizer)
- 🔒 = Safety limit (optimize with caution or not at all)
- ⚠️ = High overfitting risk (keep simple)
- ❌ = Dead/unused parameter

---

## Exchange

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `exchange.id` | str | `bybit` | Exchange identifier |
| `exchange.account_type` | str | `swap` | Account type: `swap` for futures |
| `exchange.quote` | str | `USDT` | Quote currency |
| `exchange.max_symbols` | int | `36` | Maximum symbols in trading universe |
| `exchange.min_usd_volume_24h` | float | `100000000.0` | Minimum 24h volume filter (USD) |
| `exchange.timeframe` | str | `1h` | OHLCV bar timeframe |
| `exchange.candles_limit` | int | `4000` | Number of bars to fetch (will paginate if > 1000) |
| `exchange.testnet` | bool | `false` | Use testnet (recommended for testing) |

---

## Data (Historical Data Fetching)

Controls pagination and rate limiting for fetching historical OHLCV data from Bybit when `candles_limit > 1000`.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `data.max_candles_per_request` | int | `1000` | Bybit's per-request limit (do not change) |
| `data.max_candles_total` | int | `50000` | Safety cap per symbol/timeframe (prevents runaway fetches) |
| `data.api_throttle_sleep_ms` | int | `200` | Sleep between paginated requests (milliseconds) |
| `data.max_pagination_requests` | int | `100` | Safety limit on number of pagination requests per fetch |
| `data.cache.enabled` | bool | `false` | Enable OHLCV cache (SQLite, reduces API calls) |
| `data.cache.db_path` | str | `"data/ohlcv_cache.db"` | Cache database path |
| `data.cache.max_candles_total` | int | `50000` | Max candles stored per symbol/timeframe |
| `data.validation.enabled` | bool | `true` | Enable data quality validation |
| `data.validation.check_ohlc_consistency` | bool | `true` | Check OHLC relationships (low ≤ open/close ≤ high) |
| `data.validation.check_negative_volume` | bool | `true` | Check for negative volumes |
| `data.validation.check_gaps` | bool | `true` | Check for missing bars (gaps vs timeframe) |
| `data.validation.check_spikes` | bool | `true` | Check for extreme moves (z-score threshold) |
| `data.validation.spike_zscore_threshold` | float | `5.0` | Z-score threshold for spike detection |

**⚠️ Defaults Applied Automatically:**

These defaults are **automatically applied** even if the `data:` section is missing from your `config.yaml`. The system uses:
1. **Pydantic model defaults** in `DataCfg` class
2. **Config loader defaults** in `_merge_defaults()` function

**You don't need to add these to your config** unless you want to override the defaults. However, it's recommended to include them in `config.yaml` for clarity and explicit control.

**Notes:**
- **Bybit API Limit**: Single requests are capped at 1000 bars. The system automatically paginates when `candles_limit > 1000`.
- **Pagination**: Uses backward pagination (most recent N bars) by default, or forward pagination (date ranges) when using `fetch_ohlcv_range`.
- **Rate Limiting**: Built-in delays prevent hitting Bybit's rate limits. Increase `api_throttle_sleep_ms` if you see rate limit errors.
- **Safety Limits**: `max_candles_total` and `max_pagination_requests` prevent infinite loops or excessive API usage.

**Recommended Settings:**
- For WFO with 120/30/2 days at 1h: Set `max_candles_total` to at least 4000
- If hitting rate limits: Increase `api_throttle_sleep_ms` to 500ms
- For very long backtests: Increase `max_pagination_requests` (allows up to `max_pagination_requests * 1000` bars)

---

## Strategy

### Core Signals

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `strategy.signal_power` ⚙️ | float | `1.35` | Nonlinear z-score amplification exponent |
| `strategy.lookbacks` ⚙️ | list[int] | `[12, 24, 48, 96]` | Momentum lookback periods (hours/bars) |
| `strategy.lookback_weights` | list[float] | `[0.4, 0.3, 0.2, 0.1]` | Weights for each lookback period |
| `strategy.vol_lookback` ⚙️ | int | `96` | Volatility lookback period (for inverse-vol sizing, optimized in optimizer: [48, 144]) |
| `strategy.k_min` ⚙️ | int | `2` | Minimum top-K selection (long/short pairs) |
| `strategy.k_max` ⚙️ | int | `6` | Maximum top-K selection |
| `strategy.entry_zscore_min` | float | `0.0` | Minimum entry z-score threshold (locked, not optimized) |
| `strategy.volatility_entry.enabled` | bool | `false` | Enable volatility breakout entry gate |
| `strategy.volatility_entry.atr_lookback` | int | `48` | ATR lookback for vol breakout detection |
| `strategy.volatility_entry.expansion_mult` | float | `1.5` | ATR expansion multiplier (require ATR > expansion_mult × ATR_mean) |

### Position Sizing

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `strategy.gross_leverage` ⚙️ | float | `0.95` | Portfolio gross leverage cap |
| `strategy.max_weight_per_asset` | float | `0.10` | Per-asset weight cap (fraction of portfolio, locked, not optimized) |
| `strategy.market_neutral` | bool | `true` | Market neutrality (long/short balance) |
| `strategy.carry.budget_frac` ⚙️ | float | `0.25` | Carry sleeve budget fraction (0.0-1.0, optimized in optimizer) |

### Filters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `strategy.regime_filter.enabled` ⚙️ | bool | `false` | Enable regime filter (EMA slope) |
| `strategy.regime_filter.ema_len` | int | `200` | EMA period for regime filter (locked, not optimized) |
| `strategy.regime_filter.slope_min_bps_per_day` ⚙️ | float | `3.5` | Minimum EMA slope (bps/day, optimized) |
| `strategy.adx_filter.enabled` | bool | `false` | Enable ADX filter (disabled by default, removed from optimizer) ⚠️ |
| `strategy.adx_filter.min_adx` | float | `28.0` | Minimum ADX threshold ⚠️ |
| `strategy.meta_label.enabled` | bool | `false` | Enable meta-labeler (disabled by default, removed from optimizer) ⚠️ |
| `strategy.majors_regime.enabled` | bool | `false` | Enable majors regime filter (disabled by default, removed from optimizer) ⚠️ |
| `strategy.symbol_filter.enabled` | bool | `false` | Enable symbol scoring ⚠️ |
| `strategy.time_of_day_whitelist.enabled` | bool | `false` | Enable time-of-day filter ⚠️ |

### Volatility Targeting

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `strategy.portfolio_vol_target.enabled` ⚙️ | bool | `false` | Enable portfolio volatility targeting |
| `strategy.portfolio_vol_target.target_ann_vol` ⚙️ | float | `0.24` | Target annualized volatility |
| `strategy.portfolio_vol_target.lookback_hours` | int | `72` | Volatility lookback period (hours) |
| `strategy.portfolio_vol_target.min_scale` | float | `0.6` | Minimum scale factor |
| `strategy.portfolio_vol_target.max_scale` | float | `1.4` | Maximum scale factor |

---

## Risk

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `risk.max_daily_loss_pct` 🔒 | float | `5.0` | Daily loss kill-switch threshold (%) |
| `risk.max_portfolio_drawdown_pct` 🔒 | float | `0.0` | Portfolio drawdown limit (0.0 = disabled) |
| `risk.portfolio_dd_window_days` | int | `30` | Lookback window for high watermark (days) |
| `risk.atr_mult_sl` ⚙️ | float | `2.0` | Stop-loss ATR multiplier |
| `risk.atr_len` | int | `28` | ATR period |
| `risk.trailing_enabled` | bool | `true` | Enable trailing stops (enabled by default) |
| `risk.trail_atr_mult` | float | `1.0` | Trailing stop ATR multiplier (locked, not optimized) |
| `risk.breakeven_after_r` | float | `0.5` | Breakeven threshold (in units of R, enabled by default) |
| `risk.max_hours_in_trade` | int | `48` | Maximum hours in trade (time-based exit, enabled by default) |
| `risk.profit_targets.enabled` | bool | `false` | Enable R-multiple profit targets |
| `risk.profit_targets.targets[]` | list | `[]` | List of `{r_multiple, exit_pct}` targets (e.g., `[{r_multiple: 2.0, exit_pct: 0.5}]`) |
| `risk.partial_tp_enabled` | bool | `false` | Enable partial profit-taking (legacy, use `profit_targets` instead) |
| `risk.partial_tp_r` | float | `0.0` | Partial TP threshold (legacy) |
| `risk.partial_tp_size` | float | `0.0` | Portion to exit at partial TP (legacy) |
| `risk.sizing_mode` | str | `"inverse_vol"` | Sizing mode: `"inverse_vol"` (default) or `"fixed_r"` |
| `risk.risk_per_trade_pct` | float | `0.005` | Risk per trade as fraction of equity (0.5% default, used if `sizing_mode == "fixed_r"`) |
| `risk.correlation.enabled` | bool | `false` | Enable correlation limits |
| `risk.correlation.lookback_hours` | int | `48` | Correlation lookback period (hours) |
| `risk.correlation.max_allowed_corr` | float | `0.8` | Maximum allowed correlation (0.0-1.0) |
| `risk.correlation.max_high_corr_positions` | int | `2` | Maximum positions with high correlations |
| `risk.max_open_positions_hard` | int | `8` | Hard cap on total open positions |
| `risk.volatility_regime.enabled` | bool | `false` | Enable volatility regime-based leverage scaling |
| `risk.volatility_regime.lookback_hours` | int | `72` | ATR lookback period for vol regime (hours) |
| `risk.volatility_regime.high_vol_mult` | float | `1.5` | High-vol threshold (ATR must exceed baseline × this) |
| `risk.volatility_regime.max_scale_down` | float | `0.5` | Minimum scale factor in high vol (0.0-1.0) |
| `risk.long_term_dd.enabled` | bool | `false` | Enable long-term drawdown tracking (90/180/365d) |
| `risk.long_term_dd.max_dd_90d` | float | `0.3` | 90-day drawdown threshold (0.0-1.0, default: 30%) |
| `risk.long_term_dd.max_dd_180d` | float | `0.4` | 180-day drawdown threshold (0.0-1.0, default: 40%) |
| `risk.long_term_dd.max_dd_365d` | float | `0.5` | 365-day drawdown threshold (0.0-1.0, default: 50%) |
| `risk.use_trailing_killswitch` | bool | `false` | Use trailing kill-switch (from day high) |
| `risk.margin_soft_limit_pct` 🔒 | float | `0.0` | Margin soft limit (pause new trades, 0.0 = disabled) |
| `risk.margin_hard_limit_pct` 🔒 | float | `0.0` | Margin hard limit (close positions, 0.0 = disabled) |
| `risk.margin_action` | str | `pause` | Action on hard limit: `pause` or `close` |
| `risk.api_circuit_breaker.enabled` 🔒 | bool | `true` | Enable API circuit breaker |
| `risk.api_circuit_breaker.max_errors` | int | `5` | Max errors in window to trip breaker |
| `risk.api_circuit_breaker.window_seconds` | int | `300` | Error tracking window (seconds) |
| `risk.api_circuit_breaker.cooldown_seconds` | int | `600` | Cooldown period after trip (seconds) |

---

## Execution

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `execution.rebalance_minute` | int | `1` | Minute of hour to rebalance (0-59) |
| `execution.poll_seconds` | int | `10` | Poll interval for main loop (seconds) |
| `execution.order_type` | str | `limit` | Order type: `limit` or `market` |
| `execution.post_only` | bool | `true` | Post-only orders (maker fee) |
| `execution.price_offset_bps` | float | `0.0` | Price offset (basis points) |
| `execution.min_notional_per_order_usdt` | float | `5.0` | Minimum order notional (USDT) |
| `execution.min_rebalance_delta_bps` | float | `1.0` | Minimum rebalance delta (basis points) |
| `execution.cancel_open_orders_on_start` | bool | `false` | Cancel all open orders on startup |
| `execution.spread_guard.enabled` | bool | `false` | Enable spread guard |
| `execution.spread_guard.max_spread_bps` | float | `15.0` | Maximum spread (basis points) |
| `execution.dynamic_offset.enabled` | bool | `false` | Enable dynamic offset based on spread |
| `execution.stale_orders.enabled` | bool | `false` | Enable stale order cleanup |
| `execution.stale_orders.max_age_sec` | int | `180` | Maximum order age (seconds) |

---

## Liquidity

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `liquidity.notional_cap_usdt` | float | `20000.0` | Absolute notional cap per asset (USDT) |
| `liquidity.adv_cap_pct` | float | `0.0` | ADV% cap (optional, via tickers) |

---

## Costs

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `costs.maker_fee_bps` | float | `1.0` | Maker fee (basis points, negative = rebate) |
| `costs.taker_fee_bps` | float | `5.0` | Taker fee (basis points) |
| `costs.slippage_bps` | float | `2.0` | Slippage (basis points) |
| `costs.borrow_bps` | float | `0.0` | Borrow cost (basis points) |
| `costs.maker_fill_ratio` | float | `0.5` | Maker fill ratio (for cost estimation) |

---

## Paths

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `paths.state_path` | str | `/opt/xsmom-bot/state.json` | State file path |
| `paths.logs_dir` | str | `/opt/xsmom-bot/logs` | Logs directory |
| `paths.metrics_path` | str | `null` | Metrics file path (optional) |

---

## Logging

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `logging.level` | str | `INFO` | Logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `logging.file_max_mb` | int | `20` | Maximum log file size (MB) |
| `logging.file_backups` | int | `5` | Number of log file backups |

---

## Monitoring (MAKE MONEY hardening)

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `notifications.monitoring.no_trade.enabled` | bool | `true` | Enable no-trade detection |
| `notifications.monitoring.no_trade.threshold_hours` | float | `4.0` | Alert if no trades for N hours |
| `notifications.monitoring.cost_tracking.enabled` | bool | `true` | Enable cost tracking |
| `notifications.monitoring.cost_tracking.compare_to_backtest` | bool | `true` | Compare live costs to backtest |
| `notifications.monitoring.cost_tracking.alert_threshold_pct` | float | `20.0` | Alert if costs exceed backtest by N% |

---

## Notifications

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `notifications.discord.enabled` | bool | `false` | Enable Discord notifications |
| `notifications.discord.send_optimizer_results` | bool | `true` | Send optimizer result notifications |
| `notifications.discord.send_daily_report` | bool | `true` | Send daily performance reports |
| `notifications.discord.webhook_url` | str | `null` | Fallback webhook URL (if env var not set) |

---

## Optimizer (Deployment & OOS Sample Size)

Controls when optimizer results are considered reliable enough for deployment decisions. Prevents deployment based on unreliable metrics from tiny out-of-sample windows.

### OOS Sample Size Requirements

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `optimizer.oos_min_bars_for_deploy` | int | `200` | Minimum OOS bars required before trusting Sharpe for deployment |
| `optimizer.oos_min_days_for_deploy` | float | `5.0` | Minimum OOS days (approximate, based on timeframe) |
| `optimizer.oos_min_trades_for_deploy` | int | `30` | Minimum trades in OOS period (if available) |
| `optimizer.require_min_oos_for_deploy` | bool | `true` | If true, no deployment when OOS is too small |
| `optimizer.prefer_larger_oos_windows` | bool | `true` | Prefer larger OOS windows when enough data is available |
| `optimizer.max_oos_days_when_available` | int | `60` | Use up to N days for OOS if data allows |
| `optimizer.ignore_baseline_if_oos_too_small` | bool | `true` | Ignore baseline metrics if OOS sample is too small |
| `optimizer.warn_on_small_oos` | bool | `true` | Log warnings when OOS sample is below minimum |

### Database & Persistence (Optimizer Service)

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `optimizer.db_path` | str | `"data/optimizer.db"` | Path to SQLite database for trial storage |
| `optimizer.study_name_prefix` | str | `"xsmom_wfo"` | Prefix for Optuna study names |

### Historical Lookup & Filtering

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `optimizer.skip_known_params` | bool | `true` | Skip already-tested parameter combinations |
| `optimizer.enable_bad_combo_filter` | bool | `true` | Enable bad combination filtering |
| `optimizer.bad_combo_min_score` | float | `-1.0` | Below this score = bad combo (auto-marked) |
| `optimizer.bad_combo_dd_threshold` | float | `0.3` | Max drawdown threshold (30% = bad combo) |

**⚠️ Defaults Applied Automatically:**

These defaults are **automatically applied** even if the `optimizer:` section is missing from your `config.yaml`. The system uses:
1. **Pydantic model defaults** in `OptimizerCfg` class
2. **Config loader defaults** in `_merge_defaults()` function

**You don't need to add these to your config** unless you want to override the defaults. However, it's recommended to include them in `config.yaml` for clarity and explicit control.

**Notes:**
- **Problem**: Tiny OOS windows (e.g., 19 bars = ~0.8 days) produce unreliable metrics (inflated Sharpe ratios, meaningless annualized returns).
- **Solution**: The optimizer tracks OOS sample size (bars, days, trades) and adjusts deployment logic:
  - If baseline OOS is too small: Evaluates candidates on **absolute metrics only** (no baseline comparison)
  - If candidate OOS is too small: Rejects candidate (requires minimum sample size)
  - If both are sufficient: Performs normal baseline vs candidate comparison
- **Recommendations**:
  - Minimum OOS: At least 200 bars (~8.3 days at 1h timeframe) for reliable Sharpe ratios
  - Ideal OOS: 30-60 days for robust statistical significance
  - Trade count: At least 30 trades in OOS period for meaningful performance metrics

See [`../usage/optimizer.md#oos-sample-size-requirements`](../usage/optimizer.md#oos-sample-size-requirements) for detailed examples and behavior.

**Optimizer Service:**

The optimizer service provides database-backed, continuous optimization with historical lookup and bad-combo filtering. See [`../usage/optimizer_service.md`](../usage/optimizer_service.md) for full documentation.

**Key Features:**
- **SQLite persistence**: All trials stored in `optimizer.db_path`
- **Optuna warm-start**: Studies persist across runs
- **Historical lookup**: Skips duplicate parameter combinations
- **Bad-combo filtering**: Automatically avoids poor parameter regions
- **Query interface**: Inspect historical results via `python -m src.optimizer.query`

---

## Parameter Importance & Optimization

### Core Optimizable Parameters (11 total) ✅

**Updated:** The optimizer now optimizes **11 core parameters** (reduced from 15 to minimize overfitting risk).

1. **Signals (3 params)**: 
   - `signal_power`: [1.0, 1.5] (narrowed)
   - `lookbacks[0]` (short lookback)
   - `lookbacks[2]` (long lookback)
   - ⚠️ **Removed:** `lookbacks[1]` (medium lookback)

2. **Selection (2 params)**: `k_min`, `k_max`

3. **Filters (1 param)**: `regime_filter.slope_min_bps_per_day`
   - ⚠️ **Removed:** `regime_filter.ema_len` (locked at 200)

4. **Risk (1 param)**: `atr_mult_sl`
   - ⚠️ **Removed:** `trail_atr_mult` (locked at 1.0)
   - ⚠️ **Removed:** `entry_zscore_min` (locked at 0.0)

5. **Portfolio (3 params)**:
   - `gross_leverage`: [0.75, 1.5] (narrowed)
   - `portfolio_vol_target.target_ann_vol`: [0.15, 0.40] (narrowed)
   - `vol_lookback`: [48, 144] (NEW)
   - ⚠️ **Removed:** `max_weight_per_asset` (locked at 0.10)

6. **Carry (1 param)**: `carry.budget_frac`: [0.0, 0.40] (NEW)

### Locked/Removed Parameters

These parameters are **no longer optimized** (locked at fixed values or disabled):

- `regime_filter.ema_len` = 200 (fixed)
- `entry_zscore_min` = 0.0 (fixed)
- `trail_atr_mult` = 1.0 (fixed)
- `max_weight_per_asset` = 0.10 (fixed)
- `lookbacks[1]` (medium lookback) — removed
- **ADX filter** — disabled by default (removed from optimizer)
- **Meta-labeler** — disabled by default (removed from optimizer)
- **Majors regime** — disabled by default (removed from optimizer)

### Safety Limits (Do NOT Optimize Heavily) 🔒

- `risk.max_daily_loss_pct` 🔒 - Absolute safety limit
- `risk.max_portfolio_drawdown_pct` 🔒 - Portfolio drawdown stop
- `risk.margin_soft_limit_pct` 🔒 - Margin soft limit
- `risk.margin_hard_limit_pct` 🔒 - Margin hard limit

### High Overfitting Risk Parameters ⚠️

These parameters should be used with caution (disabled by default):

- `strategy.symbol_filter.score.*` - Symbol scoring (many parameters, high overfit risk)
- `strategy.time_of_day_whitelist.*` - Time-of-day filter (high overfit risk)
- `strategy.adx_filter.*` - ADX filter (disabled by default, removed from optimizer)
- `strategy.meta_label.*` - Meta-labeler (disabled by default, removed from optimizer)
- `strategy.majors_regime.*` - Majors regime (disabled by default, removed from optimizer)

**Recommendation:** These are disabled by default. Keep them disabled unless you have strong evidence they improve performance.

---

## Environment Variables

Environment variables are loaded from `/opt/xsmom-bot/.env` (created by `install.sh` installer).

### Exchange API Keys

| Variable | Required | Description |
|----------|----------|-------------|
| `BYBIT_API_KEY` | Yes | Bybit API key (for live trading) |
| `BYBIT_API_SECRET` | Yes | Bybit API secret (for live trading) |
| `API_KEY` | No | Alternative name for API key (fallback) |
| `API_SECRET` | No | Alternative name for API secret (fallback) |

**How to set:**
- **During installation:** Installer prompts for keys
- **After installation:** Edit `/opt/xsmom-bot/.env`:
  ```bash
  sudo nano /opt/xsmom-bot/.env
  # Add:
  # BYBIT_API_KEY=your_key_here
  # BYBIT_API_SECRET=your_secret_here
  ```

**Security:**
- Stored in `.env` file (mode 600, owner-only access)
- Never commit to git (in `.gitignore`)
- Loaded by systemd services via `EnvironmentFile=/opt/xsmom-bot/.env`

### Discord Notifications

| Variable | Required | Description |
|----------|----------|-------------|
| `DISCORD_WEBHOOK_URL` | No | Discord webhook URL (primary, takes precedence over config) |

**How to set:**
- **During installation:** Installer prompts for webhook (optional)
- **After installation:** Edit `/opt/xsmom-bot/.env`:
  ```bash
  sudo nano /opt/xsmom-bot/.env
  # Add:
  # DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
  ```

**Fallback:** If env var not set, uses `notifications.discord.webhook_url` from config.

### Python Environment

| Variable | Default | Description |
|----------|---------|-------------|
| `PYTHONPATH` | `/opt/xsmom-bot` | Python path |
| `PYTHONUNBUFFERED` | `1` | Unbuffered output (set automatically by installer) |

### Environment File Location

**Default:** `/opt/xsmom-bot/.env`

**Created by:** `install.sh` installer

**Format:**
```bash
BYBIT_API_KEY=your_key_here
BYBIT_API_SECRET=your_secret_here
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
PYTHONUNBUFFERED=1
```

**Permissions:** `600` (read/write for owner only)

**Owner:** `ubuntu` (or configured `RUN_AS` user)

**See also:** [`../operations/installation.md`](../operations/installation.md) for installation details.

---

## Next Steps

- **Config System**: [`../architecture/config_system.md`](../architecture/config_system.md) - How config maps to code
- **Strategy Logic**: [`../architecture/strategy_logic.md`](../architecture/strategy_logic.md) - How parameters control strategy
- **Knowledge Base**: [`../kb/framework_overview.md`](../kb/framework_overview.md) - Framework map

---

**Motto: MAKE MONEY** — with clear, well-documented, and type-safe configuration. 📈

