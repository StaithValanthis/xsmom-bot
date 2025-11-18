"""
Data quality validation for OHLCV data.

Checks for common data quality issues before using data for backtests or live trading.
"""
from __future__ import annotations

import logging
from typing import List, Dict, Optional, Tuple
import numpy as np
import pandas as pd

log = logging.getLogger("data.validator")


class ValidationResult:
    """Result of data validation check."""
    
    def __init__(self, passed: bool, warnings: List[str], errors: List[str]):
        self.passed = passed
        self.warnings = warnings
        self.errors = errors
    
    def is_valid(self) -> bool:
        """Returns True if validation passed (no errors)."""
        return self.passed and len(self.errors) == 0


def validate_ohlcv(
    bars: List[List] | pd.DataFrame,
    symbol: str = "unknown",
    timeframe: str = "1h",
    check_ohlc_consistency: bool = True,
    check_negative_volume: bool = True,
    check_gaps: bool = True,
    check_spikes: bool = True,
    spike_zscore_threshold: float = 5.0,
) -> ValidationResult:
    """
    Validate OHLCV data quality.
    
    Args:
        bars: List of bars in CCXT format [[ts, open, high, low, close, volume], ...] or DataFrame
        symbol: Symbol name for logging
        timeframe: Timeframe for gap detection
        check_ohlc_consistency: Check that low <= open/close <= high
        check_negative_volume: Check for negative volumes
        check_gaps: Check for missing bars (timestamp gaps)
        check_spikes: Check for extreme price moves (z-score)
        spike_zscore_threshold: Z-score threshold for spike detection
    
    Returns:
        ValidationResult with passed flag, warnings, and errors
    """
    warnings = []
    errors = []
    
    try:
        # Convert to DataFrame if needed
        if isinstance(bars, list):
            if not bars:
                return ValidationResult(False, [], ["Empty bar list"])
            
            df = pd.DataFrame(bars, columns=["ts", "open", "high", "low", "close", "volume"])
        else:
            df = bars.copy()
        
        if df.empty:
            return ValidationResult(False, [], ["Empty DataFrame"])
        
        # Ensure required columns exist
        required_cols = ["open", "high", "low", "close", "volume"]
        missing = [c for c in required_cols if c not in df.columns]
        if missing:
            return ValidationResult(False, [], [f"Missing columns: {missing}"])
        
        # Check for negative prices
        negative_prices = (df[["open", "high", "low", "close"]] < 0).any(axis=1).sum()
        if negative_prices > 0:
            errors.append(f"{negative_prices} bars with negative prices")
        
        # Check for negative volumes
        if check_negative_volume:
            negative_vol = (df["volume"] < 0).sum()
            if negative_vol > 0:
                errors.append(f"{negative_vol} bars with negative volume")
        
        # Check OHLC consistency
        if check_ohlc_consistency:
            invalid_ohlc = (
                (df["low"] > df["open"]) |
                (df["low"] > df["close"]) |
                (df["high"] < df["open"]) |
                (df["high"] < df["close"]) |
                (df["low"] > df["high"])
            ).sum()
            
            if invalid_ohlc > 0:
                errors.append(f"{invalid_ohlc} bars with invalid OHLC relationships (low > open/close or high < open/close)")
        
        # Check for gaps (missing bars)
        if check_gaps and "ts" in df.columns and len(df) > 1:
            df_sorted = df.sort_values("ts")
            ts_diff = df_sorted["ts"].diff()
            
            # Estimate expected interval based on timeframe
            timeframe_ms_map = {
                "1m": 60_000,
                "5m": 300_000,
                "15m": 900_000,
                "1h": 3_600_000,
                "4h": 14_400_000,
                "1d": 86_400_000,
            }
            expected_interval = timeframe_ms_map.get(timeframe, 3_600_000)  # Default to 1h
            
            # Allow 10% tolerance
            gap_threshold = expected_interval * 1.1
            
            large_gaps = (ts_diff > gap_threshold).sum()
            if large_gaps > 0:
                warnings.append(f"{large_gaps} potential gaps detected (timestamp jumps > {expected_interval/1000/60:.0f} min)")
        
        # Check for spikes (extreme price moves)
        if check_spikes and len(df) > 10:
            # Compute log returns
            df_sorted = df.sort_values("ts") if "ts" in df.columns else df
            log_returns = np.log(df_sorted["close"] / df_sorted["close"].shift(1)).dropna()
            
            if len(log_returns) > 0:
                mean_ret = log_returns.mean()
                std_ret = log_returns.std()
                
                if std_ret > 0:
                    z_scores = np.abs((log_returns - mean_ret) / std_ret)
                    spikes = (z_scores > spike_zscore_threshold).sum()
                    
                    if spikes > 0:
                        warnings.append(f"{spikes} potential spikes detected (|z-score| > {spike_zscore_threshold})")
        
        # Check for zero volume bars (may indicate stale data)
        zero_vol = (df["volume"] == 0).sum()
        if zero_vol > len(df) * 0.1:  # More than 10% zero volume
            warnings.append(f"{zero_vol} bars with zero volume ({zero_vol/len(df)*100:.1f}%)")
        
        passed = len(errors) == 0
        
        if warnings or errors:
            log.warning(f"[VALIDATE] {symbol}: {len(errors)} errors, {len(warnings)} warnings")
            for err in errors:
                log.error(f"[VALIDATE] {symbol}: ERROR - {err}")
            for warn in warnings:
                log.warning(f"[VALIDATE] {symbol}: WARNING - {warn}")
        
        return ValidationResult(passed, warnings, errors)
    
    except Exception as e:
        log.error(f"[VALIDATE] {symbol}: Validation failed with exception: {e}", exc_info=True)
        return ValidationResult(False, [], [f"Validation exception: {e}"])


def validate_before_backtest(
    bars_dict: Dict[str, pd.DataFrame],
    cfg: Optional[Dict] = None,
) -> Tuple[bool, List[str]]:
    """
    Validate all symbols' data before running a backtest.
    
    Args:
        bars_dict: Dictionary of {symbol: DataFrame} with OHLCV data
        cfg: Optional config dict with validation settings
    
    Returns:
        Tuple of (all_valid, list_of_errors)
    """
    if cfg is None:
        cfg = {}
    
    val_cfg = cfg.get("data", {}).get("validation", {}) or {}
    enabled = val_cfg.get("enabled", True)
    
    if not enabled:
        return True, []
    
    all_valid = True
    all_errors = []
    
    for symbol, df in bars_dict.items():
        result = validate_ohlcv(
            df,
            symbol=symbol,
            timeframe=cfg.get("exchange", {}).get("timeframe", "1h"),
            check_ohlc_consistency=val_cfg.get("check_ohlc_consistency", True),
            check_negative_volume=val_cfg.get("check_negative_volume", True),
            check_gaps=val_cfg.get("check_gaps", True),
            check_spikes=val_cfg.get("check_spikes", True),
            spike_zscore_threshold=float(val_cfg.get("spike_zscore_threshold", 5.0)),
        )
        
        if not result.is_valid():
            all_valid = False
            all_errors.extend([f"{symbol}: {e}" for e in result.errors])
    
    return all_valid, all_errors

