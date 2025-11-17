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


# ================= Two-bar confirmation gate (active call site wiring) =================
from collections import deque

class _ZBuffer:
    def __init__(self, maxlen: int = 2):
        self.buf = {}
        self.maxlen = maxlen

    def push(self, sym: str, zval: float):
        dq = self.buf.get(sym)
        if dq is None:
            dq = deque(maxlen=self.maxlen)
            self.buf[sym] = dq
        dq.append(float(zval))

    def last_n(self, sym: str, n: int):
        dq = self.buf.get(sym) or []
        return list(dq)[-n:] if len(dq) >= n else []

_zbuf = _ZBuffer(maxlen=2)

def confirmation_ok(symbol: str, z_now: float, entry_zmin: float, cfg: dict) -> bool:
    """Return True if two-bar confirmation passes (if enabled), else True."""
    try:
        cf = (cfg or {}).get("strategy", {}).get("confirmation", {}) or {}
        if not bool(cf.get("enabled", False)):
            return True
        n = max(2, int(cf.get("lookback_bars", 2)))
        z_boost = float(cf.get("z_boost", 0.0))
        thr = float(entry_zmin) + float(z_boost)
        prev = _zbuf.last_n(symbol, n-1)
        if len(prev) < (n-1):
            return False
        cond_prev = all(abs(v) >= thr for v in prev)
        cond_now  = abs(float(z_now)) >= thr
        return bool(cond_prev and cond_now)
    except Exception:
        return True

def apply_confirmation_gate(zscores, entry_zmin: float, cfg: dict):
    """
    Returns a *copy* of zscores where symbols failing confirmation are zeroed,
    and pushes current z into the rolling buffer for next bar checks.
    Accepts pandas Series or a mapping-like object with .items().
    """
    try:
        import pandas as pd
        zs = zscores.copy()
        for sym, z in zs.items():
            if not confirmation_ok(sym, float(z), entry_zmin, cfg):
                zs.loc[sym] = 0.0
            _zbuf.push(sym, float(z))
        return zs
    except Exception:
        return zscores

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

# NOTE (auto-wiring):
# If your eligibility step didn't match our pattern, call this explicitly where you
# finalize zscores and before selection:
#   z_entry_thr = float(cfg.get("strategy", {}).get("no_trade_bands", {}).get("z_entry", 0.55))
#   zscores = apply_confirmation_gate(zscores, z_entry_thr, cfg)


# ================= Entry threshold resolution (simplified - dynamic entry band removed) =================
def _resolve_entry_threshold_from_cfg(cfg: dict, *, avg_pair_corr: float | None = None, atr_pct: float | None = None, default: float = 0.55) -> float:
    """
    Resolve entry threshold from config.
    
    NOTE: Dynamic entry band removed per parameter review (overfitting risk).
    Now uses single entry_zscore_min or no_trade_bands.z_entry.
    """
    try:
        st = (cfg or {}).get("strategy", {}) or {}
        # Use single entry_zscore_min (preferred)
        entry_zmin = st.get("entry_zscore_min")
        if entry_zmin is not None:
            return float(entry_zmin)
        # Fallback to no_trade_bands.z_entry
        nb = st.get("no_trade_bands", {}) or {}
        return float(nb.get("z_entry", default))
    except Exception:
        return float(default)

def apply_entry_band(zscores, entry_zmin: float):
    try:
        zs = zscores.copy()
        thr = float(entry_zmin)
        for sym, z in zs.items():
            if abs(float(z)) < thr:
                try:
                    zs.loc[sym] = 0.0
                except Exception:
                    zs[sym] = 0.0
        return zs
    except Exception:
        return zscores

def compute_breadth(zscores, entry_zmin: float) -> float:
    try:
        if hasattr(zscores, "abs"):
            return float((zscores.abs() >= float(entry_zmin)).mean())
        vals = [abs(float(v)) >= float(entry_zmin) for _, v in zscores.items()]
        return float(sum(vals) / max(1, len(vals)))
    except Exception:
        return 1.0

