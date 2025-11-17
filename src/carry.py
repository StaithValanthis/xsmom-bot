# =========================
# XSMOM-BOT — carry.py
# Carry/Basis sleeve:
#  - Perp funding carry (delta-neutral if hedge leg available; safe directional fallback otherwise)
#  - Dated futures cash-and-carry (basis) when futures+spot available
#  - Strict hurdles, spread/depth gates, per-symbol & gross caps
#  - Combiner to blend momentum & carry sleeves under a budget split
# =========================

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
import math
import logging
import numpy as np
import pandas as pd

log = logging.getLogger("carry")

# ---------- Config dataclasses (pure-Python, no pydantic) ----------

@dataclass
class FundingCfg:
    enabled: bool = True
    intervals_for_mean: int = 6
    min_entry_apy: float = 0.15
    min_exit_apy: float = 0.08
    min_percentile_30d: float = 0.80
    sign_stability_k: int = 5
    spread_gate_bps: float = 10.0
    depth_usd_min: float = 2000.0
    hedge_preference: List[str] = field(default_factory=lambda: ["spot", "dated_future", "perp_proxy"])

@dataclass
class BasisCfg:
    enabled: bool = True
    min_entry_apy: float = 0.15
    min_exit_apy: float = 0.08
    roll_threshold_days: int = 5
    dte_min_days: int = 10
    spread_gate_bps: float = 10.0
    depth_usd_min: float = 3000.0
    hedge_preference: List[str] = field(default_factory=lambda: ["spot", "perp"])

@dataclass
class CarryCfg:
    enabled: bool = True
    budget_frac: float = 0.35
    per_symbol_notional_cap_pct: float = 0.20
    max_weight_per_asset: float = 0.12
    gross_leverage: float = 0.90
    allow_directional_fallback: bool = True
    fallback_weight_scale: float = 0.35
    funding: FundingCfg = field(default_factory=FundingCfg)
    basis: BasisCfg = field(default_factory=BasisCfg)

# ---------- Helpers ----------

def _safe_get(d: dict, path: str, default=None):
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur

def parse_carry_cfg(cfg_dict: dict) -> CarryCfg:
    """Parse nested dict under strategy.carry into CarryCfg with defaults."""
    c = _safe_get(cfg_dict, "strategy.carry", {}) or {}
    funding = _safe_get(c, "funding", {}) or {}
    basis = _safe_get(c, "basis", {}) or {}
    return CarryCfg(
        enabled=bool(c.get("enabled", True)),
        budget_frac=float(c.get("budget_frac", 0.35)),
        per_symbol_notional_cap_pct=float(c.get("per_symbol_notional_cap_pct", 0.20)),
        max_weight_per_asset=float(c.get("max_weight_per_asset", 0.12)),
        gross_leverage=float(c.get("gross_leverage", 0.90)),
        allow_directional_fallback=bool(c.get("allow_directional_fallback", True)),
        fallback_weight_scale=float(c.get("fallback_weight_scale", 0.35)),
        funding=FundingCfg(
            enabled=bool(funding.get("enabled", True)),
            intervals_for_mean=int(funding.get("intervals_for_mean", 6)),
            min_entry_apy=float(funding.get("min_entry_apy", 0.15)),
            min_exit_apy=float(funding.get("min_exit_apy", 0.08)),
            min_percentile_30d=float(funding.get("min_percentile_30d", 0.80)),
            sign_stability_k=int(funding.get("sign_stability_k", 5)),
            spread_gate_bps=float(funding.get("spread_gate_bps", 10)),
            depth_usd_min=float(funding.get("depth_usd_min", 2000)),
            hedge_preference=list(funding.get("hedge_preference", ["spot", "dated_future", "perp_proxy"])),
        ),
        basis=BasisCfg(
            enabled=bool(basis.get("enabled", True)),
            min_entry_apy=float(basis.get("min_entry_apy", 0.15)),
            min_exit_apy=float(basis.get("min_exit_apy", 0.08)),
            roll_threshold_days=int(basis.get("roll_threshold_days", 5)),
            dte_min_days=int(basis.get("dte_min_days", 10)),
            spread_gate_bps=float(basis.get("spread_gate_bps", 10)),
            depth_usd_min=float(basis.get("depth_usd_min", 3000)),
            hedge_preference=list(basis.get("hedge_preference", ["spot", "perp"])),
        ),
    )

