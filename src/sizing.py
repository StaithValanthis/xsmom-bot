# sizing.py — v1.5 (2025-09-04)
# Enhancements:
# - Optional residual momentum (beta-neutral vs BTC/ETH) and sector-neutralization
# - Piecewise funding-tilt overlay with neutral band
# - Correlation-based diversify (greedy) kept, cleaned
# - Optional hysteresis on weight deltas (exported helper)
# - Soft vol targeting preserved
from __future__ import annotations

import math
from typing import Iterable, Optional, Callable, Dict, Tuple, List
import numpy as np
import pandas as pd

# ---------------------------- helpers ----------------------------

def _pct_change(df: pd.DataFrame) -> pd.DataFrame:
    """Safe percentage change (forward-filled, no chained warnings)."""
    df = df.ffill()
    return df.pct_change(fill_method=None)

def _zscore_last(x: pd.Series) -> float:
    """Z-score of the last value vs cross-section of last row of series vector."""
    mu = float(x.mean())
    sd = float(x.std(ddof=0))
    if sd <= 1e-12:
        return 0.0
    return (float(x.iloc[-1]) - mu) / sd

def _rolling_vol(rets: pd.DataFrame, n: int) -> pd.Series:
    v = rets.rolling(n).std(ddof=0).iloc[-1]
    v = v.replace([np.inf, -np.inf], np.nan).fillna(v.median() if np.isfinite(v.median()) else 0.0)
    v = v.clip(lower=1e-8)
    return v

def _portfolio_vol(w: pd.Series, rets: pd.DataFrame) -> float:
    # approximate daily vol from bar returns matrix rets (columns symbols)
    wv = w.reindex(rets.columns).fillna(0.0).values
    cov = np.cov(rets.fillna(0.0).values.T)
    pv = float(np.sqrt(np.maximum(0.0, wv @ cov @ wv)))
    return pv

def _regress_residuals(y: pd.Series, X: pd.DataFrame) -> pd.Series:
    """OLS residuals of y on X (with intercept)."""
    yv = y.values.astype(float)
    X_ = X.copy()
    X_["intercept"] = 1.0
    Xm = X_.values.astype(float)
    # Solve beta = argmin ||X beta - y||
    beta, *_ = np.linalg.lstsq(Xm, yv, rcond=None)
    yhat = Xm @ beta
    resid = yv - yhat
    return pd.Series(resid, index=y.index)

