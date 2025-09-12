
#!/usr/bin/env python3
import argparse, json, os, shutil, subprocess, tempfile, sys, itertools, csv, hashlib
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Tuple, Optional
import yaml

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

# ---------------- Phase 2 (existing) ----------------
def _present(cfg: Dict[str, Any], path: str) -> bool:
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

    # 2) Vol targeting (write both synonyms if present)
    if _present(cfg, "strategy.portfolio_vol_target") or _present(cfg, "strategy.portfolio_vol_target.target") or _present(cfg, "strategy.portfolio_vol_target.target_ann_vol"):
        base_t = float(deep_get(cfg, "strategy.portfolio_vol_target.target", deep_get(cfg, "strategy.portfolio_vol_target.target_ann_vol", 0.35)))
        tvals  = sorted({0.20, 0.30, 0.35, 0.40, 0.45, 0.50, round(base_t, 3)})
        cand_t = [{
            "strategy.portfolio_vol_target.enabled": True,
            "strategy.portfolio_vol_target.target": float(v),
            "strategy.portfolio_vol_target.target_ann_vol": float(v),
        } for v in tvals]
        groups.append(("portfolio_vol_target.target", cand_t))

        base_lb = int(deep_get(cfg, "strategy.portfolio_vol_target.lookback", deep_get(cfg, "strategy.portfolio_vol_target.lookback_hours", 72)))
        lbvals  = sorted({24, 48, 72, 120, 168, int(base_lb)})
        cand_lb = [{
            "strategy.portfolio_vol_target.enabled": True,
            "strategy.portfolio_vol_target.lookback": int(v),
            "strategy.portfolio_vol_target.lookback_hours": int(v),
        } for v in lbvals]
        groups.append(("portfolio_vol_target.lookback", cand_lb))

    # 3) Risk
    if _present(cfg, "strategy.gross_leverage"):
        base = float(deep_get(cfg, "strategy.gross_leverage", 1.1))
        vals = sorted({1.0, 1.2, 1.4, 1.6, round(base, 3)})
        groups.append(("gross_leverage", [{"strategy.gross_leverage": v} for v in vals]))
    if _present(cfg, "strategy.max_weight_per_asset"):
        base = float(deep_get(cfg, "strategy.max_weight_per_asset", 0.14))
        vals = sorted({0.10, 0.14, 0.18, 0.22, 0.25, round(base, 3)})
        groups.append(("max_weight_per_asset", [{"strategy.max_weight_per_asset": v} for v in vals]))

    # 4) Selection breadth
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
        cand = [{
            "strategy.cluster_diversify.enabled": True,
            "strategy.cluster_diversify.corr_threshold": float(a),
            "strategy.cluster_diversify.max_per_cluster": int(b),
        } for a in ct_vals for b in mp_vals]
        groups.append(("cluster_diversify", cand))
    if _present(cfg, "strategy.dispersion_gate.threshold"):
        base = float(deep_get(cfg, "strategy.dispersion_gate.threshold", 0.6))
        vals = sorted({0.4, 0.6, 0.8, round(base, 3)})
        groups.append(("dispersion_gate.threshold", [{"strategy.dispersion_gate.threshold": v} for v in vals]))

    # 5) Regime/ADX
    if _present(cfg, "strategy.adx_filter.min_adx") or _present(cfg, "strategy.adx_filter.len"):
        mn = int(deep_get(cfg, "strategy.adx_filter.len", 14))
        md = float(deep_get(cfg, "strategy.adx_filter.min_adx", 20.0))
        len_vals = sorted({10, 14, 20, int(mn)})
        min_vals = sorted({15.0, 20.0, 25.0, float(md)})
        cand = [{
            "strategy.adx_filter.enabled": True,
            "strategy.adx_filter.len": int(a),
            "strategy.adx_filter.min_adx": float(b),
        } for a in len_vals for b in min_vals]
        groups.append(("adx_filter", cand))
    if _present(cfg, "strategy.regime_filter.ema_len") or _present(cfg, "strategy.regime_filter.slope_min_bps_per_day"):
        el = int(deep_get(cfg, "strategy.regime_filter.ema_len", 200))
        sl = float(deep_get(cfg, "strategy.regime_filter.slope_min_bps_per_day", 2.0))
        ema_vals = sorted({100, 200, 300, int(el)})
        slope_vals = sorted({1.0, 2.0, 3.0, float(sl)})
        cand = [{
            "strategy.regime_filter.enabled": True,
            "strategy.regime_filter.ema_len": int(a),
            "strategy.regime_filter.slope_min_bps_per_day": float(b),
        } for a in ema_vals for b in slope_vals]
        groups.append(("regime_filter", cand))

    # 6) Carry
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
        cand = [{
            "strategy.carry.enabled": True,
            "strategy.carry.funding.min_percentile_30d": float(a),
            "strategy.carry.funding.min_abs_rate_8h": float(b),
            "strategy.carry.basis.min_annualized": float(c),
            "strategy.carry.basis.min_zscore": float(d),
        } for a in fp_vals for b in fr_vals for c in ba_vals for d in bz_vals]
        groups.append(("carry.thresholds", cand))

    return groups

