# regime_router.py — XSMOM-BOT (TSMOM/XSMOM regime router, object/dict-safe)
from __future__ import annotations

from typing import Any, Sequence
import numpy as np
import pandas as pd


# --------------------------- config access helpers ---------------------------

def _cfg_get(root: Any, *path: str, default: Any = None) -> Any:
    """
    Safe getter that works for dicts and nested objects.
    Example: _cfg_get(cfg, "strategy", "mode", default="auto")
    """
    cur = root
    for key in path:
        if cur is None:
            return default
        # dict-like
        if isinstance(cur, dict):
            cur = cur.get(key, default if key == path[-1] else None)
            continue
        # object-like (Pydantic, dataclass, custom *Cfg)
        if hasattr(cur, key):
            cur = getattr(cur, key)
            continue
        # fallback
        return default
    return default if cur is None else cur


def _as_list(x: Any, fallback: list) -> list:
    if x is None:
        return list(fallback)
    if isinstance(x, (list, tuple)):
        return list(x)
    return list(fallback)


def _as_int(x: Any, fallback: int) -> int:
    try:
        return int(x)
    except Exception:
        return int(fallback)


def _as_float(x: Any, fallback: float) -> float:
    try:
        return float(x)
    except Exception:
        return float(fallback)


def _as_bool(x: Any, fallback: bool) -> bool:
    if isinstance(x, bool):
        return x
    if x is None:
        return fallback
    s = str(x).strip().lower()
    if s in ("1", "true", "yes", "y", "on"):
        return True
    if s in ("0", "false", "no", "n", "off"):
        return False
    return fallback


# --------------------------- math helpers ---------------------------

def _pct_change_df(df: pd.DataFrame) -> pd.DataFrame:
    """Forward-fill then compute percent change by column."""
    return df.ffill().pct_change()


def _zscore(s: pd.Series) -> pd.Series:
    """Standardize a vector and sanitize inf/nan."""
    m, sd = float(s.mean()), float(s.std())
    if not np.isfinite(sd) or sd < 1e-12:
        sd = 1.0
    z = (s - m) / sd
    return z.replace([np.inf, -np.inf], 0.0).fillna(0.0)


def _inverse_vol_weights(closes_window: pd.DataFrame, vol_lookback: int) -> pd.Series:
    """
    Inverse-volatility weights over the last `vol_lookback` bars.
    Falls back to equal weights if the denominator degenerates.
    """
    cols = list(closes_window.columns)
    if len(cols) == 0:
        return pd.Series(dtype=float)

    rets = _pct_change_df(closes_window)
    if len(rets) == 0:
        return pd.Series(1.0 / len(cols), index=cols, dtype=float)

    rets = rets.iloc[-min(len(rets), max(1, int(vol_lookback))):]
    vol = rets.std()

    iv = 1.0 / vol.replace(0.0, np.nan)
    iv = iv.replace([np.inf, -np.inf], np.nan)

    if not np.isfinite(iv.dropna().sum()) or iv.dropna().sum() <= 0:
        return pd.Series(1.0 / len(cols), index=cols, dtype=float)

    iv = iv / iv.sum(skipna=True)
    return iv.fillna(0.0).reindex(cols).astype(float)


def _last_over_lookback_return(closes_window: pd.DataFrame, lb: int) -> pd.Series:
    """
    Return vector of (P_t / P_{t-lb} - 1) per asset. If not enough history, zeros.
    """
    if lb <= 0 or lb >= len(closes_window):
        return pd.Series(0.0, index=closes_window.columns, dtype=float)
    last = closes_window.iloc[-1]
    prev = closes_window.iloc[-lb]
    r = (last / prev - 1.0).astype(float)
    return r.replace([np.inf, -np.inf], 0.0).fillna(0.0)


# --------------------------- signal engines ---------------------------

def xsmom_score(closes_window: pd.DataFrame,
                lookbacks: Sequence[int],
                weights: Sequence[float]) -> pd.Series:
    """
    Cross-sectional momentum score:
    Weighted sum of same-timestamp lookback returns across multiple horizons.
    """
    score = pd.Series(0.0, index=closes_window.columns, dtype=float)
    for lb, w in zip(lookbacks, weights):
        score = score.add(float(w) * _last_over_lookback_return(closes_window, int(lb)), fill_value=0.0)
    return score.fillna(0.0)


