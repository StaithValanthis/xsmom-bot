# Bybit 1000-Item Limit Solution

## Problem Summary

Bybit's API limits single OHLCV requests to **1000 bars per request**. When the optimizer requests more than 1000 bars (e.g., `candles_limit: 4000`), the system must paginate to fetch the full range.

### Where It Manifests

- **Endpoint**: Bybit's kline endpoint (via CCXT `fetch_ohlcv`)
- **Limit**: 1000 bars per single API call
- **Impact**: 
  - Optimizer requests >1000 bars but only gets 1000 (silent truncation)
  - WFO requires 3648+ bars (120/30/2 days at 1h) but fails with insufficient data
  - Backtests are truncated when longer histories are needed

## Solution Implemented

### 1. Enhanced Pagination in `ExchangeWrapper.fetch_ohlcv()`

**Location**: `src/exchange.py`

**Features**:
- Automatic pagination when `limit > 1000`
- Backward pagination (most recent → older) for "most recent N bars"
- Deduplication during fetching and final pass
- Rate limiting with configurable delays
- Error handling with retry logic

**How it works**:
1. First chunk: Fetches most recent 1000 bars (since=None)
2. Subsequent chunks: Goes backwards in time using oldest timestamp - timeframe_ms
3. Deduplicates timestamps as it goes
4. Combines chunks, sorts chronologically, trims to exact limit

### 2. New Date Range Fetcher: `fetch_ohlcv_range()`

**Location**: `src/exchange.py`

**Purpose**: Fetch data for explicit date ranges (start_ts to end_ts)

**Features**:
- Forward pagination (oldest → newest)
- Respects date range boundaries
- Configurable safety limits
- Rate limiting

**Usage**:
```python
ex = ExchangeWrapper(cfg.exchange, data_cfg=cfg.data)
bars = ex.fetch_ohlcv_range(
    symbol="BTC/USDT:USDT",
    timeframe="1h",
    start_ts=int(start_time.timestamp() * 1000),
    end_ts=int(end_time.timestamp() * 1000),
    max_candles=5000
)
```

### 3. Configuration Options

**Location**: `config/config.yaml` → `data.*` section

```yaml
data:
  max_candles_per_request: 1000  # Bybit's limit (do not change)
  max_candles_total: 50000       # Safety cap per symbol/timeframe
  api_throttle_sleep_ms: 200     # Sleep between paginated requests (ms)
  max_pagination_requests: 100   # Safety limit on pagination requests
```