def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))

def _renorm_gross(weights: pd.Series, gross: float) -> pd.Series:
    s = float(np.sum(np.abs(weights.values))) + 1e-12
    if s == 0:
        return weights
    return weights * (gross / s)

def _cap_per_asset(weights: pd.Series, cap: float) -> pd.Series:
    return weights.clip(lower=-cap, upper=cap)

# ---------- Venue capability checks (best-effort, non-fatal) ----------

def _has_spot(ex) -> bool:
    try:
        return bool(getattr(ex, "has_spot", False))
    except Exception:
        return False

def _has_dated_futures(ex) -> bool:
    try:
        return bool(getattr(ex, "has_dated_futures", False))
    except Exception:
        return False

def _fetch_funding_history(ex, symbol: str, limit: int = 6) -> Optional[List[float]]:
    """
    Best-effort funding history fetch.
    Expected to return list of recent funding rates (decimal, e.g., 0.0001 per 8h).
    """
    fn = getattr(ex, "fetch_funding_history", None) or getattr(ex, "fetch_funding_rate_history", None)
    if fn is None:
        return None
    try:
        hist = fn(symbol=symbol, limit=limit)  # implement in your ExchangeWrapper
        # accept iterable of dicts or floats
        if len(hist) == 0:
            return None
        if isinstance(hist[0], dict):
            # look for 'fundingRate' or 'rate'
            out = []
            for r in hist:
                v = r.get("fundingRate", r.get("rate", None))
                if v is None:
                    return None
                out.append(float(v))
            return out
        else:
            return [float(x) for x in hist]
    except Exception as e:
        log.debug(f"funding fetch failed for {symbol}: {e}")
        return None

def _fetch_spot_borrow_cost_apy(ex, symbol: str) -> Optional[float]:
    """Optional: if your wrapper exposes borrow APR/APY, return it; else None."""
    fn = getattr(ex, "get_spot_borrow_cost_apy", None)
    if fn is None:
        return None
    try:
        return float(fn(symbol))
    except Exception:
        return None

# ---------- Funding-carry sleeve ----------

def _funding_apy_from_series(frates: List[float], intervals_per_day: int = 3) -> float:
    """Approx APY from mean funding rate per interval (e.g., 8h → 3/day)."""
    if frates is None or len(frates) == 0:
        return 0.0
    mu = float(np.mean(frates))
    return mu * intervals_per_day * 365.0

def _sign_stability_ok(frates: List[float], k_needed: int) -> bool:
    if frates is None or len(frates) == 0:
        return False
    signs = [1 if x > 0 else (-1 if x < 0 else 0) for x in frates]
    # take last k samples
    k = min(len(signs), max(1, k_needed))
    last = signs[-k:]
    # one sign dominates?
    pos = sum(1 for s in last if s > 0)
    neg = sum(1 for s in last if s < 0)
    return (pos >= k_needed) or (neg >= k_needed)

def _pass_micro_gates(spread_bps: float, depth_usd: float, cfg_spread_gate: float, cfg_depth_min: float) -> bool:
    if spread_bps is None or depth_usd is None:
        return False
    return (spread_bps <= cfg_spread_gate) and (depth_usd >= cfg_depth_min)

