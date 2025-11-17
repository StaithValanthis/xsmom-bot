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
| `exchange.testnet` | bool | `false` | Use testnet (recommended for testing) |

---

## Strategy

### Core Signals

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `strategy.signal_power` ⚙️ | float | `1.35` | Nonlinear z-score amplification exponent |
| `strategy.lookbacks` ⚙️ | list[int] | `[12, 24, 48, 96]` | Momentum lookback periods (hours/bars) |
| `strategy.lookback_weights` | list[float] | `[0.4, 0.3, 0.2, 0.1]` | Weights for each lookback period |
| `strategy.vol_lookback` | int | `96` | Volatility lookback period (for inverse-vol sizing) |
| `strategy.k_min` ⚙️ | int | `2` | Minimum top-K selection (long/short pairs) |
| `strategy.k_max` ⚙️ | int | `6` | Maximum top-K selection |
| `strategy.entry_zscore_min` ⚙️ | float | `0.0` | Minimum entry z-score threshold |

### Position Sizing

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `strategy.gross_leverage` ⚙️ | float | `0.95` | Portfolio gross leverage cap |
| `strategy.max_weight_per_asset` ⚙️ | float | `0.09` | Per-asset weight cap (fraction of portfolio) |
| `strategy.market_neutral` | bool | `true` | Market neutrality (long/short balance) |

### Filters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `strategy.regime_filter.enabled` ⚙️ | bool | `false` | Enable regime filter (EMA slope) |
| `strategy.regime_filter.ema_len` ⚙️ | int | `200` | EMA period for regime filter |
| `strategy.regime_filter.slope_min_bps_per_day` ⚙️ | float | `3.5` | Minimum EMA slope (bps/day) |
| `strategy.adx_filter.enabled` ⚙️ | bool | `false` | Enable ADX filter ⚠️ |
| `strategy.adx_filter.min_adx` | float | `28.0` | Minimum ADX threshold ⚠️ |
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
| `risk.trail_atr_mult` ⚙️ | float | `0.0` | Trailing stop ATR multiplier |
| `risk.atr_len` | int | `28` | ATR period |
| `risk.trailing_enabled` | bool | `false` | Enable trailing stops |
| `risk.breakeven_after_r` | float | `0.0` | Breakeven threshold (in units of R) |
| `risk.partial_tp_enabled` | bool | `false` | Enable partial profit-taking |
| `risk.partial_tp_r` | float | `0.0` | Partial TP threshold (in units of R) |
| `risk.partial_tp_size` | float | `0.0` | Portion to exit at partial TP |
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

## Parameter Importance & Optimization

### Core Optimizable Parameters (~18)

These parameters are recommended for optimization:

1. **Signals (6 params)**: `signal_power`, `lookbacks[0-2]`, `k_min`, `k_max`
2. **Filters (3 params)**: `regime_filter.ema_len`, `regime_filter.slope_min_bps_per_day`, `entry_zscore_min`
3. **Risk (5 params)**: `atr_mult_sl`, `trail_atr_mult`, `gross_leverage`, `max_weight_per_asset`, `portfolio_vol_target.target_ann_vol`
4. **Enable/Disable (4 params)**: `regime_filter.enabled`, `adx_filter.enabled`, `vol_target.enabled`, `diversify.enabled`

### Safety Limits (Do NOT Optimize Heavily)

- `risk.max_daily_loss_pct` 🔒 - Absolute safety limit
- `risk.max_portfolio_drawdown_pct` 🔒 - Catastrophic stop (if implemented)

### High Overfitting Risk Parameters ⚠️

These parameters should be used with caution:

- `strategy.symbol_filter.score.*` - Symbol scoring (many parameters, high overfit risk)
- `strategy.time_of_day_whitelist.*` - Time-of-day filter (high overfit risk)
- `strategy.adx_filter.*` - ADX filter (lagging indicator, overfit risk)

**Recommendation:** Simplify or disable high overfitting risk parameters.

---

## Next Steps

- **Config System**: [`../architecture/config_system.md`](../architecture/config_system.md) - How config maps to code
- **Strategy Logic**: [`../architecture/strategy_logic.md`](../architecture/strategy_logic.md) - How parameters control strategy
- **Knowledge Base**: [`../kb/framework_overview.md`](../kb/framework_overview.md) - Framework map

---

**Motto: MAKE MONEY** — with clear, well-documented, and type-safe configuration. 📈

