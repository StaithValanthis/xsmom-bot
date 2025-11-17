#!/usr/bin/env python3
"""
Test script for historical data loader.

Tests pagination and date range fetching to ensure the 1000-item limit is handled correctly.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from datetime import datetime, timedelta
import pandas as pd

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import load_config
from src.exchange import ExchangeWrapper
from src.optimizer.backtest_runner import fetch_historical_data

def test_limit_based_fetch(cfg, symbol: str, limit: int):
    """Test limit-based fetching (most recent N bars)."""
    print(f"\n{'='*60}")
    print(f"TEST 1: Limit-based fetch ({limit} bars)")
    print(f"{'='*60}")
    
    ex = ExchangeWrapper(cfg.exchange, data_cfg=cfg.data)
    try:
        raw = ex.fetch_ohlcv(symbol, cfg.exchange.timeframe, limit=limit)
        
        if not raw:
            print(f"❌ No data returned")
            return False
        
        df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
        df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        df.set_index("dt", inplace=True)
        
        print(f"✓ Fetched {len(df)} bars")
        print(f"  Date range: {df.index[0]} to {df.index[-1]}")
        print(f"  Duration: {(df.index[-1] - df.index[0]).total_seconds() / 3600:.1f} hours")
        
        # Check for duplicates
        dup_count = df.index.duplicated().sum()
        if dup_count > 0:
            print(f"  ⚠️  {dup_count} duplicate timestamps found")
        else:
            print(f"  ✓ No duplicate timestamps")
        
        # Check for gaps
        gaps = df.index.to_series().diff()
        large_gaps = gaps[gaps > pd.Timedelta(hours=2)]
        if len(large_gaps) > 0:
            print(f"  ⚠️  {len(large_gaps)} large gaps found (>2 hours)")
        else:
            print(f"  ✓ No large gaps")
        
        # Verify we got close to requested amount
        if limit <= 1000:
            expected = limit
        else:
            # With pagination, we might get slightly more or less
            expected = limit
        
        if abs(len(df) - expected) <= 10:  # Allow small variance
            print(f"  ✓ Bar count matches expected ({expected})")
            return True
        else:
            print(f"  ⚠️  Bar count mismatch: got {len(df)}, expected ~{expected}")
            return len(df) >= expected * 0.9  # At least 90% of requested
        
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        ex.close()

def test_date_range_fetch(cfg, symbol: str, days: int):
    """Test date range fetching."""
    print(f"\n{'='*60}")
    print(f"TEST 2: Date range fetch ({days} days)")
    print(f"{'='*60}")
    
    end_time = pd.Timestamp.now(tz='UTC')
    start_time = end_time - pd.Timedelta(days=days)
    
    print(f"  Requested range: {start_time} to {end_time}")
    
    ex = ExchangeWrapper(cfg.exchange, data_cfg=cfg.data)
    try:
        start_ts = int(start_time.timestamp() * 1000)
        end_ts = int(end_time.timestamp() * 1000)
        
        raw = ex.fetch_ohlcv_range(
            symbol,
            cfg.exchange.timeframe,
            start_ts=start_ts,
            end_ts=end_ts,
            max_candles=cfg.data.max_candles_total,
        )
        
        if not raw:
            print(f"❌ No data returned")
            return False
        
        df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
        df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        df.set_index("dt", inplace=True)
        
        print(f"✓ Fetched {len(df)} bars")
        print(f"  Actual range: {df.index[0]} to {df.index[-1]}")
        
        # Check coverage
        coverage_start = (df.index[0] - start_time).total_seconds() / 3600
        coverage_end = (end_time - df.index[-1]).total_seconds() / 3600
        
        if coverage_start <= 24:  # Allow up to 24h before start
            print(f"  ✓ Start coverage: {coverage_start:.1f} hours before requested")
        else:
            print(f"  ⚠️  Start coverage: {coverage_start:.1f} hours before requested (large gap)")
        
        if coverage_end <= 24:
            print(f"  ✓ End coverage: {coverage_end:.1f} hours after requested")
        else:
            print(f"  ⚠️  End coverage: {coverage_end:.1f} hours after requested (large gap)")
        
        # Check for duplicates
        dup_count = df.index.duplicated().sum()
        if dup_count > 0:
            print(f"  ⚠️  {dup_count} duplicate timestamps found")
        else:
            print(f"  ✓ No duplicate timestamps")
        
        # Check ordering
        if df.index.is_monotonic_increasing:
            print(f"  ✓ Timestamps are in chronological order")
        else:
            print(f"  ❌ Timestamps are NOT in chronological order")
            return False
        
        return True
        
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        ex.close()

def test_optimizer_integration(cfg, symbol: str):
    """Test integration with optimizer's fetch_historical_data."""
    print(f"\n{'='*60}")
    print(f"TEST 3: Optimizer integration")
    print(f"{'='*60}")
    
    try:
        bars, symbols = fetch_historical_data(
            cfg,
            symbols=[symbol],
            use_date_range=False,  # Use limit-based (backward compatible)
        )
        
        if not bars or symbol not in bars:
            print(f"❌ No data returned for {symbol}")
            return False
        
        df = bars[symbol]
        print(f"✓ Fetched {len(df)} bars via fetch_historical_data")
        print(f"  Date range: {df.index[0]} to {df.index[-1]}")
        
        # Check duplicates
        dup_count = df.index.duplicated().sum()
        if dup_count > 0:
            print(f"  ⚠️  {dup_count} duplicate timestamps found")
        else:
            print(f"  ✓ No duplicate timestamps")
        
        # Verify we got expected amount
        expected_min = min(cfg.exchange.candles_limit, cfg.data.max_candles_total)
        if len(df) >= expected_min * 0.9:
            print(f"  ✓ Bar count meets minimum expectation ({expected_min})")
            return True
        else:
            print(f"  ⚠️  Bar count below expectation: {len(df)} < {expected_min}")
            return False
        
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return False

