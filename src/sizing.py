# =========================
# XSMOM-BOT — sizing.py (SAFE SIZING HARDENED)
# Implements:
# - Dynamic K selection
# - Portfolio volatility target scaling (true scaling; no post-renorm inflation)
# - No-trade bands with hysteresis
# - Tighter notional / weight caps (both % of equity and absolute USDT)
# - ADV% cap (optional) using exchange tickers
# - Hard gross cap & numeric sanitization
# - Back-compat: apply_kelly_scaling(), apply_liquidity_caps(..., equity_usdt=..., notional_cap_usdt=..., adv_cap_pct=..., tickers=...)
# - FIX: dataclass mutable defaults via default_factory
# =========================

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
import math
import numpy as np
import pandas as pd


# ----------------------------
# Utilities
# ----------------------------

def _finite(v):
    return np.isfinite(v)

def _sanitize_vec(x: np.ndarray) -> np.ndarray:
    x = np.where(np.isfinite(x), x, 0.0)
    x = np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0)
    return x

def _zscore_power(arr: np.ndarray, power: float) -> np.ndarray:
    """
    Cross-sectional z-score each row then apply signed |z|^power.
    arr shape: (T, N)
    """
    m = np.nanmean(arr, axis=1, keepdims=True)
    s = np.nanstd(arr, axis=1, keepdims=True) + 1e-12
    z = (arr - m) / s
    return np.sign(z) * (np.abs(z) ** power)


def _cs_std(row: np.ndarray) -> float:
    return float(np.nanstd(row))


def _select_topk(row: np.ndarray, k: int) -> np.ndarray:
    """
    Long top k, short bottom k, zero others. Returns weight vector (N,).
    """
    N = row.shape[0]
    if k <= 0 or k * 2 > N:
        return np.zeros(N, dtype=float)

    # argpartition for efficiency
    longs = np.argpartition(row, -k)[-k:]
    shorts = np.argpartition(row, k)[:k]
    w = np.zeros(N, dtype=float)
    w[longs] = 1.0 / k
    w[shorts] = -1.0 / k
    return w


def _normalize_gross(w: np.ndarray, gross: float) -> np.ndarray:
    s = float(np.sum(np.abs(w))) + 1e-12
    if s == 0.0:
        return w
    return w * (gross / s)


def _hard_cap_gross(w: np.ndarray, gross: float) -> np.ndarray:
    """Uniform down-scale so sum|w| <= gross. Never scales UP."""
    s = float(np.sum(np.abs(w)))
    if s <= gross or s <= 1e-12:
        return w
    return w * (gross / s)


def _apply_no_trade_bands(z: np.ndarray,
                          prev_w: Optional[np.ndarray],
                          z_entry: float,
                          z_exit: float) -> np.ndarray:
    """
    Hysteresis: if flat and |z| < z_entry -> stay flat.
    If already positioned, keep sign until |z| < z_exit.
    Works per-asset on the latest row only.
    """
    if prev_w is None:
        prev_w = np.zeros_like(z)

    out = np.zeros_like(z)
    for i in range(z.shape[0]):
        zi = z[i]
        wi = prev_w[i]
        mag = abs(zi)

        if wi == 0.0:
            out[i] = np.sign(zi) if mag >= z_entry else 0.0
        else:
            out[i] = np.sign(zi) if mag >= z_exit else 0.0
    return out


def _dynamic_k(z_row: np.ndarray, k_min: int, k_max: int, kappa: float, fallback_k: int) -> int:
    if not np.isfinite(z_row).all():
        return fallback_k
    disp = _cs_std(z_row)
    k = int(round(kappa * disp))
    return max(k_min, min(k_max, k))