# ---------------- Walk-forward evaluation ----------------
def _wf_active(base_cfg: Dict[str, Any]) -> Tuple[int, float, int]:
    """Read walk-forward settings from config (if present)."""
    wf = (base_cfg.get("optimizer") or {}).get("walk_forward") or {}
    n_splits = int(wf.get("n_splits") or 0)
    embargo = float(wf.get("embargo_frac") or 0.0)
    lbars = int(wf.get("lookback_bars") or 0)
    return n_splits, embargo, lbars

def get_logs_dir(cfg: Dict[str, Any]) -> str:
    p = deep_get(cfg, "paths.logs_dir", "/opt/xsmom-bot/logs")
    return p or "/opt/xsmom-bot/logs"

def log_csv_row(cfg: Dict[str, Any], row: Dict[str, Any]):
    try:
        logs_dir = get_logs_dir(cfg)
        os.makedirs(logs_dir, exist_ok=True)
        path = os.path.join(logs_dir, "optimizer_history.csv")
        file_exists = os.path.exists(path)
        with open(path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=[
                "ts", "phase", "group", "objective",
                "candidate_id", "overrides_json",
                "primary", "sharpe", "calmar", "annualized", "max_drawdown",
                "avg_turnover_per_bar", "gross_turnover_per_year",
                "passed_constraints", "passed_wf", "notes"
            ])
            if not file_exists:
                w.writeheader()
            w.writerow(row)
    except Exception as e:
        print(f"[optimizer-runner] WARN: failed to write CSV log: {e}", flush=True)

def _candidate_id(ov: Dict[str, Any]) -> str:
    j = json.dumps(ov, sort_keys=True, separators=(",",":"))
    return hashlib.sha256(j.encode("utf-8")).hexdigest()[:10]

