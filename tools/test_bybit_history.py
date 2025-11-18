#!/usr/bin/env python3
"""
Test harness for Bybit historical data fetching with pagination.

Tests the bulk historical data loader to verify it can fetch 4000+ bars
despite Bybit's 1000-per-request limit.

Usage:
    python tools/test_bybit_history.py --symbol BTC/USDT:USDT --timeframe 1h --target-bars 4000
    python tools/test_bybit_history.py --symbol BTC/USDT:USDT --timeframe 1h --target-bars 10000 --config config/config.yaml
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
import pandas as pd

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import load_config
from src.exchange import ExchangeWrapper

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s'
)
log = logging.getLogger(__name__)


def test_fetch_ohlcv_limit(
    ex: ExchangeWrapper,
    symbol: str,
    timeframe: str,
    target_bars: int,
) -> dict:
    """
    Test limit-based fetching (most recent N bars).
    
    Returns:
        Dict with test results
    """
    log.info(f"\n{'='*60}")
    log.info(f"Test 1: Limit-based fetching ({target_bars} bars)")
    log.info(f"{'='*60}")
    
    start_time = datetime.now(timezone.utc)
    
    try:
        raw = ex.fetch_ohlcv(
            symbol=symbol,
            timeframe=timeframe,
            limit=target_bars,
            since=None,  # Most recent
        )
        
        end_time = datetime.now(timezone.utc)
        duration = (end_time - start_time).total_seconds()
        
        if not raw:
            return {
                "success": False,
                "error": "No data returned",
                "bars_fetched": 0,
                "duration_seconds": duration,
            }
        
        # Convert to DataFrame for analysis
        df = pd.DataFrame(
            raw,
            columns=["ts", "open", "high", "low", "close", "volume"]
        )
        df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        
        # Check for duplicates
        duplicates = df["ts"].duplicated().sum()
        
        # Calculate date range
        min_ts = df["ts"].min()
        max_ts = df["ts"].max()
        min_dt = pd.Timestamp(min_ts, unit="ms", tz="UTC")
        max_dt = pd.Timestamp(max_ts, unit="ms", tz="UTC")
        days_span = (max_dt - min_dt).total_seconds() / (24 * 3600)
        
        # Estimate API requests (assuming 1000 per request)
        max_per_request = ex.data_cfg.max_candles_per_request if ex.data_cfg else 1000
        estimated_requests = (len(raw) + max_per_request - 1) // max_per_request
        
        result = {
            "success": True,
            "bars_fetched": len(raw),
            "bars_requested": target_bars,
            "duplicates": duplicates,
            "duration_seconds": duration,
            "first_timestamp": min_dt.isoformat(),
            "last_timestamp": max_dt.isoformat(),
            "days_span": days_span,
            "estimated_api_requests": estimated_requests,
        }
        
        log.info(f"✓ Fetched {len(raw)} bars (requested {target_bars})")
        log.info(f"  Duration: {duration:.2f} seconds")
        log.info(f"  Date range: {min_dt.date()} to {max_dt.date()} ({days_span:.1f} days)")
        log.info(f"  Duplicates: {duplicates}")
        log.info(f"  Estimated API requests: {estimated_requests}")
        
        if len(raw) < target_bars:
            log.warning(f"⚠️  Fetched fewer bars ({len(raw)}) than requested ({target_bars})")
            result["warning"] = f"Only {len(raw)}/{target_bars} bars available"
        
        if duplicates > 0:
            log.warning(f"⚠️  Found {duplicates} duplicate timestamps")
        
        return result
        
    except Exception as e:
        log.error(f"✗ Test failed: {e}", exc_info=True)
        return {
            "success": False,
            "error": str(e),
            "bars_fetched": 0,
        }


def test_fetch_ohlcv_range(
    ex: ExchangeWrapper,
    symbol: str,
    timeframe: str,
    days_back: int,
) -> dict:
    """
    Test date-range-based fetching.
    
    Returns:
        Dict with test results
    """
    log.info(f"\n{'='*60}")
    log.info(f"Test 2: Date-range fetching (last {days_back} days)")
    log.info(f"{'='*60}")
    
    end_time = pd.Timestamp.now(tz='UTC')
    start_time = end_time - timedelta(days=days_back)
    
    start_ts = int(start_time.timestamp() * 1000)
    end_ts = int(end_time.timestamp() * 1000)
    
    log.info(f"Requesting: {start_time.date()} to {end_time.date()}")
    
    start_fetch = datetime.now(timezone.utc)
    
    try:
        raw = ex.fetch_ohlcv_range(
            symbol=symbol,
            timeframe=timeframe,
            start_ts=start_ts,
            end_ts=end_ts,
        )
        
        end_fetch = datetime.now(timezone.utc)
        duration = (end_fetch - start_fetch).total_seconds()
        
        if not raw:
            return {
                "success": False,
                "error": "No data returned",
                "bars_fetched": 0,
                "duration_seconds": duration,
            }
        
        # Convert to DataFrame
        df = pd.DataFrame(
            raw,
            columns=["ts", "open", "high", "low", "close", "volume"]
        )
        df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        
        # Check range coverage
        actual_start = pd.Timestamp(df["ts"].min(), unit="ms", tz="UTC")
        actual_end = pd.Timestamp(df["ts"].max(), unit="ms", tz="UTC")
        
        # Estimate requests
        max_per_request = ex.data_cfg.max_candles_per_request if ex.data_cfg else 1000
        estimated_requests = (len(raw) + max_per_request - 1) // max_per_request
        
        result = {
            "success": True,
            "bars_fetched": len(raw),
            "duration_seconds": duration,
            "requested_start": start_time.isoformat(),
            "requested_end": end_time.isoformat(),
            "actual_start": actual_start.isoformat(),
            "actual_end": actual_end.isoformat(),
            "estimated_api_requests": estimated_requests,
        }
        
        log.info(f"✓ Fetched {len(raw)} bars")
        log.info(f"  Duration: {duration:.2f} seconds")
        log.info(f"  Requested: {start_time.date()} to {end_time.date()}")
        log.info(f"  Actual: {actual_start.date()} to {actual_end.date()}")
        log.info(f"  Estimated API requests: {estimated_requests}")
        
        # Check if range is covered
        if actual_start > start_time:
            log.warning(f"⚠️  Actual start ({actual_start.date()}) is after requested start ({start_time.date()})")
            result["warning"] = "Range not fully covered (start)"
        
        if actual_end < end_time:
            log.warning(f"⚠️  Actual end ({actual_end.date()}) is before requested end ({end_time.date()})")
            result["warning"] = "Range not fully covered (end)"
        
        return result
        
    except Exception as e:
        log.error(f"✗ Test failed: {e}", exc_info=True)
        return {
            "success": False,
            "error": str(e),
            "bars_fetched": 0,
        }


def main():
    parser = argparse.ArgumentParser(
        description="Test Bybit historical data fetching with pagination"
    )
    parser.add_argument(
        "--config",
        type=str,
        default="config/config.yaml",
        help="Path to config.yaml",
    )
    parser.add_argument(
        "--symbol",
        type=str,
        default="BTC/USDT:USDT",
        help="Symbol to test (e.g., BTC/USDT:USDT)",
    )
    parser.add_argument(
        "--timeframe",
        type=str,
        default="1h",
        help="Timeframe (e.g., 1h, 5m, 1d)",
    )
    parser.add_argument(
        "--target-bars",
        type=int,
        default=4000,
        help="Number of bars to fetch (limit-based test)",
    )
    parser.add_argument(
        "--days-back",
        type=int,
        default=30,
        help="Days to go back (date-range test)",
    )
    parser.add_argument(
        "--test-limit",
        action="store_true",
        default=True,
        help="Run limit-based test (default: True)",
    )
    parser.add_argument(
        "--test-range",
        action="store_true",
        help="Run date-range test",
    )
    
    args = parser.parse_args()
    
    # Load config
    try:
        cfg = load_config(args.config)
        log.info(f"Loaded config from {args.config}")
        log.info(f"  candles_limit: {cfg.exchange.candles_limit}")
        log.info(f"  data.max_candles_per_request: {cfg.data.max_candles_per_request}")
        log.info(f"  data.max_candles_total: {cfg.data.max_candles_total}")
        log.info(f"  data.api_throttle_sleep_ms: {cfg.data.api_throttle_sleep_ms}")
    except Exception as e:
        log.error(f"Failed to load config: {e}")
        return 1
    
    # Create exchange wrapper
    try:
        ex = ExchangeWrapper(cfg.exchange, data_cfg=cfg.data)
        log.info(f"Initialized ExchangeWrapper for {cfg.exchange.id}")
    except Exception as e:
        log.error(f"Failed to initialize ExchangeWrapper: {e}")
        return 1
    
    results = {}
    
    try:
        # Test 1: Limit-based fetching
        if args.test_limit:
            results["limit_test"] = test_fetch_ohlcv_limit(
                ex=ex,
                symbol=args.symbol,
                timeframe=args.timeframe,
                target_bars=args.target_bars,
            )
        
        # Test 2: Date-range fetching
        if args.test_range:
            results["range_test"] = test_fetch_ohlcv_range(
                ex=ex,
                symbol=args.symbol,
                timeframe=args.timeframe,
                days_back=args.days_back,
            )
        
        # Summary
        log.info(f"\n{'='*60}")
        log.info("SUMMARY")
        log.info(f"{'='*60}")
        
        if "limit_test" in results:
            r = results["limit_test"]
            if r["success"]:
                log.info(f"✓ Limit test: {r['bars_fetched']}/{r['bars_requested']} bars fetched")
                if r.get("warning"):
                    log.warning(f"  Warning: {r['warning']}")
            else:
                log.error(f"✗ Limit test failed: {r.get('error', 'Unknown error')}")
        
        if "range_test" in results:
            r = results["range_test"]
            if r["success"]:
                log.info(f"✓ Range test: {r['bars_fetched']} bars fetched")
                if r.get("warning"):
                    log.warning(f"  Warning: {r['warning']}")
            else:
                log.error(f"✗ Range test failed: {r.get('error', 'Unknown error')}")
        
        # Overall success
        all_success = all(
            r.get("success", False)
            for r in results.values()
        )
        
        if all_success:
            log.info("\n✓ All tests passed!")
            return 0
        else:
            log.error("\n✗ Some tests failed")
            return 1
            
    finally:
        try:
            ex.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())