def build_funding_carry_weights(
    ex,
    universe: List[str],
    equity: float,
    cfg: CarryCfg,
    spread_bps_map: Dict[str, float],
    depth_usd_map: Dict[str, float],
    percentile_30d_map: Dict[str, float],  # funding percentile in last 30d [0..1]
) -> Tuple[pd.Series, Dict[str, Dict]]:
    """
    Returns:
      - weights: pd.Series indexed by symbols (perp IDs). Positive=long perp, negative=short perp.
        If delta-neutral hedge is available, the hedge leg is NOT returned here; see notes below.
      - meta: per-symbol diagnostics (funding_apy, mode, reason)
    Notes:
      * If spot/dated futures available, you should open the hedge leg via your execution layer.
      * If only perps are available and allow_directional_fallback=True, we take a SMALL directional tilt.
    """
    w = pd.Series(0.0, index=pd.Index(universe, dtype=str), name="carry_funding")
    meta: Dict[str, Dict] = {}

    have_spot = _has_spot(ex)
    have_futs = _has_dated_futures(ex)

    for sym in universe:
        fr_hist = _fetch_funding_history(ex, sym, limit=max(6, cfg.funding.intervals_for_mean))
        if not fr_hist:
            meta[sym] = {"chosen": False, "reason": "no_funding_history"}
            continue

        apy = _funding_apy_from_series(fr_hist)
        perc = float(percentile_30d_map.get(sym, 0.0))
        if not _sign_stability_ok(fr_hist, cfg.funding.sign_stability_k):
            meta[sym] = {"chosen": False, "funding_apy": apy, "reason": "sign_instability"}
            continue

        sp = spread_bps_map.get(sym)
        dp = depth_usd_map.get(sym)
        if not _pass_micro_gates(sp, dp, cfg.funding.spread_gate_bps, cfg.funding.depth_usd_min):
            meta[sym] = {"chosen": False, "funding_apy": apy, "reason": "micro_gate"}
            continue

        # Entry/exit
        if apy < cfg.funding.min_entry_apy or perc < cfg.funding.min_percentile_30d:
            meta[sym] = {"chosen": False, "funding_apy": apy, "reason": "below_hurdle"}
            continue

        # Determine direction: receive funding
        # funding > 0: shorts receive; funding < 0: longs receive
        dir_sign = -1.0 if apy > 0 else (1.0 if apy < 0 else 0.0)
        if dir_sign == 0.0:
            meta[sym] = {"chosen": False, "funding_apy": apy, "reason": "zero_funding"}
            continue

        # Decide if we can go delta-neutral
        can_delta_neutral = False
        hedge_mode = None
        for pref in cfg.funding.hedge_preference:
            if pref == "spot" and have_spot:
                can_delta_neutral = True
                hedge_mode = "spot"
                break
            if pref == "dated_future" and have_futs:
                can_delta_neutral = True
                hedge_mode = "dated_future"
                break
            if pref == "perp_proxy":
                # Only if you have a cross-venue or cross-contract hedge (rare)
                if hasattr(ex, "has_perp_proxy_hedge") and ex.has_perp_proxy_hedge:
                    can_delta_neutral = True
                    hedge_mode = "perp_proxy"
                    break

        # Weight sizing inside the carry sleeve
        base_w = min(cfg.max_weight_per_asset, cfg.per_symbol_notional_cap_pct)  # keep conservative

        if can_delta_neutral:
            # We only return the perp leg here (execution layer should open hedge leg).
            w.loc[sym] = dir_sign * base_w
            meta[sym] = {"chosen": True, "funding_apy": apy, "mode": f"delta_neutral_{hedge_mode}"}
        else:
            if not cfg.allow_directional_fallback:
                meta[sym] = {"chosen": False, "funding_apy": apy, "reason": "no_hedge_available"}
                continue
            # Small directional tilt fallback
            w.loc[sym] = dir_sign * (base_w * cfg.fallback_weight_scale)
            meta[sym] = {"chosen": True, "funding_apy": apy, "mode": "directional_fallback"}

    # Intra-sleeve gross normalization + per-asset cap
    w = _cap_per_asset(w, cfg.max_weight_per_asset)
    w = _renorm_gross(w, cfg.gross_leverage)

    # Apply per-symbol notional cap using equity
    cap_notional = cfg.per_symbol_notional_cap_pct * float(equity)
    if cap_notional > 0:
        for sym in w.index:
            notion = abs(float(w.loc[sym])) * float(equity)
            if notion > cap_notional > 0:
                w.loc[sym] *= (cap_notional / notion)

        w = _cap_per_asset(_renorm_gross(w, cfg.gross_leverage), cfg.max_weight_per_asset)

    return w, meta

# ---------- Basis (dated futures) sleeve (lightweight stub; relies on venue data) ----------

def _basis_apy(F: float, S: float, dte_days: float) -> float:
    if S <= 0 or dte_days <= 0:
        return 0.0
    return ((F / S) - 1.0) * (365.0 / dte_days)

