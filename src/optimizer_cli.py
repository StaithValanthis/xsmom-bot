# src/optimizer_cli.py
# Generic parameter-grid optimizer for xsmom-bot-like repos.
# - Reads a base config (YAML)
# - Applies parameter combinations from a GRID file (YAML/JSON)
# - Runs a backtest for each candidate (via Python API or CLI fallback)
# - Scores candidates with a chosen objective
# - Writes the best config back to disk (with timestamped backup)
# - Optionally restarts the live service
#
# Usage example:
#   python -m src.optimizer_cli \
#       --config /opt/xsmom-bot/config/config.yaml \
#       --grid /opt/xsmom-bot/config/optimizer.grid.yaml \
#       --objective calmar_minus_lambda_turnover \
#       --lambda_turnover 0.001 \
#       --max_drawdown_cap 0.45 \
#       --min_trades 150 \
#       --restart_service xsmom-bot
#
from __future__ import annotations

import argparse
import itertools
import json
import os
import shutil
import subprocess
import sys
import tempfile
from copy import deepcopy
from datetime import datetime
from typing import Any, Dict, List, Optional

# Lazy YAML import
try:
    import yaml  # type: ignore
except Exception as e:
    print("PyYAML is required. Try: pip install pyyaml", file=sys.stderr)
    raise

# Objectives & constraints
from .optimizer_objectives import (
    Metrics,
    objective_calmar_minus_lambda_turnover,
    check_constraints,
)


# ---- Backtest adapters -------------------------------------------------------
def _import_backtester():
    """
    Try a few common import paths for a Python backtest entrypoint.
    Must return a callable like: run_backtest(cfg: dict) -> dict(metrics)
    """
    candidates = [
        ("src.backtest", "run_backtest"),
        ("src.backtester", "run_backtest"),
        ("src.bt", "run_backtest"),
    ]
    for mod, func in candidates:
        try:
            m = __import__(mod, fromlist=[func])
            fn = getattr(m, func, None)
            if callable(fn):
                return fn
        except Exception:
            continue
    return None


def run_backtest_adapter(tmp_config_path: str) -> Dict[str, Any]:
    """
    Run a backtest, preferring Python API; fallback to CLI.
    Expect a metrics-like dict back.
    """
    # Try Python API
    fn = _import_backtester()
    if fn is not None:
        # Assume run_backtest accepts cfg dict; load YAML here.
        with open(tmp_config_path, "r") as f:
            cfg = yaml.safe_load(f)
        try:
            res = fn(cfg)  # type: ignore
            if isinstance(res, dict):
                return res
        except Exception as e:
            print(f"[optimizer] Python backtest failed, will try CLI. Error: {e}", file=sys.stderr)

    # Fallback: CLI module patterns (try a few)
    cli_patterns = [
        [sys.executable, "-m", "src.backtest_cli", "--config", tmp_config_path, "--json", "-"],
        [sys.executable, "-m", "src.main", "--mode", "backtest", "--config", tmp_config_path, "--json", "-"],
        [sys.executable, "-m", "src.backtest", "--config", tmp_config_path, "--json", "-"],
    ]
    for cmd in cli_patterns:
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, check=True)
            out = p.stdout.strip()
            try:
                return json.loads(out)
            except Exception:
                start = out.find("{")
                end = out.rfind("}")
                if start >= 0 and end > start:
                    blob = out[start : end + 1]
                    return json.loads(blob)
        except Exception:
            continue
    raise RuntimeError("No usable backtest entrypoint found (API or CLI). Consider adding src.backtest_cli with --json support.")


# ---- Grid utilities ----------------------------------------------------------
def _load_grid(grid_path: str) -> Dict[str, Any]:
    with open(grid_path, "r") as f:
        text = f.read()
    if grid_path.lower().endswith(".json"):
        g = json.loads(text)
    else:
        g = yaml.safe_load(text)
    if not isinstance(g, dict) or "stages" not in g:
        raise ValueError("Grid file must contain a top-level 'stages' list.")
    return g


