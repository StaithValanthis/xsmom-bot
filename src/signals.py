# signals.py — v1.3 (2025-09-04)
# Added:
# - helper to compute volatility "tier" for adaptive risk if needed externally
# - (no breaking changes)
from __future__ import annotations
import logging
from typing import List, Tuple, Literal, Optional
import numpy as np
import pandas as pd

log = logging.getLogger("signals")

def compute_atr(df: pd.DataFrame, n: int = 14, method: Literal["sma","rma"]="sma") -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low).abs(),
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)
    if method == "rma":
        return tr.ewm(alpha=1/n, adjust=False).mean()
    return tr.rolling(n).mean()

def regime_ok(close: pd.Series, ema_len: int, slope_min_bps_per_day: float, *, use_abs: bool=False) -> bool:
    ema = close.ewm(span=int(ema_len), adjust=False).mean()
    slope = (ema.diff().rolling(int(ema_len//4 or 1)).mean()).iloc[-1]
    # approximate bps/day using last bar change vs price
    price = float(close.iloc[-1])
    if price <= 0 or pd.isna(slope):
        return True
    bps_per_bar = (slope / price) * 10_000.0
    bps_per_day = bps_per_bar * 24.0  # assume hourly bars by default
    return abs(bps_per_day) >= float(slope_min_bps_per_day) if use_abs else (bps_per_day >= float(slope_min_bps_per_day))

def dynamic_k(zscores: pd.Series, k_min: int, k_max: int) -> Tuple[int, int]:
    """Example: widen K when strong dispersion; tighten otherwise."""
    dispersion = float(zscores.abs().median())
    if dispersion >= 1.0:
        return (k_min, k_max)
    if dispersion >= 0.6:
        return (k_min, max(k_min, (k_min + k_max)//2))
    return (k_min, max(k_min, k_min+2))

def regime_ok_with_reason(close: pd.Series, ema_len: int, slope_min_bps_per_day: float, use_abs: bool=False):
    try:
        ok = regime_ok(close, ema_len, slope_min_bps_per_day, use_abs=use_abs)
        return (ok, None) if ok else (False, f"EMA{ema_len} slope below {slope_min_bps_per_day} bps/day")
    except Exception as e:
        return True, None

def volatility_tier(atr_pct: float, low_thr_bps: float=40.0, high_thr_bps: float=120.0) -> str:
    bps = float(atr_pct) * 10_000.0
    if bps < low_thr_bps:
        return "low"
    if bps > high_thr_bps:
        return "high"
    return "mid"
