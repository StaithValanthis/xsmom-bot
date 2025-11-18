"""
Walk-forward optimization (WFO) pipeline.

Implements purged walk-forward with embargo to reduce overfitting.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, Any, List, Tuple, Optional, Callable
from datetime import datetime, timedelta
import pandas as pd
import numpy as np

log = logging.getLogger("optimizer.walk_forward")


@dataclass
class WFOSegment:
    """A single walk-forward segment."""
    segment_id: int
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    oos_start: pd.Timestamp
    oos_end: pd.Timestamp
    train_bars: Dict[str, pd.DataFrame]
    oos_bars: Dict[str, pd.DataFrame]
    symbols: List[str]


@dataclass
class WFOConfig:
    """Walk-forward optimization configuration."""
    train_days: int = 120  # Training window size (days)
    oos_days: int = 30  # Out-of-sample window size (days)
    embargo_days: int = 2  # Embargo between train and OOS (days)
    min_train_days: int = 60  # Minimum training window
    min_oos_days: int = 7  # Minimum OOS window
    timeframe_hours: float = 1.0  # Bar timeframe in hours
    
    def __post_init__(self):
        """Validate configuration."""
        assert self.train_days > 0
        assert self.oos_days > 0
        assert self.embargo_days >= 0
        assert self.min_train_days <= self.train_days
        assert self.min_oos_days <= self.oos_days


def generate_wfo_segments(
    bars: Dict[str, pd.DataFrame],
    cfg: WFOConfig,
    start_date: Optional[pd.Timestamp] = None,
    end_date: Optional[pd.Timestamp] = None,
) -> List[WFOSegment]:
    """
    Generate walk-forward segments from historical data.
    
    Args:
        bars: Dict of symbol -> OHLCV DataFrame
        cfg: WFO configuration
        start_date: Optional start date (defaults to earliest data)
        end_date: Optional end date (defaults to latest data)
    
    Returns:
        List of WFOSegment objects
    """
    if not bars:
        return []
    
    # Find common date range
    # Use flexible timestamp matching: require timestamps to be present in at least 80% of symbols
    # This is more robust than strict intersection when symbols have different trading hours
    all_indices = []
    for df in bars.values():
        if len(df) > 0:
            all_indices.append(df.index)
    
    if not all_indices:
        log.warning("No valid data in bars")
        return []
    
    # Get union of all timestamps
    all_timestamps = set()
    for idx in all_indices:
        all_timestamps.update(idx)
    
    # Count how many symbols have each timestamp
    timestamp_counts = {}
    for ts in all_timestamps:
        count = sum(1 for idx in all_indices if ts in idx)
        timestamp_counts[ts] = count
    
    # Require timestamps to be present in at least 80% of symbols
    min_symbols = max(1, int(len(all_indices) * 0.8))
    flexible_common = [ts for ts, count in timestamp_counts.items() if count >= min_symbols]
    
    if len(flexible_common) == 0:
        log.warning(f"No timestamps found in at least {min_symbols} symbols (80% of {len(all_indices)} symbols)")
        return []
    
    common_index = pd.Index(flexible_common).sort_values()
    
    if start_date is not None:
        common_index = common_index[common_index >= start_date]
    if end_date is not None:
        common_index = common_index[common_index <= end_date]
    
    if len(common_index) == 0:
        log.warning("No data in specified date range")
        return []
    
    # Calculate window sizes in bars
    train_hours = cfg.train_days * 24
    oos_hours = cfg.oos_days * 24
    embargo_hours = cfg.embargo_days * 24
    
    train_bars = int(train_hours / cfg.timeframe_hours)
    oos_bars = int(oos_hours / cfg.timeframe_hours)
    embargo_bars = int(embargo_hours / cfg.timeframe_hours)
    
    min_train_bars = int(cfg.min_train_days * 24 / cfg.timeframe_hours)
    min_oos_bars = int(cfg.min_oos_days * 24 / cfg.timeframe_hours)
    
    segments: List[WFOSegment] = []
    segment_id = 0
    
    # Walk forward: sliding windows
    i = 0
    while i < len(common_index) - min_train_bars - embargo_bars - min_oos_bars:
        train_start_idx = i
        train_end_idx = min(i + train_bars, len(common_index))
        
        if train_end_idx - train_start_idx < min_train_bars:
            break
        
        train_start = common_index[train_start_idx]
        train_end = common_index[train_end_idx - 1]
        
        # OOS starts after embargo
        oos_start_idx = train_end_idx + embargo_bars
        if oos_start_idx >= len(common_index):
            break
        
        oos_end_idx = min(oos_start_idx + oos_bars, len(common_index))
        if oos_end_idx - oos_start_idx < min_oos_bars:
            break
        
        oos_start = common_index[oos_start_idx]
        oos_end = common_index[oos_end_idx - 1]
        
        # Extract bars for this segment
        train_seg_bars = {}
        oos_seg_bars = {}
        symbols = list(bars.keys())
        
        for sym in symbols:
            df = bars[sym]
            train_mask = (df.index >= train_start) & (df.index <= train_end)
            oos_mask = (df.index >= oos_start) & (df.index <= oos_end)
            
            train_df = df[train_mask].copy()
            oos_df = df[oos_mask].copy()
            
            # Remove duplicate timestamps (keep first occurrence)
            if train_df.index.duplicated().any():
                train_df = train_df[~train_df.index.duplicated(keep='first')]
            if oos_df.index.duplicated().any():
                oos_df = oos_df[~oos_df.index.duplicated(keep='first')]
            
            if len(train_df) >= min_train_bars and len(oos_df) >= min_oos_bars:
                train_seg_bars[sym] = train_df
                oos_seg_bars[sym] = oos_df
        
        if train_seg_bars and oos_seg_bars:
            segment = WFOSegment(
                segment_id=segment_id,
                train_start=train_start,
                train_end=train_end,
                oos_start=oos_start,
                oos_end=oos_end,
                train_bars=train_seg_bars,
                oos_bars=oos_seg_bars,
                symbols=list(train_seg_bars.keys()),
            )
            segments.append(segment)
            segment_id += 1
            
            # Move forward by OOS window size
            i = oos_end_idx
        else:
            # If no valid segment, move forward by 1 bar
            i += 1
    
    log.info(f"Generated {len(segments)} WFO segments")
    return segments


def evaluate_on_segments(
    segments: List[WFOSegment],
    eval_fn: Callable[[Dict[str, pd.DataFrame], List[str]], Dict[str, float]],
    param_set: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """
    Evaluate a parameter set (or function) on all WFO segments.
    
    Args:
        segments: List of WFO segments
        eval_fn: Function that takes (bars_dict, symbols) -> metrics_dict
        param_set: Optional parameter set (passed to eval_fn if it accepts it)
    
    Returns:
        List of segment results, each with metrics and segment info
    """
    results = []
    
    for seg in segments:
        try:
            # Call eval_fn with train data
            if param_set is not None:
                # Try calling with param_set if eval_fn accepts it
                import inspect
                sig = inspect.signature(eval_fn)
                if 'param_set' in sig.parameters or 'params' in sig.parameters:
                    metrics = eval_fn(seg.train_bars, seg.symbols, param_set=param_set)
                else:
                    metrics = eval_fn(seg.train_bars, seg.symbols)
            else:
                metrics = eval_fn(seg.train_bars, seg.symbols)
            
            # Also evaluate on OOS
            oos_metrics = eval_fn(seg.oos_bars, seg.symbols)
            
            result = {
                "segment_id": seg.segment_id,
                "train_start": seg.train_start.isoformat(),
                "train_end": seg.train_end.isoformat(),
                "oos_start": seg.oos_start.isoformat(),
                "oos_end": seg.oos_end.isoformat(),
                "train_metrics": metrics,
                "oos_metrics": oos_metrics,
                "symbols_count": len(seg.symbols),
            }
            results.append(result)
        except Exception as e:
            log.error(f"Failed to evaluate segment {seg.segment_id}: {e}")
            continue
    
    return results


def aggregate_segment_results(
    segment_results: List[Dict[str, Any]],
    metric_keys: List[str],
) -> Dict[str, float]:
    """
    Aggregate metrics across all segments.
    
    Args:
        segment_results: List of segment result dicts
        metric_keys: List of metric keys to aggregate
    
    Returns:
        Dict of aggregated metrics (mean, std, min, max for each)
    """
    if not segment_results:
        return {}
    
    aggregated = {}
    
    for key in metric_keys:
        train_vals = [r["train_metrics"].get(key, np.nan) for r in segment_results]
        oos_vals = [r["oos_metrics"].get(key, np.nan) for r in segment_results]
        
        train_vals = [v for v in train_vals if not np.isnan(v)]
        oos_vals = [v for v in oos_vals if not np.isnan(v)]
        
        if train_vals:
            aggregated[f"train_{key}_mean"] = float(np.mean(train_vals))
            aggregated[f"train_{key}_std"] = float(np.std(train_vals))
            aggregated[f"train_{key}_min"] = float(np.min(train_vals))
            aggregated[f"train_{key}_max"] = float(np.max(train_vals))
        
        if oos_vals:
            aggregated[f"oos_{key}_mean"] = float(np.mean(oos_vals))
            aggregated[f"oos_{key}_std"] = float(np.std(oos_vals))
            aggregated[f"oos_{key}_min"] = float(np.min(oos_vals))
            aggregated[f"oos_{key}_max"] = float(np.max(oos_vals))
    
    # Add stability metrics
    if "sharpe" in metric_keys:
        oos_sharpes = [
            r["oos_metrics"].get("sharpe", np.nan) for r in segment_results
        ]
        oos_sharpes = [s for s in oos_sharpes if not np.isnan(s)]
        if len(oos_sharpes) > 1:
            aggregated["oos_sharpe_stability"] = 1.0 - (np.std(oos_sharpes) / (abs(np.mean(oos_sharpes)) + 1e-6))
            aggregated["oos_sharpe_consistency"] = float(np.sum(np.array(oos_sharpes) > 0) / len(oos_sharpes))
    
    # Aggregate OOS sample size metadata (if present)
    oos_sample_sizes = [r.get("oos_sample_size", {}) for r in segment_results]
    if oos_sample_sizes and any(s for s in oos_sample_sizes):
        oos_bars = [s.get("bars", 0) for s in oos_sample_sizes if s]
        oos_days = [s.get("days", 0.0) for s in oos_sample_sizes if s]
        oos_trades = [s.get("trades", 0) for s in oos_sample_sizes if s]
        
        if oos_bars:
            aggregated["oos_sample_bars_mean"] = float(np.mean(oos_bars))
            aggregated["oos_sample_bars_min"] = float(np.min(oos_bars))
            aggregated["oos_sample_bars_max"] = float(np.max(oos_bars))
        
        if oos_days:
            aggregated["oos_sample_days_mean"] = float(np.mean(oos_days))
            aggregated["oos_sample_days_min"] = float(np.min(oos_days))
            aggregated["oos_sample_days_max"] = float(np.max(oos_days))
        
        if oos_trades:
            aggregated["oos_sample_trades_mean"] = float(np.mean(oos_trades))
            aggregated["oos_sample_trades_min"] = float(np.min(oos_trades))
            aggregated["oos_sample_trades_max"] = float(np.max(oos_trades))
    
    return aggregated