def tsmom_score(closes_window: pd.DataFrame,
                lookbacks: Sequence[int],
                weights: Sequence[float]) -> pd.Series:
    """
    Time-series momentum score:
    Same formula as XSMOM at the asset level (own-trend); portfolio construction differs.
    """
    ts = pd.Series(0.0, index=closes_window.columns, dtype=float)
    for lb, w in zip(lookbacks, weights):
        ts = ts.add(float(w) * _last_over_lookback_return(closes_window, int(lb)), fill_value=0.0)
    return ts.fillna(0.0)


# --------------------------- diagnostics ---------------------------

def cross_sectional_dispersion(closes_window: pd.DataFrame,
                               lookback: int,
                               metric: str = "std") -> float:
    """
    Cross-sectional dispersion (in bps) of lookback returns across assets.
    metric: "std" (standard deviation) or "mad" (median absolute deviation around median).
    """
    if len(closes_window) <= max(1, int(lookback)):
        return float("inf")  # don't block on short history
    xret = _last_over_lookback_return(closes_window, int(lookback))
    if str(metric).lower() == "mad":
        val = float((xret - xret.median()).abs().median())
    else:
        val = float(xret.std())
    return 10_000.0 * val


def average_pairwise_correlation(closes_window: pd.DataFrame, lb: int = 96) -> float:
    """
    Average off-diagonal correlation of returns over last lb bars.
    """
    if len(closes_window) <= lb + 1:
        return 0.0
    rets = _pct_change_df(closes_window).iloc[-lb:]
    c = rets.corr().values
    n = c.shape[0]
    if n < 2:
        return 0.0
    off_diag_sum = (c.sum() - np.trace(c))
    denom = n * (n - 1)
    return float(off_diag_sum / max(denom, 1))


def majors_trend_ok(majors_series: pd.Series,
                    ema_len: int = 200,
                    slope_min_bps_per_day: float = 2.0) -> bool:
    """
    Simple trend gate on a 'majors' composite (e.g., mean of BTC & ETH closes).
    Uses EMA slope over ~25 bars to approximate bps/day drift.
    """
    ema = majors_series.ffill().ewm(span=max(2, int(ema_len)), adjust=False).mean()
    if len(ema) < max(ema_len, 25):
        # not enough data: don't block
        return True
    now, prev = float(ema.iloc[-1]), float(ema.iloc[-25])
    if now <= 0 or prev <= 0:
        return True
    slope_bps_per_day = ((now / prev) - 1.0) * 10_000.0
    return slope_bps_per_day >= float(slope_min_bps_per_day)


# --------------------------- regime decision ---------------------------

def decide_mode(cfg: Any, closes_window: pd.DataFrame) -> str:
    """
    Decide between 'tsmom' and 'xsmom' based on correlations, majors trend, and dispersion.

    cfg can be a dict or nested object tree.
    Expected keys/attrs:
      strategy.mode: "auto" | "xsmom" | "tsmom"
      strategy.regime_switch.{corr_lookback,corr_high,majors_ema,slope_min_bps_per_day}
      strategy.dispersion_gate.{lookback,metric,threshold_bps}
    """
    explicit = str(_cfg_get(cfg, "strategy", "mode", default="auto")).strip().lower()
    if explicit in ("xsmom", "tsmom"):
        return explicit

    # Defensive early-out on empty/none
    if closes_window is None or getattr(closes_window, "empty", True) or closes_window.shape[1] == 0:
        return "xsmom"

    # regime switch (correlation + majors trend)
    corr_lb = _as_int(_cfg_get(cfg, "strategy", "regime_switch", "corr_lookback", default=96), 96)
    corr_hi = _as_float(_cfg_get(cfg, "strategy", "regime_switch", "corr_high", default=0.60), 0.60)
    majors_ema = _as_int(_cfg_get(cfg, "strategy", "regime_switch", "majors_ema", default=200), 200)
    slope_min = _as_float(_cfg_get(cfg, "strategy", "regime_switch", "slope_min_bps_per_day", default=2.0), 2.0)

    corr = average_pairwise_correlation(closes_window, lb=corr_lb)

    # Majors proxy: mean of first 2 cols if available; single col if only 1
    ncols = closes_window.shape[1]
    if ncols >= 2:
        majors = closes_window.iloc[:, :2].mean(axis=1)
    elif ncols == 1:
        majors = closes_window.iloc[:, 0]
    else:
        return "xsmom"

    trending = majors_trend_ok(majors, ema_len=majors_ema, slope_min_bps_per_day=slope_min)

    # High correlation + trending → TSMOM
    if corr >= corr_hi and trending:
        return "tsmom"

    # Otherwise prefer XSMOM if there is enough cross-sectional dispersion
    disp_lb = _as_int(_cfg_get(cfg, "strategy", "dispersion_gate", "lookback", default=24), 24)
    disp_metric = str(_cfg_get(cfg, "strategy", "dispersion_gate", "metric", default="std"))
    disp_thr = _as_float(_cfg_get(cfg, "strategy", "dispersion_gate", "threshold_bps", default=35.0), 35.0)

    disp_bps = cross_sectional_dispersion(closes_window, lookback=disp_lb, metric=disp_metric)
    if disp_bps >= disp_thr:
        return "xsmom"

    # Low dispersion fallback → TSMOM
    return "tsmom"