**Defaults**:
- `max_candles_per_request`: 1000 (Bybit's limit)
- `max_candles_total`: 50000 (allows up to 50,000 bars per symbol)
- `api_throttle_sleep_ms`: 200ms (respectful of rate limits)
- `max_pagination_requests`: 100 (allows up to 100,000 bars total)

### 4. Integration with Optimizer

**Location**: `src/optimizer/backtest_runner.py::fetch_historical_data()`

**Enhancements**:
- Accepts `start_time` and `end_time` parameters
- Optional `use_date_range` flag for explicit date range fetching
- Automatically uses pagination when `candles_limit > 1000`
- Passes `data_cfg` to `ExchangeWrapper` for configurable behavior

## Bybit API Research Summary

**Per-request limit**: 1000 bars (confirmed)

**Pagination mechanism**: 
- Uses `since` parameter (timestamp in milliseconds)
- CCXT returns oldest-first
- Forward pagination: `since = last_timestamp + timeframe_ms`
- Backward pagination: `since = oldest_timestamp - timeframe_ms`

**Rate limits**:
- Public endpoints: ~120 requests/minute (varies by endpoint)
- Recommended: 200ms delay between pagination requests
- Automatic backoff on rate limit errors (2 second wait)

## Testing

### Test Script

**Location**: `tools/test_data_loader.py`

**Usage**:
```bash
# Test limit-based pagination (2000 bars)
python tools/test_data_loader.py --config config/config.yaml --symbol BTC/USDT:USDT --limit 2000

# Test date range fetching (30 days)
python tools/test_data_loader.py --config config/config.yaml --symbol BTC/USDT:USDT --days 30

# Test optimizer integration
python tools/test_data_loader.py --config config/config.yaml --symbol BTC/USDT:USDT --skip-limit --skip-range
```

**Expected Output**:
- ✓ Fetched ~2000 bars (not just 1000)
- ✓ No duplicate timestamps
- ✓ Date range covers expected period
- ✓ Timestamps in chronological order

### Manual Test

```python
from src.config import load_config
from src.exchange import ExchangeWrapper

cfg = load_config('config/config.yaml')
ex = ExchangeWrapper(cfg.exchange, data_cfg=cfg.data)

# Test pagination
bars = ex.fetch_ohlcv('BTC/USDT:USDT', '1h', limit=2000)
print(f"Fetched {len(bars)} bars (expected ~2000)")

# Test date range
import pandas as pd
end_time = pd.Timestamp.now(tz='UTC')
start_time = end_time - pd.Timedelta(days=30)
start_ts = int(start_time.timestamp() * 1000)
end_ts = int(end_time.timestamp() * 1000)

bars = ex.fetch_ohlcv_range('BTC/USDT:USDT', '1h', start_ts, end_ts)
print(f"Fetched {len(bars)} bars for date range")
```

## Configuration Recommendations

### For WFO (120/30/2 days at 1h)

```yaml
exchange:
  candles_limit: 4000  # ~167 days (comfortable margin)

data:
  max_candles_total: 5000  # Safety cap (>= candles_limit)
  api_throttle_sleep_ms: 200  # Standard delay
  max_pagination_requests: 100  # Allows up to 100k bars
```

### For Longer Backtests

```yaml
exchange:
  candles_limit: 10000  # ~417 days

data:
  max_candles_total: 12000  # Safety cap
  api_throttle_sleep_ms: 300  # Slightly longer delay
  max_pagination_requests: 150  # More requests allowed
```

### If Hitting Rate Limits

```yaml
data:
  api_throttle_sleep_ms: 500  # Increase delay
  max_pagination_requests: 50  # Reduce concurrent requests
```

## Troubleshooting

### "Only 1000 bars fetched despite candles_limit > 1000"

**Causes**:
1. Pagination failing silently (check logs)
2. `data.max_candles_total` too low
3. `data.max_pagination_requests` limit hit
4. Network/timeout issues

**Solutions**:
1. Check logs for pagination warnings/errors
2. Increase `data.max_candles_total`
3. Increase `data.max_pagination_requests`
4. Test manually with `tools/test_data_loader.py`

### "Rate limit errors during pagination"

**Causes**:
1. Too aggressive fetching (low `api_throttle_sleep_ms`)
2. Fetching many symbols simultaneously
3. Bybit API issues

**Solutions**:
1. Increase `data.api_throttle_sleep_ms` to 500ms
2. Reduce `exchange.max_symbols` or fetch in batches
3. Check Bybit API status

### "Duplicate timestamps in data"

**Causes**:
1. Pagination overlap (should be handled automatically)
2. Exchange returning duplicate data

**Solutions**:
1. Deduplication is automatic, but check logs
2. Verify exchange API behavior
3. Report if duplicates persist after deduplication

## Files Modified

1. **`src/config.py`**: Added `DataCfg` class and integration
2. **`src/exchange.py`**: Enhanced `fetch_ohlcv()` and added `fetch_ohlcv_range()`
3. **`src/optimizer/backtest_runner.py`**: Updated `fetch_historical_data()` to support date ranges
4. **`config/config.yaml.example`**: Added `data.*` section
5. **`docs/usage/optimizer.md`**: Added "Bybit Historical Data & 1000-Item Limit" section
6. **`docs/reference/config_reference.md`**: Added "Data" section
7. **`docs/operations/optimizer_data_requirements.md`**: Updated with new solution details
8. **`tools/test_data_loader.py`**: New test script

## Backward Compatibility

- **Existing code**: Works unchanged (uses defaults if `data_cfg` not provided)
- **Config files**: Old configs work (defaults applied via `_merge_defaults`)
- **API**: `fetch_ohlcv()` signature unchanged, behavior enhanced
- **Optimizer**: No changes required, automatically benefits from pagination

## Performance Impact

- **Pagination overhead**: ~200ms per 1000 bars (configurable)
- **For 4000 bars**: ~600ms total (3 requests × 200ms)
- **Acceptable**: Negligible compared to backtest/optimization time
- **Rate limit safety**: Prevents API throttling and errors

## Future Enhancements

Potential improvements (not implemented):
1. **Caching**: Cache fetched data to avoid repeated API calls
2. **Parallel fetching**: Fetch multiple symbols in parallel (with rate limit awareness)
3. **Database storage**: Store historical data in TimescaleDB for faster access
4. **Incremental updates**: Only fetch new data since last update

---

**Status**: ✅ Implemented and tested
**Last Updated**: 2025-11-17

