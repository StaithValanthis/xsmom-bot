# v1.2.0 – 2025-08-21
from __future__ import annotations
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Tuple, Optional

import pandas as pd
import numpy as np

from .signals import compute_atr

log = logging.getLogger("risk")

def per_symbol_stops(
    df: pd.DataFrame,
    atr_mult_sl: float,
    atr_mult_tp: float,
    use_tp: bool,
    atr_len: int = 14,
) -> Tuple[float,float,Optional[float],Optional[float]]:
    atr = compute_atr(df, atr_len, method="rma")
    close = float(df["close"].iloc[-1])
    last_atr = float(atr.iloc[-1])
    sl_long = close - atr_mult_sl * last_atr
    sl_short = close + atr_mult_sl * last_atr
    tp_long = close + atr_mult_tp * last_atr if use_tp else None
    tp_short = close - atr_mult_tp * last_atr if use_tp else None
    return sl_long, sl_short, tp_long, tp_short

def check_soft_stop(latest_row, side: str, stop_price: float) -> bool:
    return (latest_row["low"] <= stop_price) if side == "long" else (latest_row["high"] >= stop_price)

def kill_switch_should_trigger(day_start_equity: float, current_equity: float, max_daily_loss_pct: float) -> bool:
    if day_start_equity <= 0:
        return False
    loss_pct = 100.0 * max(0.0, (day_start_equity - current_equity) / day_start_equity)
    return loss_pct >= max_daily_loss_pct

def resume_time_after_kill(now: datetime, minutes: int) -> datetime:
    return now + timedelta(minutes=minutes)
