#!/usr/bin/env python3
import argparse, json, os, shutil, subprocess, tempfile, sys, itertools, glob
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Tuple, Optional, Iterable
import yaml
from pathlib import Path
import csv

WD = "/opt/xsmom-bot"  # repo root

# ---------------- I/O helpers ----------------
def load_cfg(path: str) -> Dict[str, Any]:
    with open(path, "r") as f:
        return yaml.safe_load(f) or {}

def dump_cfg(obj: Dict[str, Any], path: str) -> None:
    with open(path, "w") as f:
        yaml.safe_dump(obj, f, sort_keys=False)

def deep_get(d: Dict[str, Any], path: str, default=None):
    cur = d
    for key in path.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur

def deep_set(d: Dict[str, Any], path: str, value) -> None:
    cur = d
    keys = path.split(".")
    for k in keys[:-1]:
        if k not in cur or not isinstance(cur[k], dict):
            cur[k] = {}
        cur = cur[k]
    cur[keys[-1]] = value

def write_tmp_cfg(base_cfg: Dict[str, Any], overrides: Dict[str, Any]) -> str:
    cfg = deepcopy(base_cfg)
    # apply overrides
    for k, v in overrides.items():
        deep_set(cfg, k, v)
    tf = tempfile.NamedTemporaryFile(delete=False, suffix=".yaml")
    tf.close()
    dump_cfg(cfg, tf.name)
    return tf.name

# ---------------- backtest runner ----------------
def run_bt(cfg_path: str) -> Dict[str, Any]:
    """
    Run hardened backtest CLI and return its metrics dict.
    Writes JSON to a temp file to avoid parsing stdout logs.
    Uses sys.executable (or $PYTHON_BIN if set) so systemd PATH is irrelevant.
    """
    tf = tempfile.NamedTemporaryFile(delete=False, suffix=".json"); tf_path = tf.name; tf.close()
    PY = os.environ.get("PYTHON_BIN", sys.executable)
    print(f"[optimizer-runner] interpreter={PY}", flush=True)
    try:
        cmd = [PY, "-m", "src.backtest_cli", "--config", cfg_path, "--json", tf_path]
        print(f"[optimizer-runner] cmd={' '.join(cmd)}", flush=True)
        p = subprocess.run(cmd, cwd=WD, capture_output=True, text=True)
        if p.returncode != 0:
            err = (p.stderr or "").strip(); out = (p.stdout or "").strip()
            raise RuntimeError(f"backtest_cli failed (rc={p.returncode})\nSTDERR:\n{err}\nSTDOUT:\n{out}")
        if not os.path.exists(tf_path) or os.path.getsize(tf_path) == 0:
            s = p.stdout or ""; i = s.rfind("{"); j = s.rfind("}")
            if i != -1 and j != -1 and j > i:
                return json.loads(s[i:j+1])
            raise RuntimeError("backtest_cli produced no JSON output file and no parseable JSON in stdout.")
        with open(tf_path, "r") as f:
            return json.load(f)
    finally:
        try: os.unlink(tf_path)
        except Exception: pass

# ---------------- scoring/constraints ----------------
def scalar(d: Dict[str, Any], key: str, default=0.0) -> float:
    try: return float(d.get(key, default))
    except Exception: return float(default)

def score(metrics: Dict[str, Any], objective: str="sharpe") -> Tuple[float, float]:
    if objective == "calmar":
        primary = scalar(metrics, "calmar", 0.0); tie = scalar(metrics, "sharpe", 0.0)
    elif objective == "return":
        primary = scalar(metrics, "annualized", 0.0); tie = scalar(metrics, "sharpe", 0.0)
    else:
        primary = scalar(metrics, "sharpe", 0.0); tie = scalar(metrics, "calmar", 0.0)
    return (primary, tie)

def valid_under_constraints(metrics: Dict[str, Any],
                            base_metrics: Dict[str, Any],
                            require_no_worse_mdd: bool=False,
                            max_turnover_per_year: Optional[float]=None) -> bool:
    if require_no_worse_mdd:
        if metrics.get("max_drawdown") is None or base_metrics.get("max_drawdown") is None:
            return False
        if float(metrics["max_drawdown"]) < float(base_metrics["max_drawdown"]):
            return False
    if max_turnover_per_year is not None:
        gty = metrics.get("gross_turnover_per_year")
        if gty is None or float(gty) > float(max_turnover_per_year):
            return False
    return True

