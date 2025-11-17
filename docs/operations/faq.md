# Frequently Asked Questions (FAQ)

## General

### What is xsmom-bot?

**xsmom-bot** is a fully automated, multi-pair crypto futures trading bot that implements a **cross-sectional momentum (XSMOM)** strategy on Bybit USDT-perpetual futures.

**Strategy:** Ranks cryptocurrencies by momentum relative to each other, goes long top K assets (strongest momentum), shorts bottom K assets (weakest momentum), with inverse-volatility sizing and robust risk controls.

### What does "XSMOM" mean?

**XSMOM** = **Cross-Sectional Momentum**

- **Cross-Sectional**: Ranks assets **relative to each other** (not absolute momentum)
- **Momentum**: Assets with strong recent performance (relative to peers)
- **Market Neutral**: Long top K, short bottom K (removes market beta)

**Contrast with TSMOM (Time-Series Momentum):**
- **TSMOM**: Trade based on asset's own historical momentum
- **XSMOM**: Trade based on asset's momentum **relative to other assets**

---

## Strategy

### How does the strategy work?

**Flow:**
1. Compute momentum signals (multi-lookback returns)
2. Normalize to cross-sectional z-scores (relative to universe)
3. Amplify signals (nonlinear power, default: 1.35×)
4. Select top K longs, bottom K shorts
5. Size positions inversely to volatility (risk parity style)
6. Maintain market neutrality (long/short balance)

See [`../architecture/strategy_logic.md`](../architecture/strategy_logic.md) for details.

### Why inverse-volatility sizing?

**Rationale:**
- **Equal risk contribution** per asset (risk parity style)
- **Higher volatility → smaller position** (less risk per dollar)
- **Lower volatility → larger position** (more capital efficiency)
- **Stable returns** (less volatility drag from high-volatility assets)

### Why market neutral?

**Rationale:**
- **Removes market beta** (profit from relative performance, not market direction)
- **Reduces exposure to market crashes** (beta neutral)
- **Focuses on cross-sectional alpha** (relative momentum)

### What parameters are most important?

**High-Impact Parameters:**
1. `strategy.signal_power` (1.0-2.0) - Controls signal amplification
2. `strategy.k_min` / `strategy.k_max` (2-8) - Controls position count
3. `strategy.gross_leverage` (0.5-2.0) - Controls portfolio exposure
4. `risk.atr_mult_sl` (1.5-3.0) - Controls stop-loss distance
5. `strategy.portfolio_vol_target.target_ann_vol` (0.15-0.40) - Controls risk level

See [`../reference/config_reference.md`](../reference/config_reference.md) for complete list.

---

## Risk Management

### What is the daily loss limit?

**Default:** 5% of equity

**Mechanism:**
- Stop trading if daily loss > `risk.max_daily_loss_pct` (default: 5.0%)
- Optional: Trailing kill-switch (stop if loss from daily high > threshold)
- Resume trading after pause expires (configurable)

**Example:**
- Start equity: $10,000
- Max daily loss: 5% = $500
- Stop trading if equity < $9,500

### How do stop-losses work?

**ATR-Based Stops:**
- Initial stop: `entry_price ± (atr_mult_sl × ATR)` (default: 2.0× ATR)
- Trailing stop: Stop moves up as price moves favorably (optional)
- Breakeven: Move stop to entry after `breakeven_after_r × R` profit (optional)

**Example:**
- Entry: $50,000
- ATR: $500
- Stop-loss: 2.0× ATR = $1,000
- Stop level: $49,000

See [`../architecture/risk_management.md`](../architecture/risk_management.md) for details.

### What is the maximum drawdown?

**Current:** No portfolio-wide drawdown limit (only daily loss limit)

**Planned:** Portfolio-wide drawdown limit (e.g., -20% from peak)

**Rationale:**
- Daily loss limit protects against single-day losses
- Portfolio-wide drawdown limit would protect against extended losing periods
- Currently planned but not yet implemented

---

## Optimization

### How does the optimizer work?

**Full-Cycle Optimizer:**
1. **Walk-Forward Optimization (WFO)**: Splits data into train/test windows, optimizes on training data, validates on test data
2. **Bayesian Optimization (BO)**: Uses Optuna TPE sampler to efficiently explore parameter space
3. **Monte Carlo Stress Testing**: Bootstraps trades and perturbs costs to assess tail risk
4. **Safe Deployment**: Only deploys if candidate beats baseline and passes safety checks

See [`../usage/optimizer.md`](../usage/optimizer.md) for details.

### How often does the optimizer run?

**Default:** Weekly (every Sunday at 02:00 UTC)

**Configurable:** Via `systemd/xsmom-optimizer-full-cycle.timer`

**Rationale:**
- Weekly frequency balances freshness with stability
- Avoids over-optimization (reduces overfitting risk)
- Gives live trading time to validate parameters

### Which parameters should I optimize?

**Recommended (18 core parameters):**

1. **Signals (6)**: `signal_power`, `lookbacks[0-2]`, `k_min`, `k_max`
2. **Filters (3)**: `regime_filter.ema_len`, `regime_filter.slope_min_bps_per_day`, `entry_zscore_min`
3. **Risk (5)**: `atr_mult_sl`, `trail_atr_mult`, `gross_leverage`, `max_weight_per_asset`, `portfolio_vol_target.target_ann_vol`
4. **Enable/Disable (4)**: `regime_filter.enabled`, `adx_filter.enabled`, `vol_target.enabled`, `diversify.enabled`

