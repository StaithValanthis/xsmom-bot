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
        return stats if stats else {}
    except Exception as e:
        log.error(f"Backtest failed: {e}")
        raise


def fetch_historical_data(
    cfg: AppConfig,
    symbols: Optional[List[str]] = None,
    end_time: Optional[pd.Timestamp] = None,
) -> Tuple[Dict[str, pd.DataFrame], List[str]]:
    """
    Fetch historical OHLCV data for symbols.
    
    Args:
        cfg: Config object (for exchange settings)
        symbols: Optional symbol list (if None, fetched from exchange)
        end_time: Optional end timestamp (defaults to now)
    
    Returns:
        Tuple of (bars_dict, symbol_list)
    """
    ex = ExchangeWrapper(cfg.exchange)
    try:
        if symbols is None:
            symbols_list = ex.fetch_markets_filtered()
        else:
            symbols_list = symbols
        
        if not symbols_list:
            log.warning("No symbols available")
            return {}, []
        
        bars: Dict[str, pd.DataFrame] = {}
        for sym in symbols_list:
            try:
                raw = ex.fetch_ohlcv(
                    sym,
                    timeframe=cfg.exchange.timeframe,
                    limit=cfg.exchange.candles_limit,
                )
                if raw:
                    df = pd.DataFrame(
                        raw,
                        columns=["ts", "open", "high", "low", "close", "volume"]
                    )
                    df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
                    df.set_index("dt", inplace=True)
                    if end_time is not None:
                        df = df[df.index <= end_time]
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

