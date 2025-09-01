# -*- coding: utf-8 -*-
"""
sizing.py.next — drop-in replacement for build_targets with confidence-weighted sizing.
Implements `signal_power` (zscore^p inside-bucket weighting), entry z-score filter,
optional funding tilt, diversify (pair-corr cap), and soft vol targeting.

Signature kept compatible with live.py call:
    build_targets(
        closes, lookbacks, lookback_weights, vol_lookback,
        k_min, k_max, market_neutral, gross_leverage, max_weight_per_asset,
        dynamic_k_fn=None,
        funding_tilt=None, funding_weight=0.0,
        entry_zscore_min=0.0,
        diversify_enabled=False, corr_lookback=48, max_pair_corr=0.9,
        vol_target_enabled=False, target_daily_vol_bps=0.0,
        vol_target_min_scale=0.5, vol_target_max_scale=1.2,
        signal_power=1.35,
    )
"""
from __future__ import annotations

import math
from typing import Iterable, Optional, Callable, Dict
import numpy as np
import pandas as pd

def _pct_change(df: pd.DataFrame) -> pd.DataFrame:
    return df.pct_change()

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

def _cap_pair_corr(selected: list[str], rets: pd.DataFrame, max_pair_corr: float, score: pd.Series) -> list[str]:
    """
    Greedy prune to satisfy pairwise |corr| <= max_pair_corr.
    Drops the lowest-score asset among any violating pair until satisfied.
    """
    if len(selected) <= 1:
        return selected
    keep = list(selected)
    sub = rets[keep].fillna(0.0)
    # compute once; update lazily
    while True:
        C = sub.corr().fillna(0.0).values
        np.fill_diagonal(C, 0.0)
        i, j = divmod(np.abs(C).argmax(), C.shape[1])
        if C.size == 0 or abs(C[i, j]) <= max_pair_corr:
            break
        # drop the worse-scored of the pair
        a, b = keep[i], keep[j]
        drop = a if score[a] <= score[b] else b
        keep.remove(drop)
        sub = rets[keep].fillna(0.0)
        if len(keep) <= 1:
            break
    return keep

def _portfolio_vol(w: pd.Series, rets: pd.DataFrame) -> float:
    if w.abs().sum() == 0:
        return 0.0
    cov = rets.cov()
    v = float(np.dot(w.values, cov.values @ w.values))
    return math.sqrt(max(v, 0.0))

def apply_liquidity_caps(targets, cfg=None, *_, **__):
    """
    Simple per-asset cap based on a config:
      strategy.liquidity_caps.enabled: bool
      strategy.liquidity_caps.max_weight_low_liq: float (e.g., 0.02)
      strategy.liquidity_caps.symbols_low_liq: list[str]
    If cfg or fields are missing, this is a no-op.
    """
    try:
        if not hasattr(cfg, "strategy"):
            return targets.fillna(0.0)
        lc = getattr(cfg.strategy, "liquidity_caps", None)
        if not lc or not getattr(lc, "enabled", False):
            return targets.fillna(0.0)

        max_w = float(getattr(lc, "max_weight_low_liq", 0.0) or 0.0)
        low_syms = set(getattr(lc, "symbols_low_liq", []) or [])
        out = targets.copy().fillna(0.0)
        if max_w > 0 and low_syms:
            # clip only the designated low-liquidity symbols
            out.loc[out.index.intersection(low_syms)] = out.loc[out.index.intersection(low_syms)].clip(-max_w, max_w)
        return out
    except Exception:
        return targets.fillna(0.0)


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
    dynamic_k_fn: Optional[Callable[[pd.Series], int]] = None,
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
    """
    Returns a vector of target weights indexed by symbol.
    """
    closes = closes.copy()
    cols = list(closes.columns)
    if len(cols) == 0:
        return pd.Series(dtype=float)

    # 1) score & (optional) funding tilt to selection score
    score = _momentum_score(closes, lookbacks, lookback_weights)

    if funding_tilt is not None and funding_weight:
        # funding_tilt: dict {sym: signed tilt}, higher tilt favours long, lower favours short
        tilt = pd.Series(funding_tilt).reindex(cols)
        tilt = pd.to_numeric(tilt, errors="coerce").fillna(0.0)  # avoid FutureWarning on fillna downcast
        score = score + float(funding_weight) * tilt

    # entry floor by zscore
    z_all = _zscore(score)
    eligible = z_all.index[(z_all.abs() >= float(entry_zscore_min))]

    # 2) choose K
    if callable(dynamic_k_fn):
        try:
            # preferred: dynamic_k(score, k_min, k_max)
            K = int(dynamic_k_fn(score, k_min, k_max))
        except TypeError:
            try:
                # fallback: dynamic_k(score)
                K = int(dynamic_k_fn(score))
            except TypeError:
                # last resort: midpoint
                K = int(round(0.5 * (k_min + k_max)))
    else:
        K = int(round(0.5 * (k_min + k_max)))
    K = int(max(k_min, min(k_max, K)))
    kL = kS = max(0, K)

    # Rank on *raw* score for direction
    ranks_desc = score.sort_values(ascending=False).index.tolist()
    longs = [s for s in ranks_desc if s in eligible][:kL]
    shorts = [s for s in reversed(ranks_desc) if s in eligible][:kS]

    # 3) diversify: cap pairwise correlation
    if diversify_enabled:
        rets_corr = _pct_change(closes).iloc[-int(corr_lookback):].dropna(how="all")
        longs  = _cap_pair_corr(longs,  rets_corr, max_pair_corr, score)
        shorts = _cap_pair_corr(shorts, rets_corr, max_pair_corr, score)

    # 4) inside-bucket weights: inverse-vol * (zscore^signal_power)
    iv = _inverse_vol_weights(closes, int(vol_lookback)).reindex(cols).fillna(0.0)
    z = z_all.copy()

    w = pd.Series(0.0, index=cols, dtype=float)
    if longs:
        sigL = (z.loc[longs].clip(lower=0.0)) ** float(signal_power)
        w.loc[longs] = (iv.loc[longs] * sigL)
    if shorts:
        sigS = (-z.loc[shorts].clip(upper=0.0)).abs() ** float(signal_power)
        w.loc[shorts] = -(iv.loc[shorts] * sigS)

    # 5) normalize each side to 0.5 gross (market neutral book)
    gL = float(w.loc[longs].abs().sum()) if longs else 0.0
    gS = float(w.loc[shorts].abs().sum()) if shorts else 0.0
    if gL > 0:
        w.loc[longs] /= gL
    if gS > 0:
        w.loc[shorts] /= gS

    # 6) cap per-asset
    w = w.clip(-float(max_weight_per_asset), float(max_weight_per_asset))

    # 7) scale to gross leverage
    gross = float(w.abs().sum())
    if gross > 0:
        if market_neutral:
            w = (w / gross) * float(gross_leverage)
        else:
            w = w * float(gross_leverage)

    # 8) vol targeting (soft clamp via scaling)
    if vol_target_enabled and target_daily_vol_bps > 0:
        # Use last vol_lookback bars on returns for risk estimate
        rets = _pct_change(closes).iloc[-int(vol_lookback):].fillna(0.0)
        pv = _portfolio_vol(w, rets)  # standard deviation of return
        target = float(target_daily_vol_bps) / 10000.0
        if pv > 1e-9 and np.isfinite(pv):
            scale = target / pv
            scale = float(np.clip(scale, float(vol_target_min_scale), float(vol_target_max_scale)))
            w = w * scale

    return w.fillna(0.0)