def _realized_port_vol_ann(w_hist: np.ndarray, ret_hist: np.ndarray, bars_per_year: float = 8760.0) -> float:
    """
    Estimate realized portfolio annualized vol using mid-weight approximation.
    Default assumes hourly bars (8760/yr). Pass bars_per_year=365 if using daily.
    """
    if w_hist.shape[0] != ret_hist.shape[0]:
        T = min(w_hist.shape[0], ret_hist.shape[0])
        w_hist = w_hist[-T:, :]
        ret_hist = ret_hist[-T:, :]

    w_prev = np.roll(w_hist, 1, axis=0)
    w_prev[0, :] = 0.0
    pnl = np.sum(0.5 * (w_hist + w_prev) * ret_hist, axis=1)
    vol = float(np.std(pnl, ddof=0))
    ann_vol = vol * math.sqrt(bars_per_year)
    return ann_vol


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _round_weights(w: np.ndarray, digits: int = 8) -> np.ndarray:
    return np.round(w, digits)


def _try_get(d: dict, *keys, default=None):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def _infer_adv_quote_usdt(tkr: dict) -> Optional[float]:
    """
    Best-effort extraction of 24h quote turnover (USDT) from CCXT-style ticker dicts.
    Works with Bybit/Binance-style fields.
    """
    if not isinstance(tkr, dict):
        return None
    # Direct common fields
    for key in ("quoteVolume", "baseVolumeQuote", "24hQuoteVolume", "quote_volume"):
        v = tkr.get(key)
        try:
            if v is not None:
                return float(v)
        except Exception:
            pass
    # Nested vendor info
    info = tkr.get("info") or {}
    for key in ("turnover24h", "quote_volume", "quote_turnover_24h", "value"):
        v = info.get(key)
        try:
            if v is not None:
                return float(v)
        except Exception:
            pass
    return None


# ----------------------------
# Config dataclasses
# ----------------------------

@dataclass
class SelectionCfg:
    k_min: int = 2
    k_max: int = 8
    kappa: float = 5.0
    fallback_k: int = 6
    enabled: bool = True


@dataclass
class BandsCfg:
    enabled: bool = True
    z_entry: float = 0.65
    z_exit: float = 0.45


@dataclass
class VolTargetCfg:
    enabled: bool = True
    target_ann_vol: float = 0.35
    lookback_hours: int = 72
    min_scale: float = 0.5
    max_scale: float = 1.6
    bars_per_year: float = 8760.0  # set 365.0 if using daily returns


@dataclass
class StrategyCfg:
    lookbacks: Tuple[int, int, int] = (1, 6, 24)
    lookback_weights: Tuple[float, float, float] = (1.0, 1.5, 2.0)
    z_power: float = 1.35
    market_neutral: bool = True
    gross_leverage: float = 1.20
    max_weight_per_asset: float = 0.14
    per_symbol_notional_cap_pct: float = 0.20  # percent of equity per symbol (legacy)

    # FIX: use default_factory for nested configs (mutable default issue)
    selection: SelectionCfg = field(default_factory=SelectionCfg)
    no_trade_bands: BandsCfg = field(default_factory=BandsCfg)
    portfolio_vol_target: VolTargetCfg = field(default_factory=VolTargetCfg)

    # NOTE: We don't add new required fields here to keep back-compat.
    # We will detect optional attributes dynamically (e.g., per_symbol_notional_cap_usdt).


# ----------------------------
# Core computations
# ----------------------------

def compute_signal_scores(prices: pd.DataFrame,
                          lookbacks: List[int],
                          weights: List[float],
                          z_power: float) -> pd.DataFrame:
    """
    Build momentum scores as weighted sum of multi-lookback returns, then z^power.
    """
    assert len(lookbacks) == len(weights), "lookbacks/weights length mismatch"
    prices = prices.sort_index()
    T, N = prices.shape

    scores = np.zeros((T, N), dtype=float)
    vals = prices.values

    for lb, w in zip(lookbacks, weights):
        rr = np.zeros((T, N), dtype=float)
        if lb < T:
            rr[lb:, :] = vals[lb:, :] / vals[:-lb, :] - 1.0
        scores += w * rr

    scores = _zscore_power(scores, z_power)
    return pd.DataFrame(scores, index=prices.index, columns=prices.columns)


