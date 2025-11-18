"""
Backtest runner wrapper for optimizer use.

Provides clean entrypoint for running backtests with parameter overrides.
"""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
import yaml
import pandas as pd

from ..config import load_config, AppConfig
from ..backtester import run_backtest
from ..exchange import ExchangeWrapper

log = logging.getLogger("optimizer.backtest_runner")


def patch_config(base_cfg: AppConfig, overrides: Dict[str, Any]) -> AppConfig:
    """
    Apply parameter overrides to a base config.
    
    Args:
        base_cfg: Base AppConfig object
        overrides: Dict of path-value pairs, e.g., {"strategy.signal_power": 1.5}
    
    Returns:
        New AppConfig with overrides applied
    """
    # Convert to dict, apply deep updates, reload
    cfg_dict = base_cfg.model_dump()
    
    for path, value in overrides.items():
        _deep_set(cfg_dict, path, value)
    
    # Reload from dict (handles validation)
    return AppConfig.model_validate(cfg_dict)


def _deep_set(d: Dict[str, Any], path: str, value: Any) -> None:
    """Set a nested dict value using dot notation."""
    keys = path.split(".")
    cur = d
    for k in keys[:-1]:
        # Handle array indices like "lookbacks[0]"
        if "[" in k:
            k, idx_str = k.split("[")
            idx = int(idx_str.rstrip("]"))
            if k not in cur:
                cur[k] = []
            while len(cur[k]) <= idx:
                cur[k].append(0)
            cur = cur[k][idx]
        else:
            if k not in cur or not isinstance(cur[k], dict):
                cur[k] = {}
            cur = cur[k]
    
    final_key = keys[-1]
    if "[" in final_key:
        final_key, idx_str = final_key.split("[")
        idx = int(idx_str.rstrip("]"))
        if final_key not in cur:
            cur[final_key] = []
        while len(cur[final_key]) <= idx:
            cur[final_key].append(0)
        cur[final_key][idx] = value
    else:
        cur[final_key] = value


def run_backtest_with_params(
    base_cfg: AppConfig,
    param_overrides: Dict[str, Any],
    symbols: Optional[List[str]] = None,
    prefetch_bars: Optional[Dict[str, pd.DataFrame]] = None,
    return_curve: bool = False,
) -> Dict[str, Any]:
    """
    Run backtest with parameter overrides.
    
    Args:
        base_cfg: Base config object
        param_overrides: Parameter overrides (dot notation paths)
        symbols: Optional symbol list (if None, derived from cfg)
        prefetch_bars: Optional prefetched OHLCV data
        return_curve: If True, include equity_curve in results
    
    Returns:
        Dict of performance metrics
    """
    try:
        patched_cfg = patch_config(base_cfg, param_overrides)
    except Exception as e:
        log.error(f"Failed to patch config: {e}")
        raise
    
    try:
        stats = run_backtest(
            cfg=patched_cfg,
            symbols=symbols,
            prefetch_bars=prefetch_bars,
            return_curve=return_curve,
        )
        
        if not stats:
            log.warning("Backtest returned empty stats - possible causes: no trades, all filters blocking, or data issues")
            return {}
        
        # Log key metrics for debugging
        total_return = stats.get("total_return", 0.0)
        sharpe = stats.get("sharpe", 0.0)
        trades = stats.get("trades", 0)
        
        if total_return == 0.0 and sharpe == 0.0 and trades == 0:
            log.warning(
                f"Backtest produced zero metrics - total_return={total_return}, sharpe={sharpe}, trades={trades}. "
                f"Possible causes: no trades executed, all symbols filtered, or parameter combination produces no signals."
            )
        else:
            log.debug(f"Backtest metrics: total_return={total_return:.4f}, sharpe={sharpe:.4f}, trades={trades}")
        
        return stats
    except Exception as e:
        log.error(f"Backtest failed: {e}")
        import traceback
        log.debug(traceback.format_exc())
        raise


