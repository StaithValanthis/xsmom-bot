import logging
import pandas as pd
import numpy as np
from typing import Dict, Optional, List

from .signals import momentum_score, inverse_vol_weights

log = logging.getLogger("sizing")

def _percentile_tilt(series: pd.Series) -> pd.Series:
    """Map values to [-1, +1] by percentile."""
    if series.empty:
        return series
    r = series.rank(pct=True)
    return (r - 0.5) * 2.0

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
    """
    Greedy forward selection to cap pairwise correlation.
    ranked: list of symbols sorted by desirability
    rets: recent returns (rows=time, cols=symbol)
    """
    chosen: List[str] = []
    if k <= 0 or len(ranked) == 0 or rets.shape[0] < 2:
        return chosen

    corr = rets.corr().clip(-1.0, 1.0).fillna(0.0)
    for s in ranked:
        if len(chosen) == 0:
            chosen.append(s)
            if len(chosen) >= k:
                break
            continue
        ok = True
        for c in chosen:
            try:
                if abs(float(corr.loc[s, c])) > max_pair_corr:
                    ok = False
                    break
            except Exception:
                pass
        if ok:
            chosen.append(s)
            if len(chosen) >= k:
                break

    # If we couldn't fill k due to correlation cap, allow loosening a bit by padding
    if len(chosen) < k:
        for s in ranked:
            if s not in chosen:
                chosen.append(s)
                if len(chosen) >= k:
                    break

    return chosen

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
    # Funding tilt
    funding_tilt: Optional[Dict[str, float]] = None,
    funding_weight: float = 0.0,
    # NEW: quality, diversification, vol-targeting controls
    entry_zscore_min: float = 0.0,
    diversify_enabled: bool = False,
    corr_lookback: int = 48,
    max_pair_corr: float = 0.9,
    vol_target_enabled: bool = False,
    target_daily_vol_bps: float = 0.0,
    vol_target_min_scale: float = 0.5,
    vol_target_max_scale: float = 2.0,
) -> pd.Series:
    assert prices.shape[0] > max(max(lookbacks), vol_lookback) + 5, "Not enough bars"

    last_prices = prices.iloc[-1]
    valid_cols = last_prices[last_prices.notna() & (last_prices > 0)].index
    prices = prices[valid_cols]

    # Momentum + inverse-vol
    score = momentum_score(prices, lookbacks, weights)

    # Funding tilt (carry)
    if funding_tilt and abs(funding_weight) > 1e-12:
        f = pd.Series({k: float(v) for k, v in (funding_tilt or {}).items()}, dtype="float64")
        f = f.reindex(prices.columns)
        f = f.fillna(f.median())
        tilt = -_percentile_tilt(f)   # expensive funding -> negative tilt
        score = score.add(funding_weight * tilt, fill_value=0.0)

    # Entry quality filter
    if entry_zscore_min > 0:
        z = _zscore(score.fillna(0.0))
        mask = z.abs() >= entry_zscore_min
        score = score.where(mask, other=0.0)

    iv = inverse_vol_weights(prices, vol_lookback)

    # Returns for correlation / vol targeting
    rets = prices.pct_change().dropna()
    rets_vol = rets.tail(vol_lookback) if rets.shape[0] >= vol_lookback else rets

    # Determine K via dispersion (caller may pass dynamic)
    topk, bottomk = dynamic_k_fn(score, k_min, k_max)
    ranked = score.sort_values(ascending=False)

    # Longs / shorts candidate lists
    long_list = ranked.index.tolist()[:max(0, topk)]
    short_list = ranked.index.tolist()[-max(0, bottomk):][::-1]  # reverse: most negative first

    # Diversification gate (optional)
    if diversify_enabled and rets.shape[0] >= max(20, corr_lookback):
        recent = rets.tail(corr_lookback)
        long_list = _pick_diversified(long_list, recent, topk, max_pair_corr)
        short_list = _pick_diversified(short_list, recent, bottomk, max_pair_corr)

    # Build weights
    w = pd.Series(0.0, index=prices.columns)
    if market_neutral:
        long_raw = (score.loc[long_list].clip(lower=0.0)) * iv.loc[long_list]
        short_raw = (-score.loc[short_list].clip(upper=0.0)) * iv.loc[short_list]
        long_w = long_raw / long_raw.sum() if long_raw.sum() > 0 else pd.Series(0.0, index=long_list)
        short_w = short_raw / short_raw.sum() if short_raw.sum() > 0 else pd.Series(0.0, index=short_list)
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

    # Volatility targeting (optional; approximate)
    if vol_target_enabled and target_daily_vol_bps > 0 and rets_vol.shape[0] >= 20:
        try:
            cov = rets_vol.cov().fillna(0.0).values  # covariance of hourly returns
            wv = w.reindex(prices.columns).fillna(0.0).values
            port_vol_hourly = float(np.sqrt(max(0.0, np.dot(wv, cov @ wv))))
            port_vol_daily = port_vol_hourly * np.sqrt(24.0)  # assume 1h bars
            target = float(target_daily_vol_bps) / 10_000.0   # bps -> decimal
            if port_vol_daily > 1e-12 and target > 0:
                scale = target / port_vol_daily
                scale = float(np.clip(scale, vol_target_min_scale, vol_target_max_scale))
                if np.isfinite(scale) and scale > 0:
                    w = w * scale
        except Exception as e:
            log.debug(f"vol_targeting failed: {e}")

    # Re-normalize to match gross leverage when market-neutral (post caps/scale)
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