def build_targets(
    prices: pd.DataFrame,
    equity: float,
    strategy_cfg: StrategyCfg,
    prev_weights: Optional[pd.Series] = None,
    returns: Optional[pd.DataFrame] = None,
    weights_history: Optional[pd.DataFrame] = None,
    zscores_ready: Optional[pd.Series] = None
) -> pd.Series:
    """
    Main entry: compute target weights for the latest bar using:
    - multi-lookback momentum with z^power
    - market-neutral centering (optional)
    - no-trade bands (hysteresis)
    - dynamic K selection (dispersion-driven)
    - gross leverage normalization (never scales UP past target)
    - per-asset cap and per-symbol notional caps (both % of equity and absolute USDT)
    - portfolio vol-target scaling (true scaling; keep caps AFTER)
    - hard gross cap & rounding
    """

    prices = prices.sort_index()
    symbols = list(prices.columns)
    latest_ts = prices.index[-1]

    # Compute or accept precomputed z-scores
    if zscores_ready is not None and not zscores_ready.empty:
        z = zscores_ready.reindex(symbols).fillna(0.0).astype(float).values.copy()
    else:
        # Handle both z_power (sizing.py StrategyCfg) and signal_power (config.py StrategyCfg)
        z_power = getattr(strategy_cfg, 'z_power', getattr(strategy_cfg, 'signal_power', 1.35))
        scores = compute_signal_scores(
            prices,
            list(strategy_cfg.lookbacks),
            list(strategy_cfg.lookback_weights),
            z_power
        )
        z = scores.iloc[-1].values.copy()
    z = _sanitize_vec(z)

    # Market neutral: de-mean cross-section before ranking
    if strategy_cfg.market_neutral:
        z = z - float(np.nanmean(z))

    # Apply no-trade bands (hysteresis) per symbol
    prev_w_vec = None
    if prev_weights is not None:
        prev_w_vec = prev_weights.reindex(symbols).fillna(0.0).values
        prev_w_vec = _sanitize_vec(prev_w_vec)

    # Handle no_trade_bands (may not exist in config.py StrategyCfg)
    no_trade_bands = getattr(strategy_cfg, 'no_trade_bands', None)
    if no_trade_bands and getattr(no_trade_bands, 'enabled', False):
        z_dir = _apply_no_trade_bands(
            z,
            prev_w_vec,
            getattr(no_trade_bands, 'z_entry', 0.65),
            getattr(no_trade_bands, 'z_exit', 0.45)
        )
    else:
        z_dir = np.sign(z)

    # Dynamic K selection (handle both selection config and direct k_min/k_max)
    selection = getattr(strategy_cfg, 'selection', None)
    k_min = getattr(strategy_cfg, 'k_min', 2)
    k_max = getattr(strategy_cfg, 'k_max', 6)
    
    if selection and getattr(selection, 'enabled', False):
        # Use dynamic K selection
        k = _dynamic_k(
            z,
            getattr(selection, 'k_min', k_min),
            getattr(selection, 'k_max', k_max),
            getattr(selection, 'kappa', 5.0),
            getattr(selection, 'fallback_k', k_max)
        )
    else:
        # Fallback: use k_max directly (static K selection)
        k = k_max

    # Build raw long/short mask using the scores but respect banded direction
    masked_scores = z.copy()
    masked_scores[z_dir == 0.0] = 0.0

    raw_w = _select_topk(masked_scores, k)

    # Normalize toward target gross, but never increase above target if raw gross is lower.
    gross_tgt = float(strategy_cfg.gross_leverage)
    raw_sum_abs = float(np.sum(np.abs(raw_w)))
    if raw_sum_abs > 1e-12:
        scale = min(1.0, gross_tgt / raw_sum_abs)  # never scale UP above target
        w = raw_w * scale
    else:
        w = raw_w.copy()

    # Per-asset cap FIRST (do not renormalize upward after clipping)
    max_w = float(strategy_cfg.max_weight_per_asset)
    if max_w > 0:
        w = np.clip(w, -max_w, max_w)

    # Per-symbol notional caps:
    # (1) percentage of equity (legacy field)
    cap_pct = float(getattr(strategy_cfg, "per_symbol_notional_cap_pct", 0.0) or 0.0)
    # (2) absolute USDT cap (optional, detected dynamically)
    cap_abs = float(getattr(strategy_cfg, "per_symbol_notional_cap_usdt", 0.0) or 0.0)

    if equity and equity > 0:
        for i in range(len(symbols)):
            wi = float(w[i])
            if wi == 0.0:
                continue
            notional = abs(wi) * float(equity)

            # Percentage-of-equity cap
            if cap_pct > 0:
                limit_pct = cap_pct * float(equity)
                if notional > limit_pct:
                    w[i] = np.sign(wi) * (limit_pct / float(equity))
                    notional = abs(w[i]) * float(equity)

            # Absolute-USDT cap
            if cap_abs > 0 and notional > cap_abs:
                w[i] = np.sign(wi) * (cap_abs / float(equity))

    # Portfolio volatility target scaling (based on realized vol)
    # Handle both portfolio_vol_target (sizing.py) and vol_target (config.py)
    vol_target_cfg = getattr(strategy_cfg, 'portfolio_vol_target', None) or getattr(strategy_cfg, 'vol_target', None)
    if vol_target_cfg and getattr(vol_target_cfg, 'enabled', False) and returns is not None and weights_history is not None:
        # Handle different attribute names: lookback_hours vs vol_lookback
        lookback_hours = getattr(vol_target_cfg, 'lookback_hours', getattr(strategy_cfg, 'vol_lookback', 72))
        L = int(lookback_hours)
        w_hist = weights_history.reindex(prices.index).fillna(0.0).values[-L:]
        r_hist = returns.reindex(prices.index).fillna(0.0).values[-L:]
        if w_hist.size and r_hist.size:
            # Handle bars_per_year (may not exist in vol_target)
            bars_per_year = getattr(vol_target_cfg, 'bars_per_year', 8760.0)  # Default for hourly bars
            ann_vol = _realized_port_vol_ann(
                w_hist, r_hist, bars_per_year=float(bars_per_year)
            )
            # Handle target_ann_vol vs target_daily_vol_bps
            target_ann_vol = getattr(vol_target_cfg, 'target_ann_vol', None)
            if target_ann_vol is None:
                # Convert daily vol bps to annualized
                target_daily_bps = getattr(vol_target_cfg, 'target_daily_vol_bps', 0.0)
                target_ann_vol = (target_daily_bps / 10000.0) * np.sqrt(365.0) if target_daily_bps > 0 else 0.0
            tgt = float(target_ann_vol)
            if ann_vol > 1e-12 and tgt > 0:
                scaler = _clip(tgt / ann_vol,
                               float(getattr(vol_target_cfg, 'min_scale', 0.5)),
                               float(getattr(vol_target_cfg, 'max_scale', 2.0)))
                # True scaling (no renorm-to-gross here)
                w = w * scaler
                # Re-apply per-asset and per-symbol caps AFTER scaling
                if max_w > 0:
                    w = np.clip(w, -max_w, max_w)
                if equity and equity > 0:
                    for i in range(len(symbols)):
                        wi = float(w[i])
                        if wi == 0.0:
                            continue
                        notional = abs(wi) * float(equity)
                        if cap_pct > 0:
                            limit_pct = cap_pct * float(equity)
                            if notional > limit_pct:
                                w[i] = np.sign(wi) * (limit_pct / float(equity))
                                notional = abs(w[i]) * float(equity)
                        if cap_abs > 0 and notional > cap_abs:
                            w[i] = np.sign(wi) * (cap_abs / float(equity))

    # FINAL SAFETY: hard gross cap (only downscale if needed), sanitize & round
    w = _sanitize_vec(w)
    w = _hard_cap_gross(w, gross_tgt)
    if max_w > 0:
        w = np.clip(w, -max_w, max_w)
    w = _round_weights(w, digits=8)

    return pd.Series(w, index=symbols, name=latest_ts)