def apply_breadth_gate(zscores, entry_zmin: float, *, min_breadth: float = 0.20, zero_when_blocked: bool = True):
    b = compute_breadth(zscores, entry_zmin)
    if b < float(min_breadth):
        if zero_when_blocked:
            try:
                zs = zscores.copy()
                if hasattr(zs, "loc"):
                    zs.loc[:] = 0.0
                else:
                    for k in list(zs.keys()):
                        zs[k] = 0.0
                return zs, b
            except Exception:
                return zscores, b
        else:
            return zscores, b
    return zscores, b

def gate_zscores_pipeline(zscores, cfg: dict, *, avg_pair_corr: float | None = None, atr_pct: float | None = None) -> tuple:
    meta = {"entry_zmin_used": None, "breadth": None, "blocked": False}
    try:
        entry_z = _resolve_entry_threshold_from_cfg(cfg, avg_pair_corr=avg_pair_corr, atr_pct=atr_pct, default=0.55)
        meta["entry_zmin_used"] = float(entry_z)
        zs = apply_entry_band(zscores, float(entry_z))
        st = (cfg or {}).get("strategy", {}) or {}
        bg = st.get("breadth_gate", {}) or {}
        min_breadth = float(bg.get("min_fraction", 0.20))
        zero_block = bool(bg.get("zero_when_blocked", True))
        zs, breadth = apply_breadth_gate(zs, float(entry_z), min_breadth=min_breadth, zero_when_blocked=zero_block)
        meta["breadth"] = float(breadth)
        meta["blocked"] = bool(breadth < min_breadth and zero_block)
        zs = apply_confirmation_gate(zs, float(entry_z), cfg)
        return zs, meta
    except Exception:
        return zscores, meta


# ================= Rolling average pairwise correlation helper =================
def compute_avg_pair_corr(prices, lookback: int = 48) -> float:
    """
    Compute a cross-sectional average pairwise correlation of returns over a rolling window.
    Accepts:
      - pandas.DataFrame of close prices (columns = symbols), or
      - dict[str, pandas.Series] (each series indexed identically).
    Returns a float in [-1, 1]; falls back to 0.0 on failure.
    """
    try:
        import pandas as pd, numpy as np
        if isinstance(prices, dict):
            df = pd.DataFrame(prices)
        else:
            df = prices
        px = df.ffill().bfill()
        rets = np.log(px/px.shift(1)).tail(int(lookback))
        if rets.shape[0] < max(10, int(lookback/3)):
            return 0.0
        corr = rets.corr().values
        n = corr.shape[0]
        if n <= 1:
            return 0.0
        tri_vals = [corr[i,j] for i in range(n) for j in range(i+1, n)]
        if not tri_vals:
            return 0.0
        return float(sum(tri_vals) / len(tri_vals))
    except Exception:
        return 0.0


# ================= Convenience wrapper for callers =================

def _compute_base_xsec_z(prices_df: pd.DataFrame, cfg: dict | None = None) -> pd.Series:
    """
    Compute cross-sectional z from weighted multi-lookback returns and apply z_power.
    Uses strategy.lookbacks (default [1,6,24]) and lookback_weights (default [1.0,1.5,2.0]).
    Returns last-row z^power series aligned to columns (symbols).
    """
    import numpy as np, pandas as pd
    if prices_df is None or getattr(prices_df, "empty", True):
        return pd.Series(dtype="float64")
    st = (cfg or {}).get("strategy", {}) or {}
    lookbacks = list(st.get("lookbacks", [1,6,24]))
    weights   = list(st.get("lookback_weights", [1.0,1.5,2.0]))
    z_power   = float(st.get("z_power", 1.35))
    assert len(lookbacks) == len(weights), "strategy.lookbacks/weights length mismatch"
    px = prices_df.ffill().bfill()
    T, N = px.shape
    S = 0.0
    acc = None
    vals = px.values
    import numpy as np
    for lb, w in zip(lookbacks, weights):
        rr = np.zeros_like(vals, dtype=float)
        if lb < T:
            rr[lb:, :] = vals[lb:, :] / vals[:-lb, :] - 1.0
        acc = (acc + w * rr) if acc is not None else (w * rr)
    # cross-sectional z on last row
    last = acc[-1, :] if acc is not None else np.zeros(N, dtype=float)
    mu = float(np.nanmean(last)) if np.isfinite(last).any() else 0.0
    sd = float(np.nanstd(last)) if np.isfinite(last).any() else 1.0
    if sd == 0.0: sd = 1.0
    z = (last - mu) / sd
    z = np.sign(z) * (np.abs(z) ** z_power)
    return pd.Series(z, index=px.columns, dtype="float64")