def fetch_historical_data(
    cfg: AppConfig,
    symbols: Optional[List[str]] = None,
    end_time: Optional[pd.Timestamp] = None,
    start_time: Optional[pd.Timestamp] = None,
    use_date_range: bool = False,
) -> Tuple[Dict[str, pd.DataFrame], List[str]]:
    """
    Fetch historical OHLCV data for symbols.
    
    Args:
        cfg: Config object (for exchange settings)
        symbols: Optional symbol list (if None, fetched from exchange)
        end_time: Optional end timestamp (defaults to now)
        start_time: Optional start timestamp (for date range fetching)
        use_date_range: If True, use fetch_ohlcv_range for explicit date ranges
    
    Returns:
        Tuple of (bars_dict, symbol_list)
    """
    ex = ExchangeWrapper(cfg.exchange, data_cfg=cfg.data)
    try:
        if symbols is None:
            symbols_list = ex.fetch_markets_filtered()
        else:
            symbols_list = symbols
        
        if not symbols_list:
            log.warning("No symbols available")
            return {}, []
        
        # Determine time range
        if end_time is None:
            end_time = pd.Timestamp.now(tz='UTC')
        
        end_ts = int(end_time.timestamp() * 1000)
        start_ts = None
        if start_time is not None:
            start_ts = int(start_time.timestamp() * 1000)
        elif use_date_range:
            # If use_date_range but no start_time, estimate from candles_limit
            timeframe_ms = ex._timeframe_to_ms(cfg.exchange.timeframe)
            if timeframe_ms:
                start_ts = end_ts - (cfg.exchange.candles_limit * timeframe_ms)
        
        # Automatically use pagination if we need more than 1000 bars
        max_per_request = cfg.data.max_candles_per_request if cfg.data else 1000
        use_pagination = cfg.exchange.candles_limit > max_per_request
        
        # Calculate start_ts if we need pagination but don't have it
        if use_pagination and start_ts is None:
            timeframe_ms = ex._timeframe_to_ms(cfg.exchange.timeframe)
            if timeframe_ms:
                start_ts = end_ts - (cfg.exchange.candles_limit * timeframe_ms)
                log.info(f"Auto-enabling pagination: need {cfg.exchange.candles_limit} bars (API limit: {max_per_request})")
        
        bars: Dict[str, pd.DataFrame] = {}
        for sym in symbols_list:
            try:
                if (use_date_range or use_pagination) and start_ts is not None:
                    # Use date range fetching with pagination
                    raw = ex.fetch_ohlcv_range(
                        sym,
                        timeframe=cfg.exchange.timeframe,
                        start_ts=start_ts,
                        end_ts=end_ts,
                        max_candles=min(cfg.exchange.candles_limit, cfg.data.max_candles_total if cfg.data else 50000),
                    )
                else:
                    # Use limit-based fetching (backward compatible, capped at API limit)
                    raw = ex.fetch_ohlcv(
                        sym,
                        timeframe=cfg.exchange.timeframe,
                        limit=min(cfg.exchange.candles_limit, max_per_request),
                    )
                
                if raw:
                    df = pd.DataFrame(
                        raw,
                        columns=["ts", "open", "high", "low", "close", "volume"]
                    )
                    df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
                    df.set_index("dt", inplace=True)
                    
                    # Filter by time range if needed
                    if end_time is not None:
                        df = df[df.index <= end_time]
                    if start_time is not None:
                        df = df[df.index >= start_time]
                    
                    # Remove duplicates (safety check)
                    if df.index.duplicated().any():
                        dup_count = df.index.duplicated().sum()
                        log.debug(f"Removing {dup_count} duplicate timestamps from {sym}")
                        df = df[~df.index.duplicated(keep='first')]
                    
                    if len(df) > 0:
                        bars[sym] = df
            except Exception as e:
                log.warning(f"Failed to fetch {sym}: {e}")
        
        return bars, symbols_list
    finally:
        try:
            ex.close()
        except Exception:
            pass