def _flatten_stage(stage: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return list of dicts, each a single combination for a stage."""
    keys = list(stage.keys())
    value_lists: List[List[Any]] = []
    for k in keys:
        vs = stage[k]
        if not isinstance(vs, (list, tuple)):
            vs = [vs]
        value_lists.append(list(vs))
    combos = []
    import itertools as _it
    for values in _it.product(*value_lists):
        combos.append({k: v for k, v in zip(keys, values)})
    return combos


def _apply_param_change_budget(candidate: Dict[str, Any], current: Dict[str, Any], budget: Dict[str, Any]) -> bool:
    """Return True if candidate is within per-run change budget vs current config."""
    for k, lim in (budget or {}).items():
        if k not in candidate:
            continue
        try:
            delta = abs(float(candidate[k]) - float(current.get(k, candidate[k])))
            if float(delta) > float(lim):
                return False
        except Exception:
            # Non-numeric changes are allowed (e.g., timeframe swap)
            continue
    return True


def _update_config_dict(base_cfg: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    """Shallow-merge keys into base_cfg under expected subtrees when possible."""
    cfg = deepcopy(base_cfg)
    strat = cfg.setdefault("strategy", {})
    ex = cfg.setdefault("exchange", {})
    # Heuristic mapping by known names
    for k, v in patch.items():
        if k in ("lookbacks", "lookback_weights", "vol_lookback", "k_min", "k_max",
                 "gross_leverage", "max_weight_per_asset", "entry_zscore_min", "exit_zscore",
                 "regime_ema", "regime_slope", "adx_len", "adx_min",
                 "corr_lookback", "max_pair_corr",
                 "target_portfolio_vol", "fill_bias",
                 "spread_bps_max", "min_depth_usd", "impact_bps_cap",
                 "funding_tilt", "carry_budget_frac"):
            strat[k] = v
        elif k in ("timeframe", "candles_limit"):
            ex[k] = v
        elif k in ("diversify_enabled",):
            strat.setdefault("diversify", {})["enabled"] = bool(v)
        else:
            # Put unknown keys under strategy by default
            strat[k] = v
    return cfg


def _write_yaml(d: Dict[str, Any], path: str) -> None:
    with open(path, "w") as f:
        yaml.safe_dump(d, f, sort_keys=False)


# ---- Main optimization loop --------------------------------------------------
def optimize(args: argparse.Namespace) -> int:
    with open(args.config, "r") as f:
        base_cfg = yaml.safe_load(f)

    grid = _load_grid(args.grid)
    guards = grid.get("guards", {})
    min_trades = guards.get("min_trades")
    param_budget = guards.get("param_change_budget", {}).get("per_run_max", {})

    stages = grid.get("stages", [])
    if not stages:
        print("[optimizer] No stages defined in grid.", file=sys.stderr)
        return 2

    lambda_turnover = float(args.lambda_turnover)
    best_score = -1e18
    best_cfg = None
    best_metrics = None

    # Allow partial application across stages: keep the best-so-far patch
    cumulative_patch: Dict[str, Any] = {}

    for si, stage in enumerate(stages, start=1):
        print(f"[optimizer] Stage {si}/{len(stages)}: exploring {len(stage)} params")
        combos = _flatten_stage(stage)
        print(f"[optimizer] Stage {si} has {len(combos)} combinations")

        stage_best_score = -1e18
        stage_best_patch = None
        stage_best_metrics = None

        for ci, patch in enumerate(combos, start=1):
            # Build candidate cfg = base + cumulative + this stage patch
            candidate_patch = {**cumulative_patch, **patch}

            # Check per-run param change budgets vs current live config (base_cfg on disk)
            if not _apply_param_change_budget(candidate_patch, base_cfg.get("strategy", {}), param_budget):
                print(f"[optimizer] Skip combo {ci}: exceeds per-run param change budget.")
                continue

            cand_cfg = _update_config_dict(base_cfg, candidate_patch)

            import tempfile as _tf
            with _tf.TemporaryDirectory() as td:
                tmp_cfg_path = os.path.join(td, "config.yaml")
                _write_yaml(cand_cfg, tmp_cfg_path)
                try:
                    metrics_raw = run_backtest_adapter(tmp_cfg_path)
                except Exception as e:
                    print(f"[optimizer] Backtest failed for combo {ci}/{len(combos)}: {e}", file=sys.stderr)
                    continue

            m = Metrics.from_dict(metrics_raw)

            if not check_constraints(
                m,
                max_drawdown_cap=args.max_drawdown_cap,
                max_ann_vol_cap=args.max_ann_vol_cap,
                min_trades=min_trades,
            ):
                continue

            if args.objective == "calmar_minus_lambda_turnover":
                score = objective_calmar_minus_lambda_turnover(m, lam=lambda_turnover)
            elif args.objective == "sortino_minus_lambda_turnover":
                score = m.sortino - lambda_turnover * m.turnover
            else:
                raise ValueError(f"Unknown objective: {args.objective}")

            if score > stage_best_score:
                stage_best_score = score
                stage_best_patch = candidate_patch
                stage_best_metrics = m

        # If stage found something, accept into cumulative patch
        if stage_best_patch is not None:
            print(f"[optimizer] Stage {si} winner: score={stage_best_score:.4f}, patch={stage_best_patch}")
            cumulative_patch = stage_best_patch
        else:
            print(f"[optimizer] Stage {si} found no acceptable candidates.", file=sys.stderr)

    # Final evaluation of cumulative winner
    if cumulative_patch:
        final_cfg = _update_config_dict(base_cfg, cumulative_patch)
        import tempfile as _tf2
        with _tf2.TemporaryDirectory() as td:
            tmp_cfg_path = os.path.join(td, "config.yaml")
            _write_yaml(final_cfg, tmp_cfg_path)
            metrics_raw = run_backtest_adapter(tmp_cfg_path)
        m = Metrics.from_dict(metrics_raw)

        if check_constraints(
            m,
            max_drawdown_cap=args.max_drawdown_cap,
            max_ann_vol_cap=args.max_ann_vol_cap,
            min_trades=min_trades,
        ):
            if args.objective == "calmar_minus_lambda_turnover":
                best_score = objective_calmar_minus_lambda_turnover(m, lam=lambda_turnover)
            else:
                best_score = m.sortino - lambda_turnover * m.turnover

            best_cfg = final_cfg
            best_metrics = m
        else:
            print("[optimizer] Final winner failed constraints at re-check.", file=sys.stderr)

    if best_cfg is None:
        print("[optimizer] No candidate satisfied constraints. Exiting.", file=sys.stderr)
        return 3

    # Backup existing config, then write
    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    backup_path = f"{args.config}.bak-{ts}"
    shutil.copyfile(args.config, backup_path)
    _write_yaml(best_cfg, args.config)

    # Emit a summary JSON to stdout for logs
    summary = {
        "timestamp_utc": ts,
        "objective": args.objective,
        "lambda_turnover": lambda_turnover,
        "best_score": best_score,
        "metrics": best_metrics.__dict__ if best_metrics else None,
        "patch": cumulative_patch,
        "backup": backup_path,
        "config_written": args.config,
    }
    print(json.dumps(summary, indent=2))

    # Optional: restart service
    if args.restart_service:
        svc = args.restart_service
        try:
            subprocess.run(["systemctl", "restart", svc], check=True)
            print(f"[optimizer] Restarted service: {svc}")
        except Exception as e:
            print(f"[optimizer] WARNING: failed to restart {svc}: {e}", file=sys.stderr)

    return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Parameter grid optimizer")
    p.add_argument("--config", required=True, help="Path to live config.yaml to update")
    p.add_argument("--grid", required=True, help="Path to optimizer GRID file (yaml/json)")
    p.add_argument("--objective", default="calmar_minus_lambda_turnover",
                   choices=["calmar_minus_lambda_turnover", "sortino_minus_lambda_turnover"])
    p.add_argument("--lambda_turnover", type=float, default=1e-3)
    p.add_argument("--max_drawdown_cap", type=float, default=None)
    p.add_argument("--max_ann_vol_cap", type=float, default=None)
    p.add_argument("--restart_service", default=None, help="systemd service name to restart after write")
    p.add_argument("--min_trades", type=int, default=None, help="(deprecated, use guards.min_trades in GRID)")

    args = p.parse_args(argv)
    return optimize(args)


if __name__ == "__main__":
    raise SystemExit(main())
