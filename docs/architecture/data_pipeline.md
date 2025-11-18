# Data Pipeline

## Overview

**xsmom-bot** implements a robust data pipeline for fetching, caching, validating, and using historical OHLCV data. The pipeline is designed to reduce API calls, ensure data quality, and support long-term equity tracking.

**Motto:** **MAKE MONEY** — with clean, validated, cached data that reduces costs and improves reliability.

---

## Historical OHLCV Pipeline

### 1. Data Fetching (Bybit/CCXT)

The bot fetches OHLCV data via CCXT from Bybit:

- **API Limit:** 1000 bars per request (hard limit enforced by Bybit)
- **Automatic Pagination:** When `candles_limit > 1000`, the system automatically paginates
- **Rate Limiting:** Built-in delays prevent hitting rate limits

**Implementation:**
- `src/exchange.py::ExchangeWrapper.fetch_ohlcv()` — Limit-based fetching (most recent N bars)
- `src/exchange.py::ExchangeWrapper.fetch_ohlcv_range()` — Date-range fetching (start_ts to end_ts)

**Configuration:**
- `exchange.candles_limit`: Number of bars to fetch (will paginate if > 1000)
- `data.max_candles_per_request`: Bybit's limit (1000, do not change)
- `data.max_candles_total`: Safety cap per symbol/timeframe (default: 50,000)
- `data.api_throttle_sleep_ms`: Sleep between pagination requests (default: 200ms)
- `data.max_pagination_requests`: Safety limit on pagination requests (default: 100)

### 2. Historical OHLCV Cache (NEW) ✅

**Status:** ✅ **Implemented** (roadmap improvement)

The bot uses a **SQLite cache** to reduce API calls and speed up backtests/optimizer runs.

**Storage:**
- **Database:** `data/ohlcv_cache.db` (default, configurable)
- **Table:** `ohlcv(symbol, timeframe, ts, open, high, low, close, volume, updated_at)`
- **Primary Key:** `(symbol, timeframe, ts)` — Ensures no duplicates

**Behavior:**
1. **Cache Check:** Before fetching, check cache for requested time range
2. **Gap Detection:** Identify missing bars in cached data
3. **Selective Fetch:** Only fetch missing bars from exchange
4. **Cache Update:** Store newly fetched bars in cache

**Configuration:**
```yaml
data:
  cache:
    enabled: true              # Enable cache (default: false)
    db_path: "data/ohlcv_cache.db"  # Cache database path
    max_candles_total: 50000   # Max candles to store per symbol/timeframe
```

**Implementation:**
- `src/data/cache.py::OHLCVCache` — SQLite cache management
- `src/exchange.py::ExchangeWrapper` — Integrates cache into fetch pipeline

**Benefits:**
- **Reduced API Calls:** Subsequent runs use cached data
- **Faster Backtests:** No API delays for cached data
- **Cost Savings:** Fewer API requests = lower rate limit risk
- **Offline Testing:** Can run backtests with cached data (if available)

**Example:**
```
First run:  Fetch 4000 bars → 4 API calls → Store in cache
Second run: Fetch 4000 bars → 0 API calls (use cache) → Instant
```

### 3. Data Quality Validation (NEW) ✅

**Status:** ✅ **Implemented** (roadmap improvement)

All fetched data is validated before use to ensure quality and consistency.

**Validation Checks:**

1. **OHLC Consistency** — Ensures `low ≤ open/close ≤ high`
2. **Negative Prices/Volumes** — Detects invalid negative values
3. **Gap Detection** — Identifies missing bars vs timeframe
4. **Spike Detection** — Detects extreme moves via z-score (default: >5σ)

**Configuration:**
```yaml
data:
  validation:
    enabled: true                    # Enable validation (default: true)
    check_ohlc_consistency: true     # Check OHLC relationships
    check_negative_volume: true      # Check for negative volumes
    check_gaps: true                 # Check for missing bars
    check_spikes: true               # Check for extreme moves
    spike_zscore_threshold: 5.0      # Z-score threshold for spikes
```