# ----------------------------
# Liquidity/Cap helpers (Back-compat extended)
# ----------------------------

def apply_liquidity_caps(
    targets: pd.Series,
    max_weight_per_asset: Optional[float] = None,
    # Extended / back-compat:
    equity_usdt: Optional[float] = None,
    equity: Optional[float] = None,
    per_symbol_notional_cap_pct: Optional[float] = None,
    cap_pct: Optional[float] = None,
    notional_cap_pct: Optional[float] = None,
    notional_cap_usdt: Optional[float] = None,
    adv_cap_pct: Optional[float] = None,
    tickers: Optional[Dict[str, dict]] = None,
    price_map: Optional[Dict[str, float]] = None,   # accepted but unused
    last_prices: Optional[Dict[str, float]] = None, # accepted but unused
    **kwargs
) -> pd.Series:
    """
    Liquidity/weight caps applied to an existing weight vector.
    - Clips per-asset weights to max_weight_per_asset if provided.
    - Enforces per-symbol notional caps by:
        (a) % of equity (per_symbol_notional_cap_pct / cap_pct / notional_cap_pct)
        (b) absolute USDT (notional_cap_usdt)
        (c) ADV% cap if tickers & adv_cap_pct provided (uses 24h quote turnover)
    Does NOT renormalize gross; call sites can choose whether to renorm later.
    """
    out = targets.copy()
    out = out.astype(float)

    # 1) Weight cap
    if max_weight_per_asset is not None:
        out = out.clip(lower=-float(max_weight_per_asset), upper=float(max_weight_per_asset))

    # Equity resolution
    eq = None
    for v in (equity_usdt, equity):
        if v is not None:
            try:
                eq = float(v)
                break
            except Exception:
                pass

    # 2) % of equity notional cap
    pct = None
    for v in (per_symbol_notional_cap_pct, cap_pct, notional_cap_pct):
        if v is not None:
            try:
                pct = float(v)
                break
            except Exception:
                pass

    if eq is not None and pct is not None and pct > 0:
        limit = pct * float(eq)
        if limit > 0:
            for sym, w in out.items():
                notional = abs(float(w)) * float(eq)
                if notional > limit and notional > 0:
                    scale = limit / notional
                    out.loc[sym] = float(w) * scale

    # 3) Absolute USDT notional cap
    if eq is not None and notional_cap_usdt is not None:
        try:
            abs_lim = float(notional_cap_usdt)
        except Exception:
            abs_lim = 0.0
        if abs_lim > 0:
            for sym, w in out.items():
                notional = abs(float(w)) * float(eq)
                if notional > abs_lim and notional > 0:
                    scale = abs_lim / notional
                    out.loc[sym] = float(w) * scale

    # 4) ADV% cap (best-effort from tickers)
    if eq is not None and adv_cap_pct is not None and tickers:
        try:
            adv_pct = float(adv_cap_pct)
        except Exception:
            adv_pct = 0.0
        if adv_pct > 0:
            for sym, w in out.items():
                tkr = (tickers or {}).get(sym) or {}
                adv_usdt = _infer_adv_quote_usdt(tkr)
                if adv_usdt and adv_usdt > 0:
                    limit = adv_pct * float(adv_usdt)
                    notional = abs(float(w)) * float(eq)
                    if notional > limit and notional > 0:
                        scale = limit / notional
                        out.loc[sym] = float(w) * scale

    # Final sanitize/round
    out = out.astype(float).replace([np.inf, -np.inf], 0.0).fillna(0.0)
    out = out.apply(lambda x: float(np.round(x, 8)))
    return out


