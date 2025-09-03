
# sizing.py — v1.4 (2025-09-02)
# - Explicit ffill + pct_change(fill_method=None) to silence pandas FutureWarning
# - Confidence-weighted (|z|^p) sizing with market-neutral long/short buckets
# - Optional funding tilt, pair-correlation pruning, soft vol targeting
# - Liquidity caps helper for ADV/notional & low-liquidity lists
from __future__ import annotations

import math
from typing import Iterable, Optional, Callable, Dict, Tuple, List
import numpy as np
import pandas as pd

def _pct_change(df: pd.DataFrame) -> pd.DataFrame:
    df = df.ffill()
    return df.pct_change(fill_method=None)

def _zscore(s: pd.Series) -> pd.Series:
    mu = float(s.mean())
    sd = float(s.std())
    if not np.isfinite(sd) or sd < 1e-12:
        sd = 1.0
    z = (s - mu) / sd
    return z.replace([np.inf, -np.inf], 0.0).fillna(0.0)

def _momentum_score(closes: pd.DataFrame,
                    lookbacks: Iterable[int],
                    weights: Iterable[float]) -> pd.Series:
    score = pd.Series(0.0, index=closes.columns, dtype=float)
    for lb, w in zip(lookbacks, weights):
        if lb <= 0:
            continue
        ret = (closes / closes.shift(lb) - 1.0).iloc[-1].astype(float)
        score = score.add(w * ret, fill_value=0.0)
    return score.fillna(0.0)

def _inverse_vol_weights(closes: pd.DataFrame, vol_lookback: int) -> pd.Series:
    rets = _pct_change(closes).iloc[-vol_lookback:]
    vol = rets.std().replace(0, np.nan)
    iv = 1.0 / vol
    iv = iv / iv.sum()
    return iv.fillna(0.0)

def _cap_pair_corr(selected: List[str], rets: pd.DataFrame, max_pair_corr: float, score: pd.Series) -> List[str]:
    if len(selected) <= 1:
        return selected
    keep = list(selected)
    sub = rets[keep].fillna(0.0)
    while True:
        C = sub.corr().fillna(0.0).values
        if C.size == 0:
            break
        np.fill_diagonal(C, 0.0)
        i, j = divmod(np.abs(C).argmax(), C.shape[1])
        if abs(C[i, j]) <= max_pair_corr:
            break
        a, b = keep[i], keep[j]
        drop = a if float(score[a]) <= float(score[b]) else b
        keep.remove(drop)
        if len(keep) <= 1:
            break
        sub = rets[keep].fillna(0.0)
    return keep

def _portfolio_vol(w: pd.Series, rets: pd.DataFrame) -> float:
    if w.abs().sum() == 0:
        return 0.0
    cov = rets.cov()
    v = float(np.dot(w.values, cov.values @ w.values))
    return math.sqrt(max(v, 0.0))


def _kelly_scale(p: float, pf: float, base_frac: float, half_kelly: bool, min_scale: float, max_scale: float) -> float:
    p = max(0.0, min(1.0, float(p)))
    pf = max(0.0, float(pf))
    if pf <= 1.0 or p <= 0.0:
        k = 0.0
    else:
        k = p * (1.0 - 1.0 / pf)  # f* ≈ p - p/PF
        if half_kelly:
            k *= 0.5
    scale = base_frac + k
    return float(np.clip(scale, min_scale, max_scale))

def apply_kelly_scaling(w: pd.Series, sym_stats: Dict[str, dict] | None, cfg) -> pd.Series:
    if sym_stats is None or not getattr(cfg, "enabled", False):
        return w
    scaled = w.copy()
    for s in w.index:
        st = (sym_stats.get(s) or {})
        p = float(st.get("win_rate", st.get("ema_wr", 0.0))) / (100.0 if float(st.get("win_rate", 0.0)) > 1.0 else 1.0)
        pf = float(st.get("pf", st.get("ema_pf", 0.0)))
        scale = _kelly_scale(p, pf, getattr(cfg, "base_frac", 0.5), getattr(cfg, "half_kelly", True), getattr(cfg, "min_scale", 0.5), getattr(cfg, "max_scale", 1.6))
        scaled.loc[s] = float(w.loc[s]) * scale
    g0 = float(w.abs().sum())
    g1 = float(scaled.abs().sum())
    if g0 > 0 and g1 > 0:
        scaled *= g0 / g1
    return scaled