# ---------------- Phase 1 (existing) ----------------
def phase1_grid_from_cfg(cfg: Dict[str, Any], cap=64) -> List[Dict[str, Any]]:
    strat = (cfg.get("strategy") or {})
    sel   = (strat.get("selection") or {})
    topk0 = int(sel.get("top_k") or strat.get("k") or strat.get("k_max") or 4)
    kmin0 = int(strat.get("k_min") or max(1, topk0 // 2))
    kmax0 = int(strat.get("k_max") or max(topk0, kmin0 + 1))
    kap0  = float(sel.get("kappa") or 1.0)
    vol0  = int(strat.get("vol_lookback") or 72)

    topk_vals = sorted({max(1, topk0-2), topk0, topk0+2, topk0+4})
    kmin_vals = sorted({max(1, kmin0-1), kmin0})
    kmax_vals = sorted({max(kmin0+1, kmax0-1), kmax0, kmax0+1})
    kap_vals  = sorted({round(kap0*0.8,3), kap0, round(kap0*1.2,3)})
    vol_vals  = sorted({max(12, vol0-24), vol0, vol0+24})

    combos = []
    for top_k, kmin, kmax, kappa, vol in itertools.product(topk_vals, kmin_vals, kmax_vals, kap_vals, vol_vals):
        if kmin <= top_k <= kmax:
            combos.append({
                "strategy.selection.top_k": int(top_k),
                "strategy.selection.kappa": float(kappa),
                "strategy.k_min": int(kmin),
                "strategy.k_max": int(kmax),
                "strategy.vol_lookback": int(vol)
            })
    return combos[:cap]

# ---------------- Phase 2 (base groups) ----------------
def _present(cfg: Dict[str, Any], path: str) -> bool:
    """Return True if a path exists in cfg (so we only tune what's present)."""
    return deep_get(cfg, path, None) is not None

def phase2_groups_from_cfg(cfg: Dict[str, Any]) -> List[Tuple[str, List[Dict[str, Any]]]]:
    groups: List[Tuple[str, List[Dict[str, Any]]]] = []

    # 1) Signal shape
    if _present(cfg, "strategy.signal_power"):
        base = float(deep_get(cfg, "strategy.signal_power", 1.35))
        vals = sorted({1.0, 1.25, 1.5, 1.75, 2.0, round(base, 3)})
        groups.append(("signal_power", [{"strategy.signal_power": v} for v in vals]))
    if _present(cfg, "strategy.entry_zscore_min"):
        base = float(deep_get(cfg, "strategy.entry_zscore_min", 0.45))
        vals = sorted({0.3, 0.45, 0.6, 0.75, base})
        groups.append(("entry_zscore_min", [{"strategy.entry_zscore_min": v} for v in vals]))

    # 2) Vol targeting
    if _present(cfg, "strategy.portfolio_vol_target") or _present(cfg, "strategy.portfolio_vol_target.target") or _present(cfg, "strategy.portfolio_vol_target.target_ann_vol"):
        base_t = float(deep_get(cfg, "strategy.portfolio_vol_target.target", deep_get(cfg, "strategy.portfolio_vol_target.target_ann_vol", 0.35)))
        tvals  = sorted({0.20, 0.30, 0.35, 0.40, 0.45, 0.50, round(base_t, 3)})
        cand_t = []
        for v in tvals:
            cand_t.append({
                "strategy.portfolio_vol_target.enabled": True,
                "strategy.portfolio_vol_target.target": float(v),
                "strategy.portfolio_vol_target.target_ann_vol": float(v),
            })
        groups.append(("portfolio_vol_target.target", cand_t))

        base_lb = int(deep_get(cfg, "strategy.portfolio_vol_target.lookback", deep_get(cfg, "strategy.portfolio_vol_target.lookback_hours", 72)))
        lbvals  = sorted({24, 48, 72, 120, 168, int(base_lb)})
        cand_lb = []
        for v in lbvals:
            cand_lb.append({
                "strategy.portfolio_vol_target.enabled": True,
                "strategy.portfolio_vol_target.lookback": int(v),
                "strategy.portfolio_vol_target.lookback_hours": int(v),
            })
        groups.append(("portfolio_vol_target.lookback", cand_lb))

    # 3) Risk concentration
    if _present(cfg, "strategy.gross_leverage"):
        base = float(deep_get(cfg, "strategy.gross_leverage", 1.1))
        vals = sorted({1.0, 1.2, 1.4, 1.6, round(base, 3)})
        groups.append(("gross_leverage", [{"strategy.gross_leverage": v} for v in vals]))
    if _present(cfg, "strategy.max_weight_per_asset"):
        base = float(deep_get(cfg, "strategy.max_weight_per_asset", 0.14))
        vals = sorted({0.10, 0.14, 0.18, 0.22, 0.25, round(base, 3)})
        groups.append(("max_weight_per_asset", [{"strategy.max_weight_per_asset": v} for v in vals]))

    # 4) Selection breadth / diversification
    if _present(cfg, "strategy.selection.fallback_k"):
        base = int(deep_get(cfg, "strategy.selection.fallback_k", 6))
        vals = sorted({4, 6, 8, int(base)})
        groups.append(("selection.fallback_k", [{"strategy.selection.fallback_k": v} for v in vals]))
    if _present(cfg, "strategy.breadth_gate.min_fraction"):
        base = float(deep_get(cfg, "strategy.breadth_gate.min_fraction", 0.2))
        vals = sorted({0.1, 0.2, 0.3, round(base, 3)})
        groups.append(("breadth_gate.min_fraction", [{"strategy.breadth_gate.min_fraction": v} for v in vals]))
    if _present(cfg, "strategy.cluster_diversify.corr_threshold") or _present(cfg, "strategy.cluster_diversify.max_per_cluster"):
        ct = float(deep_get(cfg, "strategy.cluster_diversify.corr_threshold", 0.75))
        mp = int(deep_get(cfg, "strategy.cluster_diversify.max_per_cluster", 2))
        ct_vals = sorted({0.6, 0.7, 0.8, round(ct, 3)})
        mp_vals = sorted({1, 2, 3, int(mp)})
        cand = []
        for a in ct_vals:
            for b in mp_vals:
                cand.append({
                    "strategy.cluster_diversify.enabled": True,
                    "strategy.cluster_diversify.corr_threshold": float(a),
                    "strategy.cluster_diversify.max_per_cluster": int(b),
                })
        groups.append(("cluster_diversify", cand))
    if _present(cfg, "strategy.dispersion_gate.threshold"):
        base = float(deep_get(cfg, "strategy.dispersion_gate.threshold", 0.6))
        vals = sorted({0.4, 0.6, 0.8, round(base, 3)})
        groups.append(("dispersion_gate.threshold", [{"strategy.dispersion_gate.threshold": v} for v in vals]))

    # 5) Regime/ADX gating
    if _present(cfg, "strategy.adx_filter.min_adx") or _present(cfg, "strategy.adx_filter.len"):
        mn = int(deep_get(cfg, "strategy.adx_filter.len", 14))
        md = float(deep_get(cfg, "strategy.adx_filter.min_adx", 20.0))
        len_vals = sorted({10, 14, 20, int(mn)})
        min_vals = sorted({15.0, 20.0, 25.0, float(md)})
        cand = []
        for a in len_vals:
            for b in min_vals:
                cand.append({
                    "strategy.adx_filter.enabled": True,
                    "strategy.adx_filter.len": int(a),
                    "strategy.adx_filter.min_adx": float(b),
                })
        groups.append(("adx_filter", cand))
    if _present(cfg, "strategy.regime_filter.ema_len") or _present(cfg, "strategy.regime_filter.slope_min_bps_per_day"):
        el = int(deep_get(cfg, "strategy.regime_filter.ema_len", 200))
        sl = float(deep_get(cfg, "strategy.regime_filter.slope_min_bps_per_day", 2.0))
        ema_vals = sorted({100, 200, 300, int(el)})
        slope_vals = sorted({1.0, 2.0, 3.0, float(sl)})
        cand = []
        for a in ema_vals:
            for b in slope_vals:
                cand.append({
                    "strategy.regime_filter.enabled": True,
                    "strategy.regime_filter.ema_len": int(a),
                    "strategy.regime_filter.slope_min_bps_per_day": float(b),
                })
        groups.append(("regime_filter", cand))

    # 6) Carry / funding tilt
    if _present(cfg, "strategy.carry.budget_frac"):
        base = float(deep_get(cfg, "strategy.carry.budget_frac", 0.25))
        vals = sorted({0.10, 0.25, 0.40, round(base, 3)})
        groups.append(("carry.budget_frac", [{"strategy.carry.budget_frac": v} for v in vals]))
    if _present(cfg, "strategy.carry.funding.min_percentile_30d") or _present(cfg, "strategy.carry.basis.min_annualized") or _present(cfg, "strategy.carry.basis.min_zscore"):
        fp = float(deep_get(cfg, "strategy.carry.funding.min_percentile_30d", 0.8))
        fr = float(deep_get(cfg, "strategy.carry.funding.min_abs_rate_8h", 3e-4))
        ba = float(deep_get(cfg, "strategy.carry.basis.min_annualized", 0.05))
        bz = float(deep_get(cfg, "strategy.carry.basis.min_zscore", 0.8))
        fp_vals = sorted({0.7, 0.8, 0.9, float(fp)})
        fr_vals = sorted({2e-4, 3e-4, 4e-4, float(fr)})
        ba_vals = sorted({0.03, 0.05, 0.08, float(ba)})
        bz_vals = sorted({0.6, 0.8, 1.0, float(bz)})
        cand = []
        for a in fp_vals:
            for b in fr_vals:
                for c in ba_vals:
                    for d in bz_vals:
                        cand.append({
                            "strategy.carry.enabled": True,
                            "strategy.carry.funding.min_percentile_30d": float(a),
                            "strategy.carry.funding.min_abs_rate_8h": float(b),
                            "strategy.carry.basis.min_annualized": float(c),
                            "strategy.carry.basis.min_zscore": float(d),
                        })
        groups.append(("carry.thresholds", cand))
    if _present(cfg, "strategy.funding_tilt.weight"):
        w = float(deep_get(cfg, "strategy.funding_tilt.weight", 0.2))
        vals = sorted({0.1, 0.2, 0.3, 0.4, round(w, 3)})
        groups.append(("funding_tilt.weight", [{"strategy.funding_tilt.weight": v} for v in vals]))

    # 7) Trailing stop
    if _present(cfg, "risk.trailing_sl.multiplier") or _present(cfg, "risk.trailing_sl.ma_len") or _present(cfg, "risk.trailing_sl.atr_length"):
        mul = float(deep_get(cfg, "risk.trailing_sl.multiplier", 1.5))
        ma  = int(deep_get(cfg, "risk.trailing_sl.ma_len", 34))
        atr = int(deep_get(cfg, "risk.trailing_sl.atr_length", 14))
        mul_vals = sorted({1.2, 1.4, 1.6, 1.8, round(mul, 2)})
        ma_vals  = sorted({21, 34, 55, int(ma)})
        atr_vals = sorted({14, 28, 48, int(atr)})
        cand = []
        for a in mul_vals:
            cand.append({"risk.trailing_sl.enabled": True, "risk.trailing_sl.multiplier": float(a)})
        for b in ma_vals:
            cand.append({"risk.trailing_sl.enabled": True, "risk.trailing_sl.ma_len": int(b)})
        for c in atr_vals:
            cand.append({"risk.trailing_sl.enabled": True, "risk.trailing_sl.atr_length": int(c)})
        groups.append(("risk.trailing_sl", cand))

    # 8) Shock mode
    if _present(cfg, "strategy.shock_mode.vol_z_threshold") or _present(cfg, "strategy.shock_mode.cap_scale"):
        vz = float(deep_get(cfg, "strategy.shock_mode.vol_z_threshold", 2.5))
        cs = float(deep_get(cfg, "strategy.shock_mode.cap_scale", 0.8))
        vz_vals = sorted({2.0, 2.5, 3.0, round(vz, 2)})
        cs_vals = sorted({0.6, 0.75, 0.9, round(cs, 2)})
        cand = []
        for a in vz_vals:
            cand.append({"strategy.shock_mode.enabled": True, "strategy.shock_mode.vol_z_threshold": float(a)})
        for b in cs_vals:
            cand.append({"strategy.shock_mode.enabled": True, "strategy.shock_mode.cap_scale": float(b)})
        groups.append(("shock_mode", cand))

    # 9) Dynamic entry bands (if present)
    for band in ("low_corr", "mid_corr", "high_corr"):
        key = f"strategy.dynamic_entry_band.{band}.zmin"
        if _present(cfg, key):
            base = float(deep_get(cfg, key, 0.5))
            vals = sorted({round(base-0.1,2), round(base,2), round(base+0.1,2)})
            groups.append((f"dynamic_entry_band.{band}.zmin", [{key: v} for v in vals]))

    return groups

# ---------------- Phase 2 (extra groups requested earlier) ----------------
def phase2_extra_groups_from_cfg(cfg: Dict[str, Any],
                                 include_execution: bool,
                                 allow_enable: bool) -> List[Tuple[str, List[Dict[str, Any]]]]:
    groups: List[Tuple[str, List[Dict[str, Any]]]] = []

    def maybe_enable(path_prefix: str, ov: Dict[str, Any]) -> Dict[str, Any]:
        if allow_enable:
            en_path = f"{path_prefix}.enabled"
            cur = deep_get(cfg, en_path, None)
            if cur is False or cur is True:
                ov = dict(ov)
                ov[en_path] = True
        return ov

    # funding_trim
    if _present(cfg, "strategy.funding_trim"):
        thr = float(deep_get(cfg, "strategy.funding_trim.threshold_bps", 0.0))
        slope = float(deep_get(cfg, "strategy.funding_trim.slope_per_bps", 0.0))
        mx = float(deep_get(cfg, "strategy.funding_trim.max_reduction", 0.0))
        thr_vals = sorted({1.0, 2.0, 3.0, 4.0, thr})
        slope_vals = sorted({0.05, 0.1, 0.15, 0.2, slope})
        max_vals = sorted({0.2, 0.4, 0.6, mx})
        cand = []
        for a in thr_vals:
            ov = {"strategy.funding_trim.threshold_bps": float(a)}
            cand.append(maybe_enable("strategy.funding_trim", ov))
        for b in slope_vals:
            ov = {"strategy.funding_trim.slope_per_bps": float(b)}
            cand.append(maybe_enable("strategy.funding_trim", ov))
        for c in max_vals:
            ov = {"strategy.funding_trim.max_reduction": float(c)}
            cand.append(maybe_enable("strategy.funding_trim", ov))
        groups.append(("strategy.funding_trim", cand))

    # risk.* toggles
    for block in ("risk.exit_on_regime_flip", "risk.no_progress", "risk.profit_lock", "risk.trailing_unlocks", "risk.partial_ladders"):
        if _present(cfg, block):
            cand = []
            if allow_enable and _present(cfg, f"{block}.enabled"):
                cand.append({f"{block}.enabled": True})
            for subk in ("confirm_bars", "min_minutes", "min_rr"):
                path = f"{block}.{subk}"
                if _present(cfg, path):
                    base = deep_get(cfg, path)
                    if isinstance(base, int):
                        vals = sorted({max(0, base-1), base, base+1})
                        for v in vals: cand.append({path: v})
                    elif isinstance(base, float):
                        vals = sorted({round(max(0.0, base-0.1),2), round(base,2), round(base+0.1,2)})
                        for v in vals: cand.append({path: v})
            groups.append((block, cand))

    # partial_ladders default ladder
    if _present(cfg, "risk.partial_ladders.enabled"):
        ladd = [{"risk.partial_ladders.enabled": True,
                 "risk.partial_ladders.r_levels": [1.0, 2.0],
                 "risk.partial_ladders.sizes": [0.3, 0.3],
                 "risk.partial_ladders.reduce_only": True}]
        groups.append(("risk.partial_ladders.ladders", ladd))

    # per-trade vol target
    if _present(cfg, "strategy.vol_target.target_daily_vol_bps"):
        base = int(deep_get(cfg, "strategy.vol_target.target_daily_vol_bps", 100))
        vals = sorted({80, 100, 120, 150, int(base)})
        cand = []
        for v in vals:
            cand.append({"strategy.vol_target.enabled": True, "strategy.vol_target.target_daily_vol_bps": int(v)})
        groups.append(("strategy.vol_target", cand))

    # meta_label
    if _present(cfg, "strategy.meta_label.min_prob") or _present(cfg, "strategy.meta_label.learning_rate"):
        mp = float(deep_get(cfg, "strategy.meta_label.min_prob", 0.6))
        lr = float(deep_get(cfg, "strategy.meta_label.learning_rate", 0.05))
        mp_vals = sorted({0.5, 0.6, 0.65, round(mp,2)})
        lr_vals = sorted({0.01, 0.05, 0.1, round(lr,3)})
        cand = []
        for a in mp_vals:
            cand.append(maybe_enable("strategy.meta_label", {"strategy.meta_label.min_prob": float(a)}))
        for b in lr_vals:
            cand.append(maybe_enable("strategy.meta_label", {"strategy.meta_label.learning_rate": float(b)}))
        groups.append(("strategy.meta_label", cand))

    # (execution tuning omitted here; previously gated by --include-execution-tuning in earlier version; keep existing unit if needed)

    
# ---- Execution tuning (anti-churn & pricing) ----
    if include_execution:
        # throttle: min seconds between entries
        try:
            base = int(deep_get(cfg, "execution.throttle.min_seconds_between_entries_per_symbol", 12))
            vals = sorted({8,12,15,20,int(base)})
            groups.append(("execution.throttle.min_seconds_between_entries_per_symbol",
                           [{"execution.throttle.min_seconds_between_entries_per_symbol": int(v)} for v in vals]))
        except Exception:
            pass
        # throttle quotas
        try:
            bh = deep_get(cfg, "execution.throttle.max_entries_per_hour_per_symbol", None)
            bd = deep_get(cfg, "execution.throttle.max_entries_per_day_per_symbol", None)
            hvals = [1,2,3,4] if bh is None else sorted(set([1,2,3,4,int(bh)]))
            dvals = [6,8,12,16] if bd is None else sorted(set([6,8,12,16,int(bd)]))
            groups.append(("execution.throttle.max_entries_per_hour_per_symbol",
                           [{"execution.throttle.max_entries_per_hour_per_symbol": int(v)} for v in hvals]))
            groups.append(("execution.throttle.max_entries_per_day_per_symbol",
                           [{"execution.throttle.max_entries_per_day_per_symbol": int(v)} for v in dvals]))
        except Exception:
            pass
        # pyramiding
        try:
            base_rr = float(deep_get(cfg, "execution.pyramiding.allow_when_rr_ge", 0.8))
            rr_vals = sorted({0.5,0.8,1.0,1.5,round(base_rr,2)})
            groups.append(("execution.pyramiding.enabled",
                           [{"execution.pyramiding.enabled": False}, {"execution.pyramiding.enabled": True}]))
            groups.append(("execution.pyramiding.allow_when_rr_ge",
                           [{"execution.pyramiding.allow_when_rr_ge": float(v)} for v in rr_vals]))
        except Exception:
            pass
        # loss re-entry cooldown
        try:
            base_lc = int(deep_get(cfg, "risk.reentry_after_loss_minutes", 0))
            vals = sorted({0,3,5,10,15,int(base_lc)})
            groups.append(("risk.reentry_after_loss_minutes",
                           [{"risk.reentry_after_loss_minutes": int(v)} for v in vals]))
        except Exception:
            pass

    return groups

# ---------------- PnL CSV ingestion ----------------
def _parse_pnl_csvs(glob_pat: str,
                    tz_name: str = "Australia/Brisbane") -> List[Dict[str, Any]]:
    """
    Parse one or many CSVs (Bybit export or generic) into list of rows {symbol, dt_local, pnl}.
    Expected columns (best effort):
      - symbol: "Market" | "Symbol" | "Instrument"
      - time: "Trade time" (format: "HH:MM YYYY-MM-DD") | any col with "time"/"date"
      - pnl: "Realized P&L" | "realized_pnl" | "Pnl"
    """
    import pandas as pd
    import numpy as np
    import pytz
    rows: List[Dict[str, Any]] = []
    paths = sorted([p for g in glob.glob(glob_pat) for p in glob.glob(g)] if ("*" in glob_pat or "?" in glob_pat or "[" in glob_pat) else glob.glob(glob_pat))
    if not paths:
        return rows
    tz = pytz.timezone(tz_name)

    for path in paths:
        try:
            df = pd.read_csv(path)
        except Exception:
            continue
        cols = {c.lower(): c for c in df.columns}

        def pick(names: Iterable[str], default=None):
            for n in names:
                if n.lower() in cols:
                    return df[cols[n.lower()]]
            if default is not None:
                return pd.Series([default]*len(df))
            return None

        sym = pick(["Market","Symbol","Instrument","symbol","market","instrument"])
        tm  = pick(["Trade time","trade_time","time","closed_at","close_time","timestamp","date"], None)
        pnl = pick(["Realized P&L","realized_pnl","pnl","Realized","net_pnl"], None)
        if sym is None or tm is None or pnl is None:
            continue

        # Parse time
        s = tm.astype(str)
        # Bybit UI export usually "HH:MM YYYY-MM-DD"
        dt = None
        try:
            dt = pd.to_datetime(s, format="%H:%M %Y-%m-%d", errors="coerce")
        except Exception:
            dt = pd.to_datetime(s, errors="coerce", utc=False)
        # localize
        try:
            dloc = dt.dt.tz_localize(tz, nonexistent="NaT", ambiguous="NaT")
        except Exception:
            # if already tz-aware
            try:
                dloc = dt.dt.tz_convert(tz)
            except Exception:
                dloc = dt

        p = pd.to_numeric(pnl, errors="coerce").fillna(0.0)

        for sym_i, dt_i, pnl_i in zip(sym.astype(str).tolist(), dloc.tolist(), p.tolist()):
            if pd.isna(dt_i):
                continue
            rows.append({"symbol": sym_i.strip().upper(), "dt_local": dt_i, "pnl": float(pnl_i)})
    return rows

def _worst_symbols(rows: List[Dict[str, Any]],
                   lookback_hours: int,
                   min_trades: int,
                   top_k: int) -> List[str]:
    from collections import defaultdict
    if not rows:
        return []
    cutoff = datetime.now().astimezone(rows[0]["dt_local"].tzinfo) - timedelta(hours=lookback_hours)
    sum_by = defaultdict(float)
    cnt_by = defaultdict(int)
    for r in rows:
        if r["dt_local"] >= cutoff:
            sum_by[r["symbol"]] += r["pnl"]
            cnt_by[r["symbol"]] += 1
    losers = [(sym, s, cnt_by[sym]) for sym, s in sum_by.items() if s < 0 and cnt_by[sym] >= min_trades]
    losers.sort(key=lambda x: (x[1], -x[2]))  # most negative first; tie-break: more trades
    return [sym for sym, s, c in losers[:max(0, top_k)]]

def _worst_hours(rows: List[Dict[str, Any]], top_h: int = 4) -> List[int]:
    from collections import defaultdict
    if not rows:
        return []
    sum_by = defaultdict(float)
    for r in rows:
        h = int(getattr(r["dt_local"], "hour", 0))
        sum_by[h] += r["pnl"]
    ranks = sorted(sum_by.items(), key=lambda kv: kv[1])  # most negative first
    return [h for h, s in ranks[:max(0, top_h)]]

# ---------------- Grid runner ----------------
def run_grid_on_top(base_cfg: Dict[str, Any],
                    seed_overrides: Dict[str, Any],
                    candidates: List[Dict[str, Any]],
                    compare_to_metrics: Dict[str, Any],
                    objective: str,
                    require_no_worse_mdd: bool,
                    max_turnover_per_year: Optional[float]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    best_delta: Dict[str, Any] = {}
    best_metrics = compare_to_metrics
    best_score = score(compare_to_metrics, objective)

    for ov in candidates:
        merged = dict(seed_overrides); merged.update(ov)
        tmp = write_tmp_cfg(base_cfg, merged)
        try:
            m = run_bt(tmp)
            if not valid_under_constraints(m, compare_to_metrics, require_no_worse_mdd, max_turnover_per_year):
                continue
            sc = score(m, objective)
            if sc > best_score:
                best_score = sc
                best_metrics = m
                best_delta = ov
        finally:
            try: os.unlink(tmp)
            except Exception: pass

    return best_delta, best_metrics

# ---------------- Orchestrator ----------------
def optimize(config_path: str,
             min_abs_legacy=0.05,
             min_improve_abs: Optional[float]=None,
             min_improve_rel: float=0.0,
             objective: str="sharpe",
             dry_run: bool=False,
             backup: bool=True,
             require_no_worse_mdd: bool=False,
             max_turnover_per_year: Optional[float]=None,
             phase2: bool=True,
             phase2_passes: int=1,
             phase2_extra: bool=False,
             allow_enable: bool=False, include_execution_tuning: bool=False,
             # New: PnL-informed sweeps
             pnl_csv_glob: Optional[str]=None,
             blacklist_min_trades: int=6,
             blacklist_top_k: int=10,
             blacklist_lookback_hours: int=72,
             tod_sweep: bool=False,
             tod_bad_hours: Optional[List[int]]=None,
             tod_remove_counts: str="2,6",
             log_csv: Optional[str]=None) -> None:
    base_cfg = load_cfg(config_path)

    # ---------- Baseline
    base_tmp = write_tmp_cfg(base_cfg, {})
    try:
        baseline_metrics = run_bt(base_tmp)
    finally:
        try: os.unlink(base_tmp)
        except Exception: pass
    cur_metrics = baseline_metrics
    cur_overrides: Dict[str, Any] = {}

    # ---------- Phase 1
    p1_candidates = phase1_grid_from_cfg(base_cfg)
    p1_best_delta, p1_best_metrics = run_grid_on_top(
        base_cfg, cur_overrides, p1_candidates, cur_metrics, objective,
        require_no_worse_mdd, max_turnover_per_year
    )
    if p1_best_delta:
        cur_overrides.update(p1_best_delta)
        cur_metrics = p1_best_metrics

    # ---------- Phase 2 (base + extra groups)
    p2_summary: List[Dict[str, Any]] = []
    if phase2 or phase2_extra:
        groups = phase2_groups_from_cfg(base_cfg)
        if phase2_extra:
            groups += phase2_extra_groups_from_cfg(base_cfg, include_execution=include_execution_tuning, allow_enable=allow_enable)
        for _pass in range(phase2_passes):
            any_change = False
            for gname, ggrid in groups:
                delta, m = run_grid_on_top(
                    base_cfg, cur_overrides, ggrid, cur_metrics, objective,
                    require_no_worse_mdd, max_turnover_per_year
                )
                if delta:
                    any_change = True
                    cur_overrides.update(delta)
                    cur_metrics = m
                    p2_summary.append({"group": gname, "delta": delta, "metrics": m})
            if not any_change:
                break

    # ---------- PnL-informed symbol blacklist + time-of-day sweep
    pnl_summary = {}
    if pnl_csv_glob:
        rows = _parse_pnl_csvs(pnl_csv_glob)
        pnl_summary["csv_rows"] = len(rows)
        # Symbol blacklist proposals
        worst_syms = _worst_symbols(rows, blacklist_lookback_hours, blacklist_min_trades, blacklist_top_k)
        pnl_summary["worst_symbols"] = worst_syms

        # Determine symbol filter key if present
        sym_base = "strategy.symbol_filter"
        sym_key = None
        for candidate in ("exclude","blacklist","deny","symbols"):
            if _present(base_cfg, f"{sym_base}.{candidate}"):
                sym_key = candidate
                break

        # Build blacklist candidates (only if block exists)
        if sym_key is not None and worst_syms:
            existing = deep_get(base_cfg, f"{sym_base}.{sym_key}", []) or []
            base_set = sorted({str(s).upper() for s in existing})
            # Try excluding top 4, 6, 8, 10 losers (intersection with universe)
            sizes = sorted(set([min(4, len(worst_syms)), min(6, len(worst_syms)), min(8, len(worst_syms)), len(worst_syms)]))
            blist_candidates = []
            for n in sizes:
                new_set = sorted(set(base_set) | set(worst_syms[:n]))
                ov = {f"{sym_base}.enabled": True, f"{sym_base}.{sym_key}": new_set} if allow_enable else {f"{sym_base}.{sym_key}": new_set}
                blist_candidates.append(ov)
            delta, m = run_grid_on_top(
                base_cfg, cur_overrides, blist_candidates, cur_metrics, objective,
                require_no_worse_mdd, max_turnover_per_year
            )
            if delta:
                cur_overrides.update(delta)
                cur_metrics = m
                p2_summary.append({"group": "symbol_blacklist", "delta": delta, "metrics": m})

        # Time-of-day sweep
        if tod_sweep:
            # locate time-of-day whitelist block
            tod_base = None
            for cand in ("strategy.time_of_day_whitelist","strategy.gates.time_of_day"):
                if _present(base_cfg, cand):
                    tod_base = cand
                    break
            # find the array key
            tod_key = None
            if tod_base:
                for arrk in ("allow_hours","hours","whitelist","allowed"):
                    if _present(base_cfg, f"{tod_base}.{arrk}"):
                        tod_key = arrk
                        break

            worst_hours = _worst_hours(rows, top_h=6)
            if tod_bad_hours:  # override with explicit list
                worst_hours = sorted(set(tod_bad_hours))
            pnl_summary["worst_hours"] = worst_hours

            if tod_base and (tod_key or allow_enable):
                # Build candidates that remove the worst N hours
                remove_counts = [int(x) for x in str(tod_remove_counts).split(",") if str(x).strip().isdigit()]
                remove_counts = [n for n in remove_counts if n > 0]
                # current allowed set
                existing = deep_get(base_cfg, f"{tod_base}.{tod_key}", list(range(24))) if tod_key else list(range(24))
                if existing is None or not isinstance(existing, list) or len(existing)==0:
                    existing = list(range(24))
                base_allowed = sorted({int(h) for h in existing if 0 <= int(h) <= 23})

                cand_list = []
                for n in remove_counts:
                    bad = set(worst_hours[:min(n, len(worst_hours))])
                    allowed = sorted([h for h in range(24) if h not in bad])
                    ov = {}
                    ov[f"{tod_base}.enabled"] = True if allow_enable else deep_get(base_cfg, f"{tod_base}.enabled", True)
                    if tod_key:
                        ov[f"{tod_base}.{tod_key}"] = allowed
                    cand_list.append(ov)

                if cand_list:
                    delta, m = run_grid_on_top(
                        base_cfg, cur_overrides, cand_list, cur_metrics, objective,
                        require_no_worse_mdd, max_turnover_per_year
                    )
                    if delta:
                        cur_overrides.update(delta)
                        cur_metrics = m
                        p2_summary.append({"group": "time_of_day_whitelist", "delta": delta, "metrics": m})

    # ---------- Decision
    base_primary, _ = score(baseline_metrics, objective)
    best_primary, _ = score(cur_metrics, objective)
    min_abs = float(min_abs_legacy if min_improve_abs is None else min_improve_abs)
    abs_gain = best_primary - base_primary
    rel_gain = (abs_gain / abs(base_primary)) if base_primary != 0 else float('inf')
    improved = (abs_gain >= min_abs) or (rel_gain >= float(min_improve_rel))

    result = {
        "phase1": {"delta": p1_best_delta if p1_candidates else {}, "metrics": p1_best_metrics if p1_candidates else cur_metrics},
        "phase2": p2_summary,
        "pnl_informed": pnl_summary,
        "baseline": baseline_metrics,
        "final_metrics": cur_metrics,
        "final_overrides": cur_overrides,
        "abs_gain": abs_gain,
        "rel_gain": rel_gain,
        "thresholds": {"min_abs": min_abs, "min_rel": min_improve_rel},
        "constraints": {
            "require_no_worse_mdd": require_no_worse_mdd,
            "max_turnover_per_year": max_turnover_per_year
        },
        "improved": improved
    }
    print(json.dumps(result, indent=2))

    # ---------- Write + log
    if improved and cur_overrides and not dry_run:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        if backup:
            shutil.copy2(config_path, f"{config_path}.bak-{ts}")
        new_tmp = write_tmp_cfg(base_cfg, cur_overrides)
        try:
            os.replace(new_tmp, config_path)  # atomic
        except Exception:
            try: os.unlink(new_tmp)
            except Exception: pass
            raise

        # CSV audit log
        if log_csv:
            try:
                lp = Path(log_csv)
                lp.parent.mkdir(parents=True, exist_ok=True)
                exists = lp.exists()
                with lp.open("a", newline="") as f:
                    w = csv.writer(f)
                    if not exists:
                        w.writerow([
                            "timestamp","objective","min_abs","min_rel",
                            "require_no_worse_mdd","max_turnover_per_year",
                            "baseline_sharpe","baseline_calmar","baseline_mdd","baseline_turnover",
                            "final_sharpe","final_calmar","final_mdd","final_turnover",
                            "abs_gain","rel_gain","overrides_json"
                        ])
                    w.writerow([
                        ts, objective, min_abs, min_improve_rel,
                        require_no_worse_mdd, max_turnover_per_year,
                        baseline_metrics.get("sharpe"), baseline_metrics.get("calmar"),
                        baseline_metrics.get("max_drawdown"), baseline_metrics.get("gross_turnover_per_year"),
                        cur_metrics.get("sharpe"), cur_metrics.get("calmar"),
                        cur_metrics.get("max_drawdown"), cur_metrics.get("gross_turnover_per_year"),
                        abs_gain, rel_gain, json.dumps(cur_overrides, separators=(",",":"))
                    ])
            except Exception as e:
                print(f"[optimizer-runner] WARN: failed to append CSV log: {e}", flush=True)

# ---------------- CLI ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    # thresholds
    ap.add_argument("--min-sharpe-improve", type=float, default=0.05, help="Absolute Sharpe gain required to write (legacy; overridden by --min-improve-abs if set)")
    ap.add_argument("--min-improve-abs", type=float, default=None, help="Absolute gain required on objective")
    ap.add_argument("--min-improve-rel", type=float, default=0.0, help="Relative gain (e.g. 0.03 = +3%) required on objective")
    ap.add_argument("--objective", choices=["sharpe","calmar","return"], default="sharpe", help="Objective to maximize")
    # constraints
    ap.add_argument("--require-no-worse-mdd", action="store_true")
    ap.add_argument("--max-turnover-per-year", type=float, default=None)
    # behavior
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-backup", action="store_true")
    # phase 2 (base + extra)
    ap.add_argument("--phase2", action="store_true", help="Enable Phase 2 sweeps (coordinate descent)")
    ap.add_argument("--phase2-passes", type=int, default=1, help="Number of passes over Phase 2 groups")
    ap.add_argument("--phase2-extra", action="store_true", help="Include extra groups (funding_trim, meta_label, per-trade vol_target, partial_ladders, and risk toggles)")
    ap.add_argument("--allow-enable", action="store_true", help="Allow flipping 'enabled' from false→true inside tuned blocks when the flag exists")
    # PnL-informed features
    ap.add_argument("--pnl-csv-glob", default=None, help='Glob or path to PnL CSVs (e.g., "/opt/xsmom-bot/reports/Bybit-AllPerp-ClosedPNL-*.csv")')
    ap.add_argument("--blacklist-min-trades", type=int, default=6)
    ap.add_argument("--blacklist-top-k", type=int, default=10)
    ap.add_argument("--blacklist-lookback-hours", type=int, default=72)
    ap.add_argument("--tod-sweep", action="store_true", help="Enable time-of-day whitelist sweep based on worst hours")
    ap.add_argument("--tod-bad-hours", default=None, help="Comma-separated hours to consider bad (e.g., '11,12,17,18,19,20,21,22,23'). If omitted, inferred from PnL")
    ap.add_argument("--tod-remove-counts", default="2,6", help="Test removing N worst hours (comma-separated list)")
    # logging
    ap.add_argument("--log-csv", default=None, help="Append accepted writes to CSV (e.g., /var/log/xsmom-optimizer/history.csv)")
    args = ap.parse_args()

    tod_bad = None
    if args.tod_bad_hours:
        try:
            tod_bad = [int(x) for x in args.tod_bad_hours.split(",") if str(x).strip().isdigit()]
        except Exception:
            tod_bad = None

    optimize(args.config,
             min_abs_legacy=args.min_sharpe_improve,
             min_improve_abs=args.min_improve_abs,
             min_improve_rel=args.min_improve_rel,
             objective=args.objective,
             dry_run=args.dry_run,
             backup=not args.no_backup,
             require_no_worse_mdd=args.require_no_worse_mdd,
             max_turnover_per_year=args.max_turnover_per_year,
             phase2=args.phase2 or args.phase2_extra,
             phase2_passes=args.phase2_passes,
             phase2_extra=args.phase2_extra,
             allow_enable=args.allow_enable,
             pnl_csv_glob=args.pnl_csv_glob,
             blacklist_min_trades=args.blacklist_min_trades,
             blacklist_top_k=args.blacklist_top_k,
             blacklist_lookback_hours=args.blacklist_lookback_hours,
             tod_sweep=args.tod_sweep,
             tod_bad_hours=tod_bad,
             tod_remove_counts=args.tod_remove_counts,
             log_csv=args.log_csv)

if __name__ == "__main__":
    main()