def apply_kelly_scaling(targets: pd.Series, *args, **kwargs) -> pd.Series:
    """
    Back-compat shim so legacy live.py imports don't break.
    Accepts fraction/kelly_fraction/scale or kelly={fraction|f|scale}.
    """
    frac = 1.0
    for key in ("fraction", "kelly_fraction", "scale"):
        if key in kwargs:
            try:
                frac = float(kwargs[key]); break
            except Exception:
                pass

    kelly = kwargs.get("kelly")
    if isinstance(kelly, dict):
        for key in ("fraction", "f", "scale"):
            if key in kelly:
                try:
                    frac = float(kelly[key]); break
                except Exception:
                    pass

    frac = max(0.0, min(2.0, frac))
    return targets * frac


# =============================
# Advanced sizing extensions
# =============================

def apply_conviction_kelly_scaling(
    targets: pd.Series,
    scores: pd.Series,
    cfg: dict | None = None,
) -> pd.Series:
    """
    Conviction-weighted half-Kelly style scaling.
    - Map score ranks/quantiles to a conviction scale in [min_scale, max_scale].
    - Multiply weights by base_frac (e.g., 0.5 for half-Kelly) * conviction_scale.
    Config expected (all optional, with safe defaults):
      strategy.kelly.enabled: bool
      strategy.kelly.base_frac: float (default 0.5)
      strategy.kelly.half_kelly: bool (if True, base_frac defaults to 0.5)
      strategy.kelly.min_scale: float (default 0.5)
      strategy.kelly.max_scale: float (default 1.6)
    """
    try:
        st = (cfg or {}).get("strategy", {}) or {}
        kcfg = st.get("kelly", {}) or {}
        if not bool(kcfg.get("enabled", False)):
            return targets
        import numpy as np
        s = scores.copy().astype(float).replace([np.inf, -np.inf], np.nan).fillna(0.0)
        # Convert to ranks in [0,1]
        ranks = (s.rank(method="average") - 1.0) / max(1.0, len(s) - 1.0)
        base_frac = float(kcfg.get("base_frac", 0.5 if bool(kcfg.get("half_kelly", True)) else 1.0))
        lo, hi = float(kcfg.get("min_scale", 0.5)), float(kcfg.get("max_scale", 1.6))
        lo, hi = min(lo, hi), max(lo, hi)
        scale = lo + (hi - lo) * ranks
        out = (targets * (base_frac * scale)).astype(float)
        return out
    except Exception:
        return targets