def build_basis_carry_weights(
    ex,
    universe_spot: List[str],
    equity: float,
    cfg: CarryCfg,
    futs_quotes: Dict[str, Dict[str, float]],  # {symbol: {'F': price, 'S': spot, 'dte_days': d}}
) -> Tuple[pd.Series, Dict[str, Dict]]:
    """
    Returns:
      - weights for the futures leg only (hedge leg should be opened by execution layer)
      - meta diagnostics
    Notes:
      - Requires spot S, futures F, and DTE per symbol (provided via futs_quotes).
      - Skips symbols without valid quotes or below hurdle/roll thresholds.
    """
    w = pd.Series(0.0, index=pd.Index(universe_spot, dtype=str), name="carry_basis")
    meta: Dict[str, Dict] = {}
    if not cfg.basis.enabled or futs_quotes is None:
        return w, meta

    for sym in universe_spot:
        q = futs_quotes.get(sym)
        if not q:
            meta[sym] = {"chosen": False, "reason": "no_quote"}
            continue
        F, S, dte = float(q.get("F", 0.0)), float(q.get("S", 0.0)), float(q.get("dte_days", 0.0))
        if dte < cfg.basis.dte_min_days:
            meta[sym] = {"chosen": False, "reason": "short_dte"}
            continue

        apy = _basis_apy(F, S, dte)
        if apy < cfg.basis.min_entry_apy:
            meta[sym] = {"chosen": False, "basis_apy": apy, "reason": "below_hurdle"}
            continue

        # Direction: contango (F>S) → short future / long spot; backwardation → long future / short spot
        dir_sign = -1.0 if apy > 0 else (1.0 if apy < 0 else 0.0)
        if dir_sign == 0.0:
            meta[sym] = {"chosen": False, "basis_apy": apy, "reason": "flat_basis"}
            continue

        base_w = min(cfg.max_weight_per_asset, cfg.per_symbol_notional_cap_pct)
        w.loc[sym] = dir_sign * base_w
        meta[sym] = {"chosen": True, "basis_apy": apy, "mode": "delta_neutral_spot_hedge"}

    w = _cap_per_asset(w, cfg.max_weight_per_asset)
    w = _renorm_gross(w, cfg.gross_leverage)

    cap_notional = cfg.per_symbol_notional_cap_pct * float(equity)
    if cap_notional > 0:
        for sym in w.index:
            notion = abs(float(w.loc[sym])) * float(equity)
            if notion > cap_notional > 0:
                w.loc[sym] *= (cap_notional / notion)
        w = _cap_per_asset(_renorm_gross(w, cfg.gross_leverage), cfg.max_weight_per_asset)

    return w, meta

# ---------- Sleeve combiner ----------

def combine_sleeves(
    w_momentum: pd.Series,
    w_carry: pd.Series,
    carry_budget_frac: float,
    total_gross_leverage: float,
    per_asset_cap: float
) -> pd.Series:
    """
    Blend sleeves by budget fraction on gross, then enforce overall caps and renorm to total gross.
    - carry gets 'carry_budget_frac' of gross, momentum gets the rest.
    """
    carry_budget_frac = _clip(float(carry_budget_frac), 0.0, 0.95)
    mom_budget_frac = 1.0 - carry_budget_frac

    # Scale sleeves to their budgeted gross
    w_c = _renorm_gross(w_carry.fillna(0.0), carry_budget_frac * total_gross_leverage)
    w_m = _renorm_gross(w_momentum.fillna(0.0), mom_budget_frac * total_gross_leverage)

    # Align index union, sum
    all_syms = w_c.index.union(w_m.index)
    w_c = w_c.reindex(all_syms).fillna(0.0)
    w_m = w_m.reindex(all_syms).fillna(0.0)
    w = w_c + w_m

    # Final per-asset cap and renorm to total gross
    w = _cap_per_asset(w, per_asset_cap)
    w = _renorm_gross(w, total_gross_leverage)
    return w


# === PATCH: adaptive carry budget ===
def adaptive_carry_budget(pct_apys, sign_stability, base: float = 0.20) -> float:
    import numpy as _np
    try:
        if not pct_apys:
            return float(base)
        p = float(_np.nanmedian(_np.array(pct_apys, dtype=float)))
        s = float(max(0.0, min(1.0, sign_stability)))
        score = 0.6 * p + 0.4 * s
        return float(max(0.05, min(0.35, 0.05 + 0.30 * score)))
    except Exception:
        return float(base)
