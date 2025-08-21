# v1.1.0 – 2025-08-21
import logging
from typing import List, Tuple, Literal
import numpy as np
import pandas as pd

log = logging.getLogger("signals")

def compute_atr(df: pd.DataFrame, n: int = 14, method: Literal["sma","rma"]="sma") -> pd.Series:
    """
    ATR using either SMA (classic Wilder-like) or RMA (Wilder's smoothing).
    """
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low).abs(),
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)

    if method == "rma":
        # Wilder's smoothing (RMA)
        rma = tr.ewm(alpha=1.0/n, adjust=False).mean()
        return rma
    else:
        # Simple moving average of TR
        return tr.rolling(n).mean()

def momentum_score(prices: pd.DataFrame, lookbacks: List[int], weights: List[float]) -> pd.Series:
    score = pd.Series(0.0, index=prices.columns)
    for lb, w in zip(lookbacks, weights):
        lb_ret = (prices / prices.shift(lb) - 1.0).iloc[-1]
        score = score.add(w * lb_ret, fill_value=0.0)
    return score

def inverse_vol_weights(prices: pd.DataFrame, vol_lookback: int) -> pd.Series:
    rets = prices.pct_change().iloc[-vol_lookback:]
    vol = rets.std()
    iv = 1.0 / vol.replace(0, np.nan)
    iv = iv / iv.sum()
    return iv.replace(np.nan, 0.0)

def dispersion(series: pd.Series) -> float:
    return float(series.std())

def dynamic_k(score: pd.Series, k_min: int, k_max: int) -> Tuple[int,int]:
    d = dispersion(score.fillna(0.0))
    if d < 0.01:
        k = k_min
    elif d > 0.05:
        k = k_max
    else:
        k = int(k_min + (k_max - k_min) * (d - 0.01) / 0.04)
    k = max(k_min, min(k, k_max))
    return k, k

def regime_ok(
    close: pd.Series,
    ema_len: int,
    slope_min_bps_per_day: float,
    use_abs: bool = False
) -> bool:
    """
    True if regime is 'tradable' by EMA slope.
      - If use_abs=False: allow when slope >= threshold (directional up-trend gating).
      - If use_abs=True: allow when |slope| >= threshold (trend/noise gating).
    """
    ema = close.ewm(span=ema_len, adjust=False).mean()
    slope = ema.diff().tail(ema_len).mean()
    slope_bps = 10_000 * slope / close.iloc[-1]
    if use_abs:
        return abs(float(slope_bps)) >= slope_min_bps_per_day
    return float(slope_bps) >= slope_min_bps_per_day