def prepare_zscores_for_selection(
    zscores: pd.Series | None,
    cfg: dict,
    *,
    prices_df: Optional[pd.DataFrame] = None,
    next_funding_bps: Optional[pd.Series] = None,
    corr_lookback: int = 48,
    atr_pct: float | None = None
) -> tuple[pd.Series, dict]:
    """
    Apply ensemble blend + funding trim (if enabled), then run the entry/breadth gating
    using average pairwise correlation derived from prices_df.
    Returns: (zscores_ready, meta)
    """
    meta = {"ensemble_applied": False, "funding_trim_applied": False, "avg_pair_corr": None}

    # ---- Base zscores (if none provided) ----
    if zscores is None and prices_df is not None and not getattr(prices_df, "empty", True):
        try:
            zscores = _compute_base_xsec_z(prices_df, cfg)
        except Exception:
            pass
    if zscores is None:
        # as a last resort, return empty
        import pandas as pd
        zscores = pd.Series(dtype="float64")

    # ---- Ensemble (x-sec z + ts sign + breakout) ----
    try:
        ens = ((cfg or {}).get("strategy", {}) or {}).get("ensemble", {}) or {}
        if bool(ens.get("enabled", False)) and prices_df is not None and not getattr(prices_df, "empty", True):
            w = ens.get("weights", {'xsec':0.6,'ts':0.2,'breakout':0.2})
            ts_len = int(ens.get("ts_len", 48))
            br_len = int(ens.get("breakout_len", 96))
            out = {}
            for sym in zscores.index:
                try:
                    p = prices_df[sym].dropna()
                    r = p.pct_change().dropna()
                    ts_s = _compute_ts_sign(r, ts_len)
                    br_s = _compute_breakout_score(p, br_len)
                    out[sym] = compute_ensemble_score(float(zscores.loc[sym]), ts_s, br_s, w)
                except Exception:
                    out[sym] = float(zscores.loc[sym])
            zscores = pd.Series(out).reindex(zscores.index).astype(float)
            meta["ensemble_applied"] = True
    except Exception:
        pass

    # ---- Funding Trim ----
    try:
        st = (cfg or {}).get("strategy", {}) or {}
        ft = st.get("funding_trim", {}) or {}
        if bool(ft.get("enabled", False)) and next_funding_bps is not None:
            # same semantics as apply_funding_trim helper
            thr = float(ft.get("threshold_bps", 1.8))
            slope = float(ft.get("slope_per_bps", 0.15))
            max_red = float(ft.get("max_reduction", 0.5))
            s = pd.Series(zscores).astype(float)
            f = pd.Series(next_funding_bps).astype(float).reindex(s.index).fillna(0.0)
            adversity = pd.Series(0.0, index=s.index, dtype=float)
            adversity[(s > 0) & (f > thr)] = (f[(s > 0) & (f > thr)] - thr)
            adversity[(s < 0) & (f < -thr)] = (abs(f[(s < 0) & (f < -thr)]) - thr)
            red = 1.0 - slope * adversity.clip(lower=0.0)
            red = red.clip(lower=max(1.0 - max_red, 0.0), upper=1.0)
            zscores = (s * red).astype(float)
            meta["funding_trim_applied"] = True
    except Exception:
        pass

    # ---- Avg pairwise corr for gating ----
    avg_corr = None
    try:
        if prices_df is not None and not getattr(prices_df, "empty", True):
            # use the helper if present
            try:
                avg_corr = compute_avg_pair_corr(prices_df, lookback=int(corr_lookback))
            except Exception:
                # fallback quick corr
                px = prices_df.ffill().bfill()
                rets = np.log(px/px.shift(1)).tail(int(corr_lookback))
                if rets.shape[0] >= max(10, int(corr_lookback/3)):
                    c = rets.corr().values
                    n = c.shape[0]
                    tri = [c[i,j] for i in range(n) for j in range(i+1, n)]
                    avg_corr = float(np.mean(tri)) if tri else None
    except Exception:
        avg_corr = None
    meta["avg_pair_corr"] = None if avg_corr is None else float(avg_corr)

    # ---- Entry/breadth gating ----
    zscores_ready, gate_meta = gate_zscores_pipeline(zscores, cfg, avg_pair_corr=avg_corr, atr_pct=atr_pct)
    meta.update({k:v for k,v in gate_meta.items() if k not in meta})
    return zscores_ready, meta