**Do NOT optimize:**
- `risk.max_daily_loss_pct` - Absolute safety limit
- `risk.max_portfolio_drawdown_pct` - Catastrophic stop

See [`../reference/config_reference.md`](../reference/config_reference.md) for details.

---

## Deployment

### How do I deploy to production?

**Steps:**
1. Set up Ubuntu server (20.04+)
2. Clone repository to `/opt/xsmom-bot`
3. Install dependencies (`venv`, `pip install -r requirements.txt`)
4. Configure `.env` file (API keys)
5. Configure `config/config.yaml` (strategy parameters)
6. Set up systemd services (bot, optimizer, daily report)
7. Enable and start services

See [`deployment_ubuntu_systemd.md`](deployment_ubuntu_systemd.md) for detailed setup.

### Do I need to restart the bot after config changes?

**Yes** - Config changes require restart:

```bash
sudo systemctl restart xsmom-bot.service
```

**Exception:** Optimizer can auto-deploy configs (but still requires restart)

### How do I check if the bot is running?

**Check service status:**
```bash
sudo systemctl status xsmom-bot.service
```

**Check logs:**
```bash
journalctl -u xsmom-bot.service -f
```

**Check heartbeat:**
```bash
sudo -u xsmom cat /opt/xsmom-bot/state.json.heartbeat
```

---

## Configuration

### Where do I put my API keys?

**`.env` file** (not in `config.yaml`):

```bash
# /opt/xsmom-bot/.env
BYBIT_API_KEY=your_api_key_here
BYBIT_API_SECRET=your_api_secret_here
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...  # Optional
```

**Security:**
- Never commit `.env` to git (it's in `.gitignore`)
- Use `chmod 600` to protect file
- Use environment variables for systemd (if preferred)

### What is the difference between testnet and production?

**Testnet:**
- Uses Bybit testnet (fake money)
- Good for testing (no risk)
- Set `exchange.testnet: true` in config

**Production:**
- Uses real Bybit exchange (real money)
- Requires real API keys
- Set `exchange.testnet: false` in config

**Recommendation:** Always test on testnet first!

### How do I adjust risk settings?

**Conservative (Lower Risk):**
```yaml
risk:
  max_daily_loss_pct: 3.0          # Lower daily loss limit
  atr_mult_sl: 2.5                  # Wider stops
  gross_leverage: 0.75              # Lower leverage
```

**Moderate (Default):**
```yaml
risk:
  max_daily_loss_pct: 5.0          # Standard daily loss limit
  atr_mult_sl: 2.0                  # Standard stops
  gross_leverage: 0.95              # Standard leverage
```

**Aggressive (Higher Risk):**
```yaml
risk:
  max_daily_loss_pct: 7.0          # Higher daily loss limit
  atr_mult_sl: 1.5                  # Tighter stops
  gross_leverage: 1.5               # Higher leverage
```

**⚠️ Warning:** Aggressive settings increase risk. Use with caution.

---

## Performance

### What returns should I expect?

**Historical Performance** (backtests, not guaranteed):
- **Annualized Return**: 20-50% (depending on parameters)
- **Sharpe Ratio**: 1.2-2.0 (depending on parameters)
- **Max Drawdown**: 10-20% (depending on parameters)

**Note:** Past performance does not guarantee future results. Use testnet first!

### How do I monitor performance?

**Daily Reports:**
- Discord notifications (if configured)
- Daily report module (`src/reports/daily_report.py`)
- State file (`state.json`)

**Metrics Tracked:**
- Daily PnL (absolute and %)
- Trade count, win rate
- Cumulative PnL, max drawdown
- Current equity vs starting equity

See [`../usage/discord_notifications.md`](../usage/discord_notifications.md) for setup.

### What if performance degrades?

**Check:**
1. Review logs for errors
2. Check state file for issues
3. Compare to backtest performance
4. Check exchange API status
5. Consider rollback to previous config

**Rollback:**
```bash
python -m src.optimizer.rollback_cli --to latest
sudo systemctl restart xsmom-bot.service
```

---

## Troubleshooting

### Bot not placing orders

**Check:**
1. Universe size (may be too restrictive)
2. Regime filter (market may not be trending)
3. Entry threshold (may be too high)
4. Daily loss limit (may be triggered)
5. Cooldowns (symbols may be in cooldown)

See [`troubleshooting.md`](troubleshooting.md) for details.

### Bot crashes on startup

**Check:**
1. API keys (missing or invalid)
2. Config file (validation errors)
3. Permissions (file ownership)
4. State file (corruption)

See [`troubleshooting.md`](troubleshooting.md) for details.

---

## Next Steps

- **Start Here**: [`../start_here.md`](../start_here.md) - Your reading guide
- **Troubleshooting**: [`troubleshooting.md`](troubleshooting.md) - Common issues
- **Reference**: [`../reference/config_reference.md`](../reference/config_reference.md) - Config parameters

---

**Motto: MAKE MONEY** — but with clear answers to common questions. 📈

