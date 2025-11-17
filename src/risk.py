# v1.4.0 – 2025-08-22
from __future__ import annotations
from typing import Dict, Tuple, Optional
import pandas as pd
from datetime import datetime, timedelta

def per_symbol_stops(*args, **kwargs) -> Dict[str, Tuple[float, float]]:
    return {}

def _drawdown_pct(ref_equity: float, equity_now: float) -> float:
    if ref_equity is None or ref_equity <= 0 or equity_now is None or equity_now <= 0:
        return 0.0
    return 100.0 * max(0.0, (ref_equity - equity_now) / ref_equity)

def kill_switch_should_trigger(
    day_start_equity: float,
    day_high_equity: float,
    equity_now: float,
    max_daily_loss_pct: float,
    use_trailing: bool = True,
) -> bool:
    if max_daily_loss_pct is None or max_daily_loss_pct <= 0:
        return False
    ref = (day_high_equity if use_trailing else day_start_equity) or 0.0
    dd = _drawdown_pct(ref, equity_now)
    return dd >= float(max_daily_loss_pct)

def resume_time_after_kill(now: pd.Timestamp, minutes: int) -> pd.Timestamp:
    minutes = max(1, int(minutes or 0))
    return now + pd.Timedelta(minutes=minutes)


def check_max_portfolio_drawdown(
    equity_history: Dict[str, float],
    current_equity: float,
    max_drawdown_pct: float,
    window_days: int = 30,
) -> Tuple[bool, float, Optional[float]]:
    """
    Check if portfolio drawdown exceeds maximum threshold.
    
    Args:
        equity_history: Dict mapping ISO timestamps to equity values
        current_equity: Current portfolio equity
        max_drawdown_pct: Maximum allowed drawdown percentage (e.g., 15.0 for 15%)
        window_days: Lookback window in days (default: 30)
    
    Returns:
        Tuple of (should_stop, current_dd_pct, high_watermark)
        - should_stop: True if drawdown exceeds threshold
        - current_dd_pct: Current drawdown percentage
        - high_watermark: Highest equity in window (None if insufficient data)
    """
    if max_drawdown_pct is None or max_drawdown_pct <= 0:
        return False, 0.0, None
    
    if not equity_history or current_equity <= 0:
        return False, 0.0, None
    
    # Find high watermark within window
    cutoff = datetime.utcnow() - timedelta(days=window_days)
    high_watermark = None
    
    for ts_str, equity in equity_history.items():
        try:
            ts = pd.Timestamp(ts_str)
            if ts >= cutoff:
                if high_watermark is None or equity > high_watermark:
                    high_watermark = equity
        except (ValueError, TypeError):
            continue
    
    if high_watermark is None or high_watermark <= 0:
        return False, 0.0, None
    
    # Calculate drawdown from high watermark
    current_dd_pct = _drawdown_pct(high_watermark, current_equity)
    should_stop = current_dd_pct >= float(max_drawdown_pct)
    
    return should_stop, current_dd_pct, high_watermark