def apply_sleeve_constraints(
    weights: pd.Series,
    sleeves: dict | None = None,
    sleeve_map: dict | None = None,
) -> pd.Series:
    """
    Cap weights by sleeve (e.g., meme sleeve).
    sleeves example:
      strategy:
        sleeve_constraints:
          meme:
            max_total_weight: 0.25
    sleeve_map: dict symbol -> sleeve_name
    Behavior: if sum(|w|) in a sleeve > cap, downscale that sleeve proportionally.
    """
    try:
        st = (sleeves or {}).get("strategy", {}) if sleeves and "strategy" in sleeves else (sleeves or {})
        sc = st.get("sleeve_constraints", {}) if isinstance(st, dict) else {}
        if not sc:
            return weights
        import numpy as np
        w = weights.copy().astype(float)
        if not isinstance(sleeve_map, dict) or not sleeve_map:
            return w
        sleeves_set = set(sleeve_map.get(sym, None) for sym in w.index)
        for sleeve in sleeves_set:
            if not sleeve or sleeve not in sc:
                continue
            cap = float(sc[sleeve].get("max_total_weight", 1.0))
            idx = [sym for sym in w.index if sleeve_map.get(sym) == sleeve]
            if not idx:
                continue
            gross = float(np.sum(np.abs(w.loc[idx])))
            if gross > max(1e-12, cap):
                scale = cap / gross
                w.loc[idx] = w.loc[idx] * scale
        return w
    except Exception:
        return weights

def scale_for_shock_mode(
    weights: pd.Series,
    cfg: dict | None = None,
    *, avg_pair_corr: float | None = None, vol_z: float | None = None
) -> pd.Series:
    """
    Scale down book in shock mode. Triggers:
      strategy.shock_mode.enabled: true
      strategy.shock_mode.vol_z_threshold: float (default 2.5)
      strategy.shock_mode.cap_scale: float (default 0.7) -> multiplies all weights
      (Optionally could bump entry bands; that lives in signals.py.)
    Inputs:
      avg_pair_corr, vol_z can be used; for now only vol_z is checked if provided.
    """
    try:
        st = (cfg or {}).get("strategy", {}) or {}
        sm = st.get("shock_mode", {}) or {}
        if not bool(sm.get("enabled", False)):
            return weights
        thr = float(sm.get("vol_z_threshold", 2.5))
        cap_scale = float(sm.get("cap_scale", 0.7))
        trigger = False
        if vol_z is not None:
            trigger = float(vol_z) >= thr
        if not trigger and avg_pair_corr is not None:
            trigger = float(avg_pair_corr) >= 0.85
        if not trigger:
            return weights
        return (weights.astype(float) * cap_scale).astype(float)
    except Exception:
        return weights