def build_targets(
    closes: pd.DataFrame,
    lookbacks: Iterable[int],
    lookback_weights: Iterable[float],
    vol_lookback: int,
    k_min: int,
    k_max: int,
    market_neutral: bool,
    gross_leverage: float,
    max_weight_per_asset: float,
    dynamic_k_fn: Optional[Callable[[pd.Series, int, int], Tuple[int,int]]] = None,
    funding_tilt: Optional[Dict[str, float]] = None,
    funding_weight: float = 0.0,
    entry_zscore_min: float = 0.0,
    diversify_enabled: bool = False,
    corr_lookback: int = 48,
    max_pair_corr: float = 0.9,
    vol_target_enabled: bool = False,
    target_daily_vol_bps: float = 0.0,
    vol_target_min_scale: float = 0.5,
    vol_target_max_scale: float = 1.2,
    signal_power: float = 1.35,
) -> pd.Series:
    if closes is None or len(closes.columns) == 0:
        return pd.Series(dtype=float)
    closes = closes.copy().ffill().dropna(how="all")

    # 1) Signals
    score = _momentum_score(closes, lookbacks, lookback_weights)
    z = _zscore(score)

    # 2) Choose K
    longK, shortK = (k_min, k_max) if dynamic_k_fn is None else dynamic_k_fn(z, k_min, k_max)

    longs = z[z > 0].sort_values(ascending=False).index.tolist()[: max(0, int(longK))]
    shorts = z[z < 0].sort_values(ascending=True).index.tolist()[: max(0, int(shortK))]

    # 3) Entry threshold (skip small absolute signals)
    if entry_zscore_min and entry_zscore_min > 0:
        longs = [s for s in longs if float(z[s]) >= entry_zscore_min]
        shorts = [s for s in shorts if float(z[s]) <= -entry_zscore_min]

    # 4) Base bucket weights (confidence-weighted)
    def bucket_weights(names: List[str], sign: float) -> pd.Series:
        if not names:
            return pd.Series(0.0, index=closes.columns)
        strength = pd.Series({n: abs(float(z[n])) ** float(signal_power) for n in names})
        # inverse vol weights as risk-parity anchor
        iv = _inverse_vol_weights(closes[names], vol_lookback)
        raw = strength * iv
        total = float(raw.sum())
        if total <= 0:
            w = pd.Series(0.0, index=closes.columns)
        else:
            w = pd.Series(0.0, index=closes.columns)
            w.loc[names] = sign * (raw / total)
        return w

    w_long = bucket_weights(longs, +1.0)
    w_short = bucket_weights(shorts, -1.0)

    w = w_long.add(w_short, fill_value=0.0)

    # 5) Diversify via pair-corr cap
    if diversify_enabled and max_pair_corr < 0.999:
        rets = _pct_change(closes).iloc[-int(corr_lookback):].fillna(0.0)
        if longs:
            long_sel = _cap_pair_corr(longs, rets, max_pair_corr, z)
            w = w.add(bucket_weights(long_sel, +1.0), fill_value=0.0)
        if shorts:
            short_sel = _cap_pair_corr(shorts, rets, max_pair_corr, z)
            w = w.add(bucket_weights(short_sel, -1.0), fill_value=0.0)

    # 6) Funding tilt (reduce exposure against dear funding)
    if funding_tilt and funding_weight != 0.0:
        f = (pd.Series(funding_tilt)
            .reindex(w.index)
            .fillna(0.0)
            .infer_objects(copy=False)
            .astype(float))
        # positive funding -> longs pay -> down-weight longs; negative -> shorts pay -> down-weight shorts
        adj = pd.Series(1.0, index=w.index, dtype=float)
        adj.loc[w > 0] = 1.0 - funding_weight * (f.clip(lower=0.0) / 10000.0)
        adj.loc[w < 0] = 1.0 - funding_weight * ((-f).clip(lower=0.0) / 10000.0)
        w *= adj.fillna(1.0)

    # 7) Normalize to gross leverage and apply per-asset cap
    if market_neutral:
        long_sum = float(w[w > 0].sum())
        short_sum = float(-w[w < 0].sum())
        if long_sum > 0:
            w[w > 0] *= (0.5 * float(gross_leverage)) / long_sum
        if short_sum > 0:
            w[w < 0] *= (0.5 * float(gross_leverage)) / short_sum
    else:
        tot = float(w.abs().sum())
        if tot > 0:
            w *= float(gross_leverage) / tot

    max_w = float(max_weight_per_asset or 0.0)
    if max_w > 0:
        w = w.clip(lower=-max_w, upper=max_w)

    # 8) Vol targeting (soft scale within bounds)
    if vol_target_enabled and target_daily_vol_bps > 0:
        rets = _pct_change(closes).iloc[-int(max(vol_lookback, corr_lookback)):].fillna(0.0)
        pv = _portfolio_vol(w.fillna(0.0), rets)  # daily (since closes are hourly we slightly over-estimate; acceptable)
        if pv > 1e-9:
            target = float(target_daily_vol_bps) / 10000.0
            scale = float(target / pv)
            scale = float(np.clip(scale, float(vol_target_min_scale), float(vol_target_max_scale)))
            w *= scale

    return w.reindex(closes.columns).fillna(0.0)

def apply_liquidity_caps(
    targets: pd.Series,
    *,
    equity_usdt: float,
    tickers: Dict[str, dict],
    adv_cap_pct: float,
    notional_cap_usdt: float,
) -> pd.Series:
    """
    Cap each symbol's target notional by ADV and absolute notional caps, then rescale.
    - ADV proxy from tickers: use 24h quoteVolume (USDT). If missing, leave unchanged.
    """
    if targets is None or len(targets) == 0:
        return targets

    w = targets.copy().fillna(0.0)
    eq = float(equity_usdt or 0.0)
    if eq <= 0:
        return w

    adv_pct = float(adv_cap_pct or 0.0)
    abs_cap = float(notional_cap_usdt or 0.0)

    notionals = (w.abs() * eq).to_dict()
    new_w = w.copy()
    for s, wt in w.items():
        info = tickers.get(s, {}) or {}
        adv = float(info.get("quoteVolume") or 0.0)
        cap_adv = adv_pct * adv if adv > 0 and adv_pct > 0 else float('inf')
        cap_abs = abs_cap if abs_cap > 0 else float('inf')
        cap = min(cap_adv, cap_abs)
        if cap < float('inf'):
            desired = abs(wt) * eq
            if desired > cap and desired > 0:
                scale = cap / desired
                new_w.loc[s] = math.copysign(abs(wt) * scale, wt)

    # Renormalize to preserve gross leverage
    gross = float(new_w.abs().sum())
    if gross > 0:
        new_w *= float(w.abs().sum()) / gross
    return new_w.fillna(0.0)