def _residualize_returns(
    rets: pd.DataFrame,
    benchmark_rets: Dict[str, pd.Series] | None,
    sector_map: Dict[str, str] | None,
    lookback: int,
    sector_neutral: bool,
) -> pd.DataFrame:
    """Return residualized returns (vs BTC/ETH + optional sector index)."""
    if benchmark_rets:
        # align benchmarks
        X = pd.DataFrame({k: v for k, v in benchmark_rets.items()}).reindex(rets.index).fillna(0.0)
    else:
        X = pd.DataFrame(index=rets.index)
    # sector indices
    if sector_neutral and sector_map:
        df = rets.copy()
        sec_df = {}
        for sec, cols in _group_by_value(sector_map).items():
            cols = [c for c in cols if c in df.columns]
            if len(cols) >= 2:
                sec_df[f"sec_{sec}"] = df[cols].mean(axis=1)
        if sec_df:
            X = pd.concat([X, pd.DataFrame(sec_df)], axis=1)
    if X.shape[1] == 0:
        return rets

    # use last `lookback` rows for regression
    Xlb = X.tail(lookback)
    out = {}
    for s in rets.columns:
        y = rets[s].tail(lookback)
        if y.notna().sum() < max(20, lookback // 3):
            out[s] = rets[s]  # not enough data
        else:
            r = _regress_residuals(y, Xlb.reindex(y.index))
            # stitch residual tail into full index (pad with original earlier data)
            full = rets[s].copy()
            full.loc[r.index] = r
            out[s] = full
    return pd.DataFrame(out).reindex_like(rets).fillna(0.0)

def _group_by_value(m: Dict[str, str]) -> Dict[str, List[str]]:
    d: Dict[str, List[str]] = {}
    for k, v in m.items():
        d.setdefault(v, []).append(k)
    return d

def _greedy_diversify(scores: pd.Series, corr: pd.DataFrame, max_pair_corr: float, k: int) -> List[str]:
    """
    Greedy pick by |score| while skipping names correlated > threshold
    to any already-selected name.
    """
    chosen: List[str] = []
    for s in scores.abs().sort_values(ascending=False).index:
        if len(chosen) >= k:
            break
        if all(abs(float(corr.loc[s, c])) <= max_pair_corr for c in chosen if (s in corr.index and c in corr.columns)):
            chosen.append(s)
    return chosen

# ---------------------------- API ----------------------------

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
    # funding / carry
    funding_tilt: Optional[Dict[str, float]] = None,
    funding_weight: float = 0.0,
    entry_zscore_min: float = 0.0,
    # diversify
    diversify_enabled: bool = False,
    corr_lookback: int = 48,
    max_pair_corr: float = 0.85,
    # residual momentum
    use_residuals: bool = False,
    benchmark_rets: Optional[Dict[str, pd.Series]] = None,  # e.g., {"BTC": rets, "ETH": rets}
    sector_neutral: bool = False,
    sector_map: Optional[Dict[str, str]] = None,
    weight_power: float = 1.0,
    # vol targeting
    vol_target_enabled: bool = False,
    target_daily_vol_bps: float = 0.0,
    vol_target_min_scale: float = 0.6,
    vol_target_max_scale: float = 1.6,
    # funding-tilt band
    funding_neutral_band_bps: float = 5.0,
) -> pd.Series:
    """
    Build cross-sectional momentum targets (weights by symbol).
    Returns pd.Series of weights summing to +/- gross leverage.
    """
    closes = closes.sort_index()
    closes = closes.ffill()

    # 1) Raw returns & residualization (optional)
    rets = _pct_change(closes).fillna(0.0)
    if use_residuals:
        lb = max(int(vol_lookback), max(lookbacks) * 2 if lookbacks else 60)
        rets = _residualize_returns(
            rets,
            benchmark_rets=benchmark_rets or {},
            sector_map=sector_map or {},
            lookback=lb,
            sector_neutral=sector_neutral,
        )

    # 2) Momentum score: weighted sum of multi-horizon simple returns
    lw = np.array(list(lookback_weights), dtype=float)
    lw = lw / lw.sum() if lw.sum() != 0 else lw
    score = pd.Series(0.0, index=closes.columns)
    for L, w in zip(lookbacks, lw):
        if L <= 0: 
            continue
        rL = (closes / closes.shift(L) - 1.0).iloc[-1]
        score = score.add(float(w) * rL, fill_value=0.0)
    score = score.replace([np.inf, -np.inf], np.nan).fillna(0.0)

    # 3) Cross-sectional z-scores (last bar)
    last_vals = score.copy()
    mu = float(last_vals.mean())
    sd = float(last_vals.std(ddof=0)) or 1.0
    z = (last_vals - mu) / sd

    # 4) Entry threshold
    z_mask = z.abs() >= float(entry_zscore_min)
    z = z.where(z_mask, other=0.0)

    # 5) Dynamic K (optional) or clamp
    k_lo, k_hi = int(k_min), int(k_max)
    if dynamic_k_fn is not None:
        try:
            k_lo, k_hi = dynamic_k_fn(z, int(k_min), int(k_max))
        except Exception:
            pass
    k = max(k_lo, min(k_hi, (z.abs() > 0).sum()))

    # 6) Diversify by correlation (optional)
    if diversify_enabled and k > 0:
        corr = rets.tail(int(corr_lookback)).corr().fillna(0.0)
        chosen = _greedy_diversify(z, corr, float(max_pair_corr), int(k))
        z = z.reindex(chosen).fillna(0.0)
        k = len(z)

    # 7) Size within long/short buckets: inverse-vol risk parity * |z|^p
    vol = _rolling_vol(rets, int(vol_lookback))
    inv_vol = (1.0 / vol).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    mag = (z.abs() ** float(weight_power)) * inv_vol.reindex(z.index).fillna(0.0)
    w = mag.where(z >= 0, -mag)

    # normalize within buckets
    if market_neutral:
        long_sum = float(mag[z >= 0].sum())
        short_sum = float(mag[z < 0].sum())
        if long_sum > 0:
            w.loc[z >= 0] *= (0.5 * float(gross_leverage)) / long_sum
        if short_sum > 0:
            w.loc[z < 0] *= (0.5 * float(gross_leverage)) / short_sum
    else:
        tot = float(mag.abs().sum())
        if tot > 0:
            w *= float(gross_leverage) / tot

    # 8) Funding tilt (piecewise with neutral band)
    if funding_tilt and float(funding_weight) != 0.0:
        band = float(funding_neutral_band_bps)
        adj = {}
        for s, wt in w.items():
            f_bps = float(funding_tilt.get(s, 0.0) or 0.0)
            # positive f_bps means LONG pays; negative means LONG receives
            if abs(f_bps) <= band:
                factor = 1.0
            else:
                pays_long = f_bps > 0
                # if we are long and paying -> downweight; if long and receiving -> upweight
                # for shorts, invert
                if wt >= 0:
                    factor = (1.0 - float(funding_weight)) if pays_long else (1.0 + float(funding_weight))
                else:
                    # short pays when f_bps < 0
                    pays_short = f_bps < 0
                    factor = (1.0 - float(funding_weight)) if pays_short else (1.0 + float(funding_weight))
            adj[s] = float(wt) * float(factor)
        w = pd.Series(adj).reindex(w.index).fillna(0.0)

    # 9) Per-asset caps
    max_w = float(max_weight_per_asset or 0.0)
    if max_w > 0:
        w = w.clip(lower=-max_w, upper=max_w)

    # 10) Soft vol targeting (scale)
    if vol_target_enabled and target_daily_vol_bps > 0:
        T = int(max(vol_lookback, max(lookbacks) if lookbacks else 60))
        pv = _portfolio_vol(w.fillna(0.0), _pct_change(closes).tail(T).fillna(0.0))
        if pv > 1e-9:
            target = float(target_daily_vol_bps) / 10000.0
            scale = float(target / pv)
            scale = float(np.clip(scale, float(vol_target_min_scale), float(vol_target_max_scale)))
            w *= scale

    return w.reindex(closes.columns).fillna(0.0)