def main():
    parser = argparse.ArgumentParser(description="Test historical data loader")
    parser.add_argument("--config", type=str, default="config/config.yaml", help="Config file path")
    parser.add_argument("--symbol", type=str, default="BTC/USDT:USDT", help="Symbol to test")
    parser.add_argument("--limit", type=int, default=2000, help="Number of bars to fetch (test pagination)")
    parser.add_argument("--days", type=int, default=30, help="Days of history for date range test")
    parser.add_argument("--skip-limit", action="store_true", help="Skip limit-based test")
    parser.add_argument("--skip-range", action="store_true", help="Skip date range test")
    parser.add_argument("--skip-integration", action="store_true", help="Skip optimizer integration test")
    
    args = parser.parse_args()
    
    print("="*60)
    print("Historical Data Loader Test")
    print("="*60)
    print(f"Config: {args.config}")
    print(f"Symbol: {args.symbol}")
    print(f"Timeframe: (from config)")
    
    try:
        cfg = load_config(args.config)
        print(f"Timeframe: {cfg.exchange.timeframe}")
        print(f"Candles limit: {cfg.exchange.candles_limit}")
        print(f"Data config:")
        print(f"  max_candles_per_request: {cfg.data.max_candles_per_request}")
        print(f"  max_candles_total: {cfg.data.max_candles_total}")
        print(f"  api_throttle_sleep_ms: {cfg.data.api_throttle_sleep_ms}")
        print(f"  max_pagination_requests: {cfg.data.max_pagination_requests}")
    except Exception as e:
        print(f"❌ Failed to load config: {e}")
        return 1
    
    results = []
    
    if not args.skip_limit:
        results.append(("Limit-based fetch", test_limit_based_fetch(cfg, args.symbol, args.limit)))
    
    if not args.skip_range:
        results.append(("Date range fetch", test_date_range_fetch(cfg, args.symbol, args.days)))
    
    if not args.skip_integration:
        results.append(("Optimizer integration", test_optimizer_integration(cfg, args.symbol)))
    
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    
    all_passed = True
    for name, passed in results:
        status = "✓ PASS" if passed else "❌ FAIL"
        print(f"{status}: {name}")
        if not passed:
            all_passed = False
    
    if all_passed:
        print("\n✓ All tests passed!")
        return 0
    else:
        print("\n❌ Some tests failed. Review output above.")
        return 1

if __name__ == "__main__":
    sys.exit(main())