def build_targets_auto(closes_window: pd.DataFrame, cfg: Any) -> pd.Series:
    """
    Build normalized weights (sum |w| = target gross) using either XSMOM or TSMOM,
    chosen by decide_mode(...). Per-mode neutrality and breadth controls are applied.

    cfg can be a dict or nested object tree.
    """
    if closes_window is None or closes_window.empty:
        return pd.Series(dtype=float)

    lookbacks = _as_list(_cfg_get(cfg, "strategy", "lookbacks", default=[1, 6, 24]), [1, 6, 24])
    weights = _as_list(_cfg_get(cfg, "strategy", "lookback_weights", default=[1.0, 1.0, 1.0]), [1.0, 1.0, 1.0])
    vol_lb = _as_int(_cfg_get(cfg, "strategy", "vol_lookback", default=72), 72)
    power = _as_float(_cfg_get(cfg, "strategy", "signal_power", default=1.35), 1.35)

    # Choose regime
    mode = decide_mode(cfg, closes_window)

    # Score
    if mode == "xsmom":
        score = xsmom_score(closes_window, lookbacks, weights)
    else:
        score = tsmom_score(closes_window, lookbacks, weights)

    # Inverse-vol sizing baseline
    iw = _inverse_vol_weights(closes_window, vol_lb)

    # Nonlinear amplification of conviction, preserve sign via sign(score)
    sz = _zscore(score).abs() ** power
    raw = sz * np.sign(score) * iw

    # Normalize to L1 = 1 (handle degenerate all-zero case)
    denom = float(raw.abs().sum())
    w = (raw / denom) if denom > 1e-12 else raw * 0.0

    # Optional per-mode market-neutralization (demean, then renormalize)
    mn_xs = _as_bool(_cfg_get(cfg, "sizing", "market_neutral_xsmom", default=True), True)
    mn_ts = _as_bool(_cfg_get(cfg, "sizing", "market_neutral_tsmom", default=False), False)
    neutralize = (mode == "xsmom" and mn_xs) or (mode == "tsmom" and mn_ts)
    if neutralize:
        w = w - w.mean()
        denom = float(w.abs().sum())
        if denom > 1e-12:
            w = w / denom

    # Per-asset cap, then scale to target gross
    max_w = _as_float(_cfg_get(cfg, "sizing", "max_weight_per_asset", default=0.14), 0.14)
    w = w.clip(lower=-max_w, upper=max_w)
    gross = float(w.abs().sum())
    target_gross = _as_float(_cfg_get(cfg, "sizing", "gross_leverage", default=2.0), 2.0)
    if gross > 1e-12:
        w = w * (target_gross / gross)

    # Optional breadth minimum for XSMOM (avoid concentrating in too few names)
    if mode == "xsmom":
        min_breadth = _as_int(_cfg_get(cfg, "strategy", "dispersion_gate", "min_breadth", default=6), 6)
        if int((w.abs() > 1e-12).sum()) < max(1, min_breadth):
            w = w * 0.0

    # Ensure index consistency
    return w.reindex(closes_window.columns).fillna(0.0).astype(float)