**Implementation:**
- `src/data/validator.py::validate_ohlcv()` — Validates single symbol's data
- `src/data/validator.py::validate_before_backtest()` — Validates all symbols before backtest
- `src/exchange.py::ExchangeWrapper` — Validates data after fetching (before caching)

**Behavior:**
- **Errors:** Logged as errors (data still used, but logged for debugging)
- **Warnings:** Logged as warnings (e.g., gaps, spikes — may be legitimate)
- **Non-Fatal:** Validation failures don't crash the bot (continues with warnings)
- **Logging:** Clear log prefixes (`[VALIDATE]`) make issues easy to spot

**Example Log Output:**
```
[VALIDATE] BTC/USDT:USDT: WARNING - 3 potential gaps detected (timestamp jumps > 60 min)
[VALIDATE] ETH/USDT:USDT: ERROR - 2 bars with invalid OHLC relationships (low > open/close)
```

**Rationale:**
- **Data Quality:** Ensures backtests use clean data (reduces false positives)
- **Issue Detection:** Identifies exchange data issues early
- **Debugging:** Helps identify when data problems affect results

---

## Equity History & Long-Term Tracking

### Extended Equity History (NEW) ✅

**Status:** ✅ **Implemented** (roadmap improvement)

The bot now tracks **365 days of equity history** (extended from 60 days) for long-term analysis.

**Storage:**
- **Location:** `state.json` (in `equity_history` key)
- **Format:** `{timestamp_iso: equity_value, ...}`
- **Retention:** Keeps last 365 days (automatically pruned)

**Update Frequency:**
- Updated every trading cycle (default: every 10 seconds during active trading)
- Daily snapshots stored at cycle start

**Implementation:**
- `src/live.py::run_live()` — Updates equity history every cycle
- `src/risk.py::compute_long_term_drawdowns()` — Computes 90/180/365-day DDs

**Configuration:**
- Automatic (no config needed) — Equity history tracked by default

### Long-Term Drawdown Tracking (NEW) ✅

**Status:** ✅ **Implemented** (roadmap improvement)

The bot computes drawdowns from high watermarks over multiple time windows:

- **90-day DD** — Short-term drawdown tracking
- **180-day DD** — Medium-term drawdown tracking
- **365-day DD** — Long-term drawdown tracking

**Mechanism:**
1. Find high watermark within each window (90/180/365 days)
2. Compute drawdown from high watermark to current equity
3. Log warnings if drawdowns exceed thresholds (optional)

**Configuration:**
```yaml
risk:
  long_term_dd:
    enabled: true          # Enable long-term DD tracking (default: false)
    max_dd_90d: 0.3        # 90-day DD threshold (default: 30%)
    max_dd_180d: 0.4       # 180-day DD threshold (default: 40%)
    max_dd_365d: 0.5       # 365-day DD threshold (default: 50%)
```

**Implementation:**
- `src/risk.py::compute_long_term_drawdowns()` — Computes all three DDs
- `src/live.py::run_live()` — Logs warnings if thresholds exceeded

**Example:**
```
Current equity: $9,000
90-day high: $12,000 → 90-day DD: 25% (below 30% threshold, OK)
180-day high: $10,500 → 180-day DD: 14.3% (below 40% threshold, OK)
365-day high: $11,000 → 365-day DD: 18.2% (below 50% threshold, OK)

If 90-day DD > 30%:
[LONG-DD] 90-day drawdown: 35.00% (threshold: 30.00%) ⚠️
```

**Rationale:**
- **Long-Term Perspective:** Tracks performance beyond 30-day window
- **Extended Drawdowns:** Identifies "slow death" scenarios
- **Early Warning:** Alerts when drawdowns persist over long periods

---

## Funding Cost Tracking

### Funding PnL Integration

The bot tracks funding costs as positions are held:

- **Per-Symbol Tracking:** `state["funding_costs"][symbol]` — Cumulative funding PnL per symbol
- **Total Tracking:** `state["total_funding_cost"]` — Total cumulative funding cost
- **Integration:** Funding costs affect equity calculations

**Implementation:**
- `src/live.py::run_live()` — Tracks funding costs over time
- `src/exchange.py::ExchangeWrapper` — Fetches funding rates

