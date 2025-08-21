# v1.2.0 – 2025-08-21
from __future__ import annotations
import logging
import numpy as np
import pandas as pd
from typing import Dict, Optional, List, Tuple

from .signals import momentum_score, inverse_vol_weights

log = logging.getLogger("sizing")

def _zscore(s: pd.Series) -> pd.Series:
    m = s.mean()
    v = s.std()
    if not np.isfinite(v) or v <= 0:
        return pd.Series(0.0, index=s.index)
    return (s - m) / v

def _pick_diversified(
    ranked: List[str],
    rets: pd.DataFrame,
    k: int,
    max_pair_corr: float,
) -> List[str]:
    chosen: List[str] = []
    if k <= 0 or len(ranked) == 0 or rets.shape[0] < 2:
        return chosen
    corr = rets.corr().clip(-1.0, 1.0).fillna(0.0)
    for s in ranked:
        if len(chosen) >= k:
            break
        ok = True
        for c in chosen:
            if abs(float(corr.loc[s, c])) > max_pair_corr:
                ok = False
                break
        if ok:
            chosen.append(s)
    return chosen

def build_targets(
    window: pd.DataFrame,
    lookbacks: List[int],
    weights: List[float],
    vol_lookback: int,
    k_min: int,
    k_max: int,
    market_neutral: bool,
    gross_leverage: float,
    max_weight_per_asset: float,
    *,
    dynamic_k_fn,
    funding_tilt: Optional[Dict[str, float]] = None,
    funding_weight: float = 0.0,
    entry_zscore_min: float = 0.0,
    diversify_enabled: bool = False,
    corr_lookback: int = 48,
    max_pair_corr: float = 0.9,
    vol_target_enabled: bool = False,
    target_daily_vol_bps: float = 0.0,
    vol_target_min_scale: float = 0.5,
    vol_target_max_scale: float = 2.0,
) -> pd.Series:
    """
    Returns target weights (sum(abs)=gross_leverage if market_neutral).
    """
    closes = window["close"].unstack(0) if isinstance(window.columns, pd.MultiIndex) else window
    prices = closes.dropna(how="all", axis=1).copy()
    prices = prices.ffill().bfill()

    # 1) Momentum score
    score = momentum_score(prices, lookbacks, weights).fillna(0.0)

    # 2) Compute dynamic K and rank
    kL, kS = dynamic_k_fn(score, k_min, k_max)
    longs = score.sort_values(ascending=False).index.tolist()
    shorts = score.sort_values(ascending=True).index.tolist()

    # 3) Entry quality gate via z-score
    z = _zscore(score)
    longs = [s for s in longs if float(z.loc[s]) >= entry_zscore_min]
    shorts = [s for s in shorts if float(-z.loc[s]) >= entry_zscore_min]

    # 4) Diversification gating (optional)
    if diversify_enabled:
        recent = prices.pct_change().iloc[-corr_lookback:].dropna(how="all", axis=1)
        longs = _pick_diversified(longs, recent, kL, max_pair_corr)
        shorts = _pick_diversified(shorts, recent, kS, max_pair_corr)
    else:
        longs = longs[:kL]
        shorts = shorts[:kS]

    # 5) Inverse-vol scaling (per-side)
    iv = inverse_vol_weights(prices, vol_lookback)

    wl = (iv.reindex(longs).fillna(0.0))
    ws = (iv.reindex(shorts).fillna(0.0))
    if wl.sum() > 0:
        wl = wl / wl.sum()
    if ws.sum() > 0:
        ws = ws / ws.sum()

    raw_long = (+wl)
    raw_short = (-ws)

    w = pd.concat([raw_long, raw_short], axis=0).reindex(prices.columns).fillna(0.0)

    # 6) Funding tilt (soft preference)
    if funding_tilt and abs(funding_weight) > 0:
        tilt = pd.Series({s: float(funding_tilt.get(s, 0.0)) for s in w.index})
        tilt = (tilt - tilt.median())  # center
        tilt = tilt / (tilt.abs().max() or 1.0)
        w = w + funding_weight * tilt
        # keep direction for selected side
        w = w.where(w * pd.concat([raw_long, raw_short], axis=0).reindex(w.index).fillna(0.0) >= 0, 0.0)

    # 7) Cap per-asset weight
    w = w.clip(lower=-max_weight_per_asset, upper=+max_weight_per_asset)

    # 8) Re-scale to desired gross leverage if market-neutral
    if market_neutral:
        gross = w.abs().sum()
        if gross > 0:
            w = w * (gross_leverage / gross)

    # 9) Optional volatility targeting at portfolio level
    if vol_target_enabled and target_daily_vol_bps > 0:
        try:
            rets = prices.pct_change().dropna().iloc[-vol_lookback:]
            port_rets = (rets * w).sum(axis=1)
            port_vol_daily = float(port_rets.std()) * np.sqrt(24)  # 1h bars ⇒ ~24 per day
            target = float(target_daily_vol_bps) / 10_000.0
            if port_vol_daily > 1e-12 and target > 0:
                scale = target / port_vol_daily
                scale = float(np.clip(scale, vol_target_min_scale, vol_target_max_scale))
                if np.isfinite(scale) and scale > 0:
                    w = w * scale
        except Exception as e:
            log.debug(f"vol_targeting failed: {e}")

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