# ---------------------------- Liquidity caps ----------------------------

def apply_liquidity_caps(
    targets: pd.Series,
    *,
    equity_usdt: float,
    tickers: Dict[str, dict],
    adv_cap_pct: float,
    notional_cap_usdt: float,
) -> pd.Series:
    """
    Cap per-symbol notional by (ADV * pct) and absolute USDT cap.
    Then renormalize to preserve original gross exposure.
    """
    w = targets.copy().fillna(0.0)
    eq = float(equity_usdt)
    new_w = w.copy()

    for s, wt in w.items():
        t = tickers.get(s, {}) or {}
        last = t.get("last") or t.get("close") or 0.0
        qv = t.get("quoteVolume", 0.0) or 0.0  # 24h quote volume
        adv_usdt = float(qv) / 24.0  # rough hourly ADV in USDT
        cap_adv = float(adv_usdt) * float(adv_cap_pct)
        cap_abs = float(notional_cap_usdt)

        cap = min(cap_adv, cap_abs) if cap_abs > 0 else cap_adv
        if cap < float('inf'):
            desired = abs(wt) * eq
            if desired > cap and desired > 0:
                scale = cap / desired
                new_w.loc[s] = math.copysign(abs(wt) * scale, wt)

    # Renormalize to preserve gross leverage
    gross_old = float(w.abs().sum())
    gross_new = float(new_w.abs().sum())
    if gross_new > 0 and gross_old > 0:
        new_w *= gross_old / gross_new
    return new_w.fillna(0.0)

# ---------------------------- Hysteresis helper ----------------------------

def apply_hysteresis(
    new_w: pd.Series,
    old_w: Optional[pd.Series],
    min_change_bps: float = 10.0
) -> pd.Series:
    """
    Suppress tiny weight changes to reduce churn.
    Keep changes whose absolute delta >= min_change_bps (i.e., 10 bps = 0.001).
    """
    if old_w is None or len(old_w) == 0:
        return new_w
    thresh = float(min_change_bps) / 10000.0
    old_w = old_w.reindex(new_w.index).fillna(0.0)
    delta = (new_w - old_w).fillna(0.0)
    keep = delta.abs() >= thresh
    out = old_w.copy()
    out.loc[keep] = new_w.loc[keep]
    return out
