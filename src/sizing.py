import logging
import pandas as pd
import numpy as np
from typing import Dict, Optional

from .signals import momentum_score, inverse_vol_weights

log = logging.getLogger("sizing")

def _percentile_tilt(series: pd.Series) -> pd.Series:
    """Map values to [-1, +1] by percentile.
    Higher values -> closer to +1. We’ll invert for funding costs in caller.
    """
    if series.empty:
        return series
    r = series.rank(pct=True)  # [0,1]
    return (r - 0.5) * 2.0

def build_targets(
    prices: pd.DataFrame,
    lookbacks,
    weights,
    vol_lookback: int,
    k_min: int,
    k_max: int,
    market_neutral: bool,
    gross_leverage: float,
    max_weight_per_asset: float,
    dynamic_k_fn,
    # NEW: optional funding-rate tilt (pass dict symbol->fundingRate)
    funding_tilt: Optional[Dict[str, float]] = None,
    funding_weight: float = 0.0,
) -> pd.Series:
    assert prices.shape[0] > max(max(lookbacks), vol_lookback) + 5, "Not enough bars"
    last_prices = prices.iloc[-1]
    valid_cols = last_prices[last_prices.notna() & (last_prices > 0)].index
    prices = prices[valid_cols]

    score = momentum_score(prices, lookbacks, weights)

    # === Funding tilt (optional) ============================================
    # Funding rates: positive = expensive to be long. We *penalize* positive
    # funding and reward negative (cheaper) funding.
    if funding_tilt and abs(funding_weight) > 1e-12:
        f = pd.Series({k: float(v) for k, v in (funding_tilt or {}).items()}, dtype="float64")
        f = f.reindex(prices.columns)
        f = f.fillna(f.median())  # be conservative on missing
        # Convert to percentile tilt in [-1, +1], then invert so expensive funding -> negative
        tilt = -_percentile_tilt(f)
        score = score.add(funding_weight * tilt, fill_value=0.0)

    iv = inverse_vol_weights(prices, vol_lookback)

    topk, bottomk = dynamic_k_fn(score, k_min, k_max)
    ranked = score.sort_values(ascending=False)
    longs = ranked.index[:topk]
    shorts = ranked.index[-bottomk:]

    w = pd.Series(0.0, index=prices.columns)
    if market_neutral:
        long_raw = (score.loc[longs].clip(lower=0.0)) * iv.loc[longs]
        short_raw = (-score.loc[shorts].clip(upper=0.0)) * iv.loc[shorts]
        long_w = long_raw / long_raw.sum() if long_raw.sum() > 0 else pd.Series(0.0, index=longs)
        short_w = short_raw / short_raw.sum() if short_raw.sum() > 0 else pd.Series(0.0, index=shorts)
        long_w = (gross_leverage / 2.0) * long_w
        short_w = -(gross_leverage / 2.0) * short_w
        w.loc[long_w.index] = long_w
        w.loc[short_w.index] = short_w
    else:
        pos_raw = (score.clip(lower=0.0)) * iv
        pos_w = pos_raw / pos_raw.sum() if pos_raw.sum() > 0 else pd.Series(0.0, index=prices.columns)
        w = gross_leverage * pos_w

    # Cap per-asset
    w = w.apply(lambda x: x if abs(x) <= max_weight_per_asset else np.sign(x) * max_weight_per_asset)

    # Re-normalize to match gross leverage when market-neutral
    if market_neutral:
        gross = w.abs().sum()
        if gross > 0:
            w = w * (gross_leverage / gross)

    w = w[w.abs() > 1e-9]
    return w.round(6)

def apply_liquidity_caps(
    targets: pd.Series,
    equity_usdt: float,
    tickers: Dict[str, dict],
    adv_cap_pct: float,
    notional_cap_usdt: float,
) -> pd.Series:
    capped = targets.copy()
    for s in targets.index:
        qv = 0.0
        try:
            qv = float(tickers.get(s, {}).get("quoteVolume") or 0.0)
        except Exception:
            qv = 0.0
        cap_by_adv = adv_cap_pct * qv
        cap_abs = notional_cap_usdt
        notional = abs(targets[s]) * equity_usdt
        max_notional = max(0.0, min(cap_by_adv if cap_by_adv > 0 else float("inf"), cap_abs))
        if max_notional > 0 and notional > max_notional:
            new_weight = max_notional / equity_usdt
            capped[s] = new_weight * np.sign(targets[s])
    return capped