**Configuration:**
```yaml
notifications:
  monitoring:
    cost_tracking:
      enabled: true                    # Enable cost tracking (default: true)
      compare_to_backtest: true        # Compare live costs to backtest
      alert_threshold_pct: 20.0        # Alert if costs exceed backtest by N%
```

**Behavior:**
- **Tracking:** Funding costs accumulated over time
- **Reporting:** Included in daily reports (if Discord enabled)
- **Alerts:** Warns if live costs significantly exceed backtest assumptions

**Rationale:**
- **Cost Awareness:** Tracks actual funding costs vs backtest assumptions
- **Performance Attribution:** Separates funding costs from trading PnL
- **Optimization:** Helps identify when funding costs are too high

---

## Data Pipeline Flow

```
1. Request Historical Data (optimizer/backtest/live)
   ↓
2. Check Cache (if enabled)
   ├─ Cache Hit: Return cached data (skip API call)
   └─ Cache Miss: Proceed to fetch
   ↓
3. Fetch from Exchange (Bybit via CCXT)
   ├─ Paginate if limit > 1000 bars
   └─ Rate limit between requests
   ↓
4. Validate Data
   ├─ Check OHLC consistency
   ├─ Check for negative values
   ├─ Check for gaps
   └─ Check for spikes
   ↓
5. Store in Cache (if enabled and validated)
   ↓
6. Return Data (to optimizer/backtest/live)
```

---

## Configuration Summary

```yaml
# Historical data fetching
data:
  # Pagination settings
  max_candles_per_request: 1000      # Bybit's limit (do not change)
  max_candles_total: 50000           # Safety cap
  api_throttle_sleep_ms: 200         # Sleep between requests (ms)
  max_pagination_requests: 100       # Max pagination requests
  
  # Cache (NEW)
  cache:
    enabled: false                   # Enable cache (default: false)
    db_path: "data/ohlcv_cache.db"   # Cache database path
    max_candles_total: 50000         # Max candles per symbol/timeframe
  
  # Validation (NEW)
  validation:
    enabled: true                    # Enable validation (default: true)
    check_ohlc_consistency: true     # Check OHLC relationships
    check_negative_volume: true      # Check for negative volumes
    check_gaps: true                 # Check for missing bars
    check_spikes: true               # Check for extreme moves
    spike_zscore_threshold: 5.0      # Z-score threshold for spikes

# Long-term drawdown tracking (NEW)
risk:
  long_term_dd:
    enabled: false                   # Enable long-term DD tracking
    max_dd_90d: 0.3                  # 90-day DD threshold (30%)
    max_dd_180d: 0.4                 # 180-day DD threshold (40%)
    max_dd_365d: 0.5                 # 365-day DD threshold (50%)

# Cost tracking
notifications:
  monitoring:
    cost_tracking:
      enabled: true                  # Enable cost tracking
      compare_to_backtest: true      # Compare live vs backtest
      alert_threshold_pct: 20.0      # Alert if costs exceed by 20%
```

---

## Best Practices

1. **Enable Cache for Optimizer Runs:**
   - Reduces API calls during optimization
   - Speeds up repeated runs
   - Set `data.cache.enabled: true`

2. **Enable Validation:**
   - Catches data quality issues early
   - Helps debug backtest discrepancies
   - Set `data.validation.enabled: true`

3. **Monitor Long-Term Drawdowns:**
   - Identifies extended drawdown periods
   - Set `risk.long_term_dd.enabled: true` for alerts

4. **Track Funding Costs:**
   - Compare live costs to backtest assumptions
   - Set `notifications.monitoring.cost_tracking.enabled: true`

---

## Next Steps

- **Exchange Integration:** [`../architecture/high_level_architecture.md`](high_level_architecture.md) — Exchange wrapper details
- **Optimizer Data:** [`../operations/optimizer_data_requirements.md`](../operations/optimizer_data_requirements.md) — Optimizer data needs
- **Config Reference:** [`../reference/config_reference.md`](../reference/config_reference.md) — All data config parameters

---

**Motto: MAKE MONEY** — with clean, validated, cached data that reduces costs and improves reliability. 📈

