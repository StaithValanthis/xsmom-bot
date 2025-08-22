# v1.4.0 – 2025-08-22
from __future__ import annotations
from typing import Dict, Tuple
import pandas as pd

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
