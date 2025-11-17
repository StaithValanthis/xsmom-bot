# Optimizer Data Requirements

## Issue: Insufficient Historical Data for WFO

The **Walk-Forward Optimization (WFO)** requires a minimum amount of historical data to generate training and out-of-sample segments.

### Requirements

**Minimum data for WFO:**
- **Training window:** 60 days minimum (default: 120 days)
- **Out-of-sample window:** 7 days minimum (default: 30 days)
- **Embargo period:** 2 days (default)
- **Total required:** ~69 days minimum (~1656 bars at 1h timeframe)

**For default settings (120 train + 30 OOS + 2 embargo):**
- **Total required:** ~152 days (~3648 bars at 1h timeframe)

### Why Only 1000 Bars Are Fetched

The number of bars fetched is controlled by `exchange.candles_limit` in your config:

```yaml
exchange:
  candles_limit: 1500  # Default in config.py
```

**Bybit API Limit:**
- **Per request:** Maximum 1000 bars per single API call
- **Automatic pagination:** The `ExchangeWrapper.fetch_ohlcv()` method now automatically paginates when `candles_limit > 1000`
- **How it works:** Makes multiple requests of 1000 bars each and concatenates them

**Common causes of limited data:**
1. **Config override:** Your `config.yaml` may have `candles_limit: 1000` or lower
2. **Pagination not working:** If pagination fails, you'll only get 1000 bars (check logs for pagination errors)
3. **Network/timeout issues:** Partial data fetch due to timeouts
4. **Exchange historical data limits:** Some exchanges may not have enough historical data available

### Solutions

#### Solution 1: Increase `candles_limit` (Recommended)

**Edit your config:**
```yaml
exchange:
  candles_limit: 2000  # For 1h bars: ~83 days (minimum for WFO)
  # Or higher for default WFO settings:
  candles_limit: 4000  # ~167 days (comfortable for 120/30/2 WFO)
```

**After updating:**
```bash
# Restart optimizer
sudo systemctl restart xsmom-optimizer.service
```

#### Solution 2: Use Alternative Optimizers (No WFO Required)

If you don't have enough historical data, use simpler optimizers that don't require WFO:

**1. Grid Search Optimizer (`optimizer_runner.py`):**
```bash
python -m src.optimizer_runner --config config/config.yaml
```
- **Data requirement:** ~500-1000 bars (20-40 days)
- **Method:** Grid search on core parameters
- **Pros:** Fast, works with limited data
- **Cons:** Less robust than WFO (higher overfitting risk)

**2. Staged Grid Search (`optimizer_cli.py`):**
```bash
python -m src.optimizer_cli optimize \
  --config config/config.yaml \
  --grid config/optimizer.grid.yaml
```
- **Data requirement:** ~500-1000 bars
- **Method:** Staged parameter exploration
- **Pros:** More systematic than simple grid
- **Cons:** Still higher overfitting risk than WFO

**3. Legacy Simple Grid (`optimizer.py`):**
```bash
python -m src.optimizer --config config/config.yaml
```
- **Data requirement:** ~500 bars minimum
- **Method:** Hardcoded parameter grid
- **Pros:** Simplest, works with minimal data
- **Cons:** Least robust, highest overfitting risk

#### Solution 3: Reduce WFO Requirements

If you must use WFO but have limited data, reduce the window sizes:

**Edit the optimizer call:**
```python
# In bin/run-optimizer-full-cycle.sh or direct call:
python -m src.optimizer.full_cycle \
  --base-config config/config.yaml \
  --live-config config/config.yaml \
  --train-days 60 \      # Reduced from 120
  --oos-days 14 \        # Reduced from 30
  --embargo-days 1       # Reduced from 2
```

**Minimum with reduced settings:**
- 60 train + 7 OOS + 1 embargo = 68 days (~1632 bars)

### Checking Available Data

**Check how many bars you're actually getting:**
```python
from src.config import load_config
from src.optimizer.backtest_runner import fetch_historical_data

cfg = load_config("config/config.yaml")
bars, symbols = fetch_historical_data(cfg)
print(f"Symbols: {len(symbols)}")
print(f"Bars per symbol: {len(list(bars.values())[0])}")
print(f"Days available: {len(list(bars.values())[0]) * 1.0 / 24:.1f}")
```

### Exchange API Limits

**Bybit limits:**
- **Per request:** Up to 200 bars (CCXT may handle pagination automatically)
- **Historical depth:** Typically 1-2 years of hourly data available
- **Rate limits:** May throttle if fetching many symbols

**If you're hitting API limits:**
1. Reduce symbol universe (`exchange.max_symbols`)
2. Fetch data in batches
3. Use a data provider with deeper history (if available)

### Recommended Settings

**For WFO with default settings:**
```yaml
exchange:
  candles_limit: 4000  # ~167 days at 1h (comfortable margin)
```

**For simple grid search:**
```yaml
exchange:
  candles_limit: 1000  # ~42 days (sufficient for grid search)
```

**For testing/development:**
```yaml
exchange:
  candles_limit: 500   # ~21 days (minimal for basic backtest)
```

### Troubleshooting

**Error: "WFO requires at least X days but only Y days available"**

1. **Check your config:**
   ```bash
   grep candles_limit config/config.yaml
   ```

2. **Check actual data fetched:**
   - Look at optimizer logs for "Fetched data for X symbols, Y bars"
   - Verify bars match `candles_limit`

3. **If bars < candles_limit:**
   - Exchange API may be limiting data
   - Check network connectivity
   - Try fetching fewer symbols
   - Check exchange status/maintenance

4. **Use alternative optimizer:**
   - Switch to `optimizer_runner.py` for grid search
   - Or reduce WFO window sizes

---

**See also:**
- [`../usage/optimizer.md`](../usage/optimizer.md) - Full optimizer documentation
- [`../reference/config_reference.md`](../reference/config_reference.md) - Config parameter reference