def evaluate_candidate(base_cfg: Dict[str, Any],
                       seed_overrides: Dict[str, Any],
                       delta: Dict[str, Any],
                       objective: str,
                       base_metrics: Dict[str, Any],
                       require_no_worse_mdd: bool,
                       max_turnover_per_year: Optional[float],
                       wf_win_frac: float,
                       wf_max_rel_down: float) -> Tuple[bool, Dict[str, Any], Dict[str, Any]]:
    """Return (accepted, merged_overrides, metrics). Handles WF stability checks if configured."""
    merged = dict(seed_overrides); merged.update(delta)
    tmp = write_tmp_cfg(base_cfg, merged)
    try:
        # full-period run
        m_full = run_bt(tmp)
        passed_constraints = valid_under_constraints(m_full, base_metrics, require_no_worse_mdd, max_turnover_per_year)
        passed_wf = True
        notes = ""

        n_splits, embargo, lbars = _wf_active(base_cfg)
        if n_splits and n_splits > 1:
            # attempt walk-forward by signaling split index in config
            wins = 0
            splits_ok = 0
            for s in range(n_splits):
                # we set an index the backtester can optionally use; if ignored, results will be same for each split
                tmp2 = write_tmp_cfg(base_cfg, {**merged, "optimizer.walk_forward.active_split": int(s)})
                try:
                    m_s = run_bt(tmp2)
                    # per-split constraints vs full baseline
                    if not valid_under_constraints(m_s, base_metrics, require_no_worse_mdd, max_turnover_per_year):
                        passed_wf = False
                    # win condition
                    sp, _ = score(m_s, objective)
                    bp, _ = score(base_metrics, objective)
                    if sp >= bp: wins += 1
                    # do not allow large relative degradation vs baseline on any split
                    if bp != 0 and (bp - sp) / abs(bp) > wf_max_rel_down:
                        passed_wf = False
                    splits_ok += 1
                finally:
                    try: os.unlink(tmp2)
                    except Exception: pass
            if splits_ok == n_splits and wins / n_splits < wf_win_frac:
                passed_wf = False
                notes = f"WF win frac {wins}/{n_splits} below {wf_win_frac}"
        else:
            notes = "WF not active or only 1 split; using full-period only"

        accepted = passed_constraints and passed_wf
        # CSV log
        cand_id = _candidate_id(merged)
        log_csv_row(base_cfg, {
            "ts": datetime.now(timezone.utc).isoformat(),
            "phase": "search",
            "group": delta.get("_group",""),
            "objective": objective,
            "candidate_id": cand_id,
            "overrides_json": json.dumps(delta, sort_keys=True),
            "primary": score(m_full, objective)[0],
            "sharpe": m_full.get("sharpe"),
            "calmar": m_full.get("calmar"),
            "annualized": m_full.get("annualized"),
            "max_drawdown": m_full.get("max_drawdown"),
            "avg_turnover_per_bar": m_full.get("avg_turnover_per_bar"),
            "gross_turnover_per_year": m_full.get("gross_turnover_per_year"),
            "passed_constraints": passed_constraints,
            "passed_wf": passed_wf,
            "notes": notes
        })
        return accepted, merged, m_full
    finally:
        try: os.unlink(tmp)
        except Exception: pass

def better(sc_a: Tuple[float,float], sc_b: Tuple[float,float]) -> bool:
    return sc_a > sc_b