def _compute_ts_sign(returns: pd.Series, length: int = 48) -> float:
    """
    Simple time-series filter: sign of EMA of returns over `length`.
    Returns:
        +1.0 if the EMA of returns > 0,
        -1.0 if the EMA of returns < 0,
         0.0 if insufficient data or NaN.
    """
    try:
        r = returns.dropna()
        if r.shape[0] < max(5, int(length // 2)):
            return 0.0
        ema = r.ewm(span=int(length), adjust=False).mean().iloc[-1]
        if not np.isfinite(ema):
            return 0.0
        return 1.0 if ema > 0 else (-1.0 if ema < 0 else 0.0)
    except Exception:
        return 0.0


def _compute_breakout_score(prices: pd.Series, length: int = 96) -> float:
    """
    Donchian-style breakout score in [-1, +1]:
    +1 => near upper channel, -1 => near lower channel, 0 => middle.
    """
    try:
        p = prices.dropna()
        if p.shape[0] < int(length):
            return 0.0
        window = p.tail(int(length))
        lo, hi, last = float(window.min()), float(window.max()), float(window.iloc[-1])
        if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
            return 0.0
        pos = (last - lo) / (hi - lo)            # 0..1
        return float(pos * 2.0 - 1.0)            # -1..+1
    except Exception:
        return 0.0


def compute_ensemble_score(xsec_z: float, ts_sign: float, breakout: float, weights: dict | None) -> float:
    """
    Linear blend of components with weights {'xsec','ts','breakout'}.
    Output is a blended score; caller can re-normalize across assets if desired.
    """
    try:
        w = weights or {'xsec': 0.6, 'ts': 0.2, 'breakout': 0.2}
        return float(w.get('xsec',0.6))*xsec_z + float(w.get('ts',0.2))*ts_sign + float(w.get('breakout',0.2))*breakout
    except Exception:
        return xsec_z



# =============================
# Funding Trim (Carry Edge 2.0)
# =============================
def apply_funding_trim(raw_scores: "pd.Series", next_funding_bps: "pd.Series", cfg) -> "pd.Series":
    """
    Down-weight names whose expected next funding is strongly adverse to the sign of the score.
    Config:
      strategy.funding_trim.enabled: bool
      strategy.funding_trim.threshold_bps: float (e.g., 1.8)
      strategy.funding_trim.slope_per_bps: float (e.g., 0.15)   # reduction per bps over threshold
      strategy.funding_trim.max_reduction: float (e.g., 0.5)    # max multiplicative reduction
    Returns scaled scores (same index).
    """
    import pandas as pd, numpy as np
    try:
        st = getattr(cfg, "strategy", None)
        ft = getattr(st, "funding_trim", None) if st else None
        if not (ft and getattr(ft, "enabled", False)):
            return raw_scores

        thr = float(getattr(ft, "threshold_bps", 1.8))
        slope = float(getattr(ft, "slope_per_bps", 0.15))
        max_red = float(getattr(ft, "max_reduction", 0.5))

        s = pd.Series(raw_scores).astype(float)
        f = pd.Series(next_funding_bps).astype(float)
        # Adversity: long-bias (score>0) + positive funding; short-bias (score<0) + negative funding
        adversity = pd.Series(0.0, index=s.index, dtype=float)
        adversity[(s > 0) & (f > thr)] = (f[(s > 0) & (f > thr)] - thr)
        adversity[(s < 0) & (f < -thr)] = (abs(f[(s < 0) & (f < -thr)]) - thr)

        reduction = 1.0 - slope * adversity.clip(lower=0.0)
        reduction = reduction.clip(lower=max(1.0 - max_red, 0.0), upper=1.0)
        return (s * reduction).astype(float)
    except Exception:
        return raw_scores


def compute_conviction_scores(closes, cfg, next_funding_bps=None):
    import pandas as pd, numpy as np
    if closes is None or getattr(closes, "empty", True):
        return pd.Series(dtype="float64")
    px = closes.ffill().bfill()
    rets = np.log(px/px.shift(1))
    last = rets.iloc[-1:].T[rets.index[-1]]
    mu, sd = float(last.mean()), float(last.std() or 1e-9)
    z = ((last - mu) / sd).astype(float)
    ens = getattr(getattr(cfg, "strategy", None), "ensemble", None)
    if ens and getattr(ens, "enabled", False):
        w = getattr(ens, "weights", {'xsec':0.6,'ts':0.2,'breakout':0.2})
        ts_len = int(getattr(ens, "ts_len", 48))
        br_len = int(getattr(ens, "breakout_len", 96))
        out = {}
        for sym in z.index:
            p = closes[sym]
            r = p.pct_change().dropna()
            ts_s = _compute_ts_sign(r, ts_len) if "_compute_ts_sign" in globals() else 0.0
            br_s = _compute_breakout_score(p, br_len) if "_compute_breakout_score" in globals() else 0.0
            base = float(z.loc[sym])
            try:
                out[sym] = float(w.get('xsec',0.6))*base + float(w.get('ts',0.2))*ts_s + float(w.get('breakout',0.2))*br_s
            except Exception:
                out[sym] = base
        z = pd.Series(out).reindex(z.index).astype(float)
    # Funding trim if available
    try:
        ft = getattr(getattr(cfg, "strategy", None), "funding_trim", None)
        if next_funding_bps is not None and ft and getattr(ft, "enabled", False):
            thr = float(getattr(ft, "threshold_bps", 1.8))
            slope = float(getattr(ft, "slope_per_bps", 0.15))
            max_red = float(getattr(ft, "max_reduction", 0.5))
            f = pd.Series(next_funding_bps).astype(float).reindex(z.index).fillna(0.0)
            adv = pd.Series(0.0, index=z.index, dtype=float)
            adv[(z > 0) & (f > thr)] = (f[(z > 0) & (f > thr)] - thr)
            adv[(z < 0) & (f < -thr)] = (abs(f[(z < 0) & (f < -thr)]) - thr)
            red = 1.0 - slope * adv.clip(lower=0.0)
            red = red.clip(lower=max(1.0 - max_red, 0.0), upper=1.0)
            z = (z * red).astype(float)
    except Exception:
        pass
    # Dynamic gating if available
    try:
        avg_corr = compute_avg_pair_corr(closes, lookback=int(getattr(getattr(cfg.strategy, "diversify", None), "corr_lookback", 48)))
    except Exception:
        avg_corr = None
    try:
        if "gate_zscores_pipeline" in globals():
            z, _meta = gate_zscores_pipeline(z, cfg if hasattr(cfg, "model_dump") else getattr(cfg, "__dict__", cfg), avg_pair_corr=avg_corr)
    except Exception:
        pass
    return z




# =============================
# Lightweight Online Meta-Labeler (SGD Logistic Regression)
# =============================
from dataclasses import dataclass, field

@dataclass
class _MetaCfg:
    enabled: bool = True
    min_prob: float = 0.55
    learning_rate: float = 0.05
    l2: float = 1e-3
    feature_names: list[str] = field(default_factory=lambda: ["abs_z","sign","vol_look","breakout","funding_bps"])
    state_path: str = "meta_label_state.json"

class _OnlineMetaLabeler:
    def __init__(self, cfg: _MetaCfg):
        self.cfg = cfg
        self.w: dict[str, np.ndarray] = {}
        self.b: dict[str, float] = {}
        self.n = len(cfg.feature_names)
        # lazy load on first update/predict
        try:
            import json, os
            if os.path.exists(cfg.state_path):
                data = json.load(open(cfg.state_path, "r"))
                self.w = {k: np.array(v, dtype=float) for k, v in data.get("w", {}).items()}
                self.b = {k: float(v) for k, v in data.get("b", {}).items()}
        except Exception:
            pass

    def _save(self):
        try:
            import json
            json.dump({"w": {k: v.tolist() for k, v in self.w.items()}, "b": self.b}, open(self.cfg.state_path, "w"))
        except Exception:
            pass

    def _ensure(self, sym: str):
        if sym not in self.w:
            self.w[sym] = np.zeros(self.n, dtype=float)
            self.b[sym] = 0.0

    def _sigmoid(self, x: float) -> float:
        return float(1.0 / (1.0 + np.exp(-x)))

    def predict(self, X: pd.DataFrame) -> pd.Series:
        probs = {}
        for sym, row in X.iterrows():
            self._ensure(sym)
            z = float(np.dot(self.w[sym], row.values) + self.b[sym])
            probs[sym] = self._sigmoid(z)
        return pd.Series(probs).reindex(X.index)

    def update(self, X: pd.DataFrame, y: pd.Series):
        lr = float(self.cfg.learning_rate)
        l2 = float(self.cfg.l2)
        for sym, row in X.iterrows():
            if sym not in y.index:
                continue
            target = float(y.loc[sym])
            self._ensure(sym)
            z = float(np.dot(self.w[sym], row.values) + self.b[sym])
            p = self._sigmoid(z)
            grad_w = (p - target) * row.values + l2 * self.w[sym]
            grad_b = (p - target)
            self.w[sym] = self.w[sym] - lr * grad_w
            self.b[sym] = self.b[sym] - lr * grad_b
        self._save()

def _build_meta_features(
    zscores: pd.Series,
    closes: pd.DataFrame | None = None,
    next_funding_bps: pd.Series | None = None,
    breakout_len: int = 96,
    vol_len: int = 48,
) -> pd.DataFrame:
    idx = list(zscores.index)
    feats = pd.DataFrame(index=idx, columns=["abs_z","sign","vol_look","breakout","funding_bps"], dtype=float)
    feats["abs_z"] = zscores.abs().astype(float)
    feats["sign"]  = np.sign(zscores).astype(float)
    feats["funding_bps"] = (pd.Series(next_funding_bps).reindex(idx).astype(float).fillna(0.0)
                            if next_funding_bps is not None else 0.0)
    if closes is not None and not getattr(closes, "empty", True):
        px = closes.reindex(columns=idx).ffill().bfill()
        r = np.log(px/px.shift(1))
        vol = r.rolling(int(vol_len)).std().iloc[-1].reindex(idx).astype(float)
        feats["vol_look"] = vol.fillna(float(np.nanmedian(vol))) if len(vol.dropna()) else 0.0
        wind = int(breakout_len)
        hi = px.tail(wind).max()
        lo = px.tail(wind).min()
        last = px.tail(1).iloc[0]
        denom = (hi - lo).replace(0, np.nan)
        br = ((last - lo) / denom * 2.0 - 1.0).clip(-1,1).reindex(idx)
        feats["breakout"] = br.fillna(0.0).values
    else:
        feats["vol_look"] = 0.0
        feats["breakout"] = 0.0
    return feats.fillna(0.0).astype(float)

def _filter_by_meta(zscores: pd.Series, cfg: dict, *, closes: pd.DataFrame | None, next_funding_bps: pd.Series | None) -> tuple[pd.Series, dict]:
    st = (cfg or {}).get("strategy", {}) or {}
    ml = st.get("meta_label", {}) or {}
    meta = {"meta_label_applied": False}
    if not bool(ml.get("enabled", False)):
        return zscores, meta
    feats = _build_meta_features(
        zscores,
        closes=closes,
        next_funding_bps=next_funding_bps,
        breakout_len=int(ml.get("breakout_len", 96)),
        vol_len=int(ml.get("vol_len", 48)),
    )
    model = _OnlineMetaLabeler(_MetaCfg(
        enabled=True,
        min_prob=float(ml.get("min_prob", 0.55)),
        learning_rate=float(ml.get("learning_rate", 0.05)),
        l2=float(ml.get("l2", 1e-3)),
        feature_names=list(feats.columns),
        state_path=str(ml.get("state_path", "meta_label_state.json"))
    ))
    p = model.predict(feats)
    thr = float(ml.get("min_prob", 0.55))
    mask = (p >= thr)
    z = zscores.copy().astype(float)
    z.loc[~mask.reindex(z.index).fillna(False)] = 0.0
    meta["meta_label_applied"] = True
    meta["kept"] = int(mask.sum())
    meta["blocked"] = int((~mask).sum())
    return z, meta