def finalize_weights_pipeline(
    targets: pd.Series,
    cfg: dict | None = None,
    *, 
    scores: pd.Series | None = None,
    sleeve_map: dict | None = None,
    avg_pair_corr: float | None = None,
    vol_z: float | None = None,
    tickers: dict | None = None,
    equity_usdt: float | None = None
) -> pd.Series:
    """
    Post-processing pipeline to be called after build_targets():
      1) Liquidity & notional caps (back-compat via apply_liquidity_caps)
      2) Sleeve constraints (e.g., meme max_total_weight)
      3) Conviction-weighted Kelly scaling (uses `scores`)
      4) Shock-mode scaling (global downscale)
    Keeps vector shape and index; numerically safe.
    """
    import numpy as np
    w = targets.copy().astype(float).replace([np.inf, -np.inf], 0.0).fillna(0.0)

    # 1) Liquidity caps
    st = (cfg or {}).get("strategy", {}) or {}
    liq = st.get("liquidity_caps", {}) or {}
    if bool(liq.get("enabled", False)):
        w = apply_liquidity_caps(
            w,
            max_weight_per_asset=liq.get("max_weight_low_liq", None),
            equity_usdt=equity_usdt,
            adv_cap_pct=liq.get("adv_cap_pct", None),
            tickers=tickers
        )

    # 2) Sleeve constraints
    w = apply_sleeve_constraints(w, cfg, sleeve_map)

    # 3) Conviction-weighted Kelly scaling
    if scores is not None:
        w = apply_conviction_kelly_scaling(w, scores, cfg)

    # 4) Shock-mode scaling
    w = scale_for_shock_mode(w, cfg, avg_pair_corr=avg_pair_corr, vol_z=vol_z)

    # Final sanitize & rounding
    w = w.astype(float).replace([np.inf, -np.inf], 0.0).fillna(0.0)
    w = w.apply(lambda x: float(np.round(x, 8)))
    return w






# =============================
# Correlation-Cluster Diversification (Top-K per cluster)
# =============================
def _cd_corr_matrix(prices: pd.DataFrame, lookback: int) -> pd.DataFrame:
    px = prices.ffill().bfill()
    rets = np.log(px/px.shift(1)).tail(int(lookback))
    if rets.shape[0] < max(10, int(lookback/3)):
        return pd.DataFrame(index=px.columns, columns=px.columns, dtype='float64')
    return rets.corr()

def _cd_connected_components(corr: pd.DataFrame, threshold: float) -> list[list[str]]:
    syms = list(corr.columns)
    n = len(syms)
    visited = set()
    comps: list[list[str]] = []
    adj = {s: [] for s in syms}
    for i in range(n):
        si = syms[i]
        for j in range(i+1, n):
            sj = syms[j]
            try:
                c = float(corr.iloc[i, j])
            except Exception:
                c = np.nan
            if np.isfinite(c) and c >= threshold:
                adj[si].append(sj)
                adj[sj].append(si)
    for s in syms:
        if s in visited: 
            continue
        stack = [s]
        comp = []
        while stack:
            u = stack.pop()
            if u in visited:
                continue
            visited.add(u)
            comp.append(u)
            for v in adj.get(u, []):
                if v not in visited:
                    stack.append(v)
        comps.append(comp)
    return comps

def _apply_cluster_diversification(weights: pd.Series, prices: pd.DataFrame, *, lookback: int = 240, corr_threshold: float = 0.75, max_per_cluster: int = 2) -> pd.Series:
    if weights is None or weights.empty:
        return weights
    if prices is None or getattr(prices, "empty", True):
        return weights
    corr = _cd_corr_matrix(prices.loc[:, weights.index], lookback=lookback)
    comps = _cd_connected_components(corr, threshold=float(corr_threshold))
    w = weights.copy().astype(float)
    for comp in comps:
        if not comp:
            continue
        sabs = w.reindex(comp).abs().sort_values(ascending=False)
        keep = list(sabs.head(int(max_per_cluster)).index)
        drop = [x for x in comp if x not in keep]
        if drop:
            w.loc[drop] = 0.0
    return w