def run_grid_on_top(base_cfg: Dict[str, Any],
                    seed_overrides: Dict[str, Any],
                    candidates: List[Dict[str, Any]],
                    base_metrics: Dict[str, Any],
                    objective: str,
                    require_no_worse_mdd: bool,
                    max_turnover_per_year: Optional[float],
                    wf_win_frac: float,
                    wf_max_rel_down: float,
                    group_name: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    best_delta: Dict[str, Any] = {}
    best_metrics = base_metrics
    best_score = score(base_metrics, objective)

    for ov in candidates:
        ov = dict(ov)  # copy
        ov["_group"] = group_name
        ok, merged, m = evaluate_candidate(base_cfg, seed_overrides, ov, objective,
                                           base_metrics, require_no_worse_mdd, max_turnover_per_year,
                                           wf_win_frac, wf_max_rel_down)
        if not ok:
            continue
        sc = score(m, objective)
        if better(sc, best_score):
            best_score = sc
            best_metrics = m
            # keep only the actual delta (drop helper key)
            best_delta = {k:v for k,v in ov.items() if k != "_group"}
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
             # new robustness controls
             wf_win_frac: float=0.6,
             wf_max_rel_down: float=0.02,
             min_days_between_writes: int=1,
             force_write: bool=False) -> None:

    base_cfg = load_cfg(config_path)

    # cadence control
    last_updated = deep_get(base_cfg, "optimizer.last_updated", None)
    if last_updated and not dry_run and not force_write:
        try:
            dt = datetime.fromisoformat(last_updated.replace("Z","+00:00"))
            if datetime.now(timezone.utc) - dt < timedelta(days=min_days_between_writes):
                print(json.dumps({
                    "status": "skipped_due_to_cadence",
                    "min_days_between_writes": min_days_between_writes,
                    "last_updated": last_updated
                }, indent=2))
                return
        except Exception:
            pass

    # ---------- Phase 1
    base_tmp = write_tmp_cfg(base_cfg, {})
    try:
        baseline_metrics = run_bt(base_tmp)
    finally:
        try: os.unlink(base_tmp)
        except Exception: pass
    cur_metrics = baseline_metrics
    cur_overrides: Dict[str, Any] = {}

    # grid
    p1_candidates = phase1_grid_from_cfg(base_cfg)
    p1_best_delta, p1_best_metrics = run_grid_on_top(
        base_cfg, cur_overrides, p1_candidates, cur_metrics, objective,
        require_no_worse_mdd, max_turnover_per_year,
        wf_win_frac, wf_max_rel_down, "phase1"
    )
    if p1_best_delta:
        cur_overrides.update(p1_best_delta)
        cur_metrics = p1_best_metrics

    # ---------- Phase 2
    p2_summary: List[Dict[str, Any]] = []
    if phase2:
        groups = phase2_groups_from_cfg(base_cfg)
        for _pass in range(phase2_passes):
            any_change = False
            for gname, ggrid in groups:
                delta, m = run_grid_on_top(
                    base_cfg, cur_overrides, ggrid, cur_metrics, objective,
                    require_no_worse_mdd, max_turnover_per_year,
                    wf_win_frac, wf_max_rel_down, gname
                )
                if delta:
                    any_change = True
                    cur_overrides.update(delta)
                    cur_metrics = m
                    p2_summary.append({"group": gname, "delta": delta, "metrics": m})
            if not any_change:
                break

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
        "wf": {
            "configured": _wf_active(base_cfg)[0] or 0,
            "wf_win_frac": wf_win_frac,
            "wf_max_rel_down": wf_max_rel_down
        },
        "improved": improved
    }
    print(json.dumps(result, indent=2))

    # ---------- Write
    if improved and cur_overrides and not dry_run:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        if backup:
            shutil.copy2(config_path, f"{config_path}.bak-{ts}")
        # include optimizer metadata updates
        cur_overrides["optimizer.last_updated"] = datetime.now(timezone.utc).isoformat()
        cur_overrides["optimizer.last_best_params"] = cur_overrides.copy()
        new_tmp = write_tmp_cfg(base_cfg, cur_overrides)
        try:
            os.replace(new_tmp, config_path)  # atomic
        except Exception:
            try: os.unlink(new_tmp)
            except Exception: pass
            raise

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
    # phase 2
    ap.add_argument("--phase2", action="store_true", help="Enable Phase 2 sweeps (coordinate descent)")
    ap.add_argument("--phase2-passes", type=int, default=1, help="Number of passes over Phase 2 groups")
    # robustness
    ap.add_argument("--wf-win-frac", type=float, default=0.6, help="Require candidate to beat baseline on at least this fraction of splits (0..1). Only applied if optimizer.walk_forward.n_splits>1")
    ap.add_argument("--wf-max-rel-down", type=float, default=0.02, help="On any split, candidate cannot be worse than baseline by more than this fraction (e.g., 0.02=2%)")
    ap.add_argument("--min-days-between-writes", type=int, default=1, help="Skip writes if last update was more recent than this many days (cadence control)")
    ap.add_argument("--force-write", action="store_true", help="Bypass cadence control")
    args = ap.parse_args()

    optimize(args.config,
             min_abs_legacy=args.min_sharpe_improve,
             min_improve_abs=args.min_improve_abs,
             min_improve_rel=args.min_improve_rel,
             objective=args.objective,
             dry_run=args.dry_run,
             backup=not args.no_backup,
             require_no_worse_mdd=args.require_no_worse_mdd,
             max_turnover_per_year=args.max_turnover_per_year,
             phase2=args.phase2,
             phase2_passes=args.phase2_passes,
             wf_win_frac=args.wf_win_frac,
             wf_max_rel_down=args.wf_max_rel_down,
             min_days_between_writes=args.min_days_between_writes,
             force_write=args.force_write)

if __name__ == "__main__":
    main()
