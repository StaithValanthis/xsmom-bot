import argparse
import logging
import os
import subprocess
from datetime import datetime, timezone
from math import ceil
from typing import Dict, List, Tuple

import pandas as pd

from .config import load_config, AppConfig
from .exchange import ExchangeWrapper
from .backtester import run_backtest

log = logging.getLogger("auto-opt")


def _setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)sZ | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


def _tf_minutes(tf: str) -> float:
    tf = tf.strip().lower()
    if tf.endswith("m"):
        return float(tf[:-1])
    if tf.endswith("h"):
        return float(tf[:-1]) * 60.0
    if tf.endswith("d"):
        return float(tf[:-1]) * 1440.0
    raise ValueError(f"Unsupported timeframe: {tf}")


def _scale_bars_from_hours(hours_val: float, tf_minutes: float) -> int:
    return int(max(1, ceil((hours_val * 60.0) / tf_minutes)))


def _scale_list_hours(hours_list: List[float], tf_minutes: float) -> List[int]:
    return [int(max(1, ceil((h * 60.0) / tf_minutes))) for h in hours_list]


def _calmar(stats: Dict[str, float], turnover_penalty: float = 0.0) -> float:
    cagr = float(stats.get("annualized", 0.0))
    dd = float(stats.get("max_drawdown", 0.0))
    base = cagr / abs(dd) if dd < 0 else -1e9
    gtpy = float(stats.get("gross_turnover_per_year", 0.0))
    return base - turnover_penalty * gtpy


def _hours_from_bars(bars: int, tf_minutes: float) -> float:
    return (bars * tf_minutes) / 60.0


def _current_hours(cfg: AppConfig) -> Tuple[List[float], float, float]:
    base_min = _tf_minutes(cfg.exchange.timeframe)
    lbs_h = [_hours_from_bars(lb, base_min) for lb in cfg.strategy.lookbacks]
    vol_h = _hours_from_bars(cfg.strategy.vol_lookback, base_min)
    ema_h = _hours_from_bars(cfg.strategy.regime_filter.ema_len, base_min)
    return lbs_h, vol_h, ema_h


def _try_restart(service: str):
    try:
        subprocess.run(["/bin/systemctl", "restart", service], check=True)
        log.info(f"systemd: restarted {service}")
    except Exception as e:
        log.warning(f"Could not restart {service}: {e}")


def sweep_timeframe_and_regime(
    base_cfg: AppConfig,
    universe: List[str],
    timeframes: List[str],
    ema_candidates: List[int],
    slope_candidates: List[float],
    drawdown_cap: float = 0.40,
    max_rows: int = 200,
) -> pd.DataFrame:
    rows: List[Dict] = []
    lbs_h, vol_h, ema_h = _current_hours(base_cfg)

    for tf in timeframes:
        tf_min = _tf_minutes(tf)
        scaled_lbs = _scale_list_hours(lbs_h, tf_min)
        scaled_vol = _scale_bars_from_hours(vol_h, tf_min)
        ema_time_consistent = _scale_bars_from_hours(ema_h, tf_min)

        for ema_len in sorted(set([ema_time_consistent] + ema_candidates)):
            for slope_bps in slope_candidates:
                trial = AppConfig(**base_cfg.model_dump())
                trial.exchange.timeframe = tf
                trial.strategy.lookbacks = scaled_lbs
                trial.strategy.vol_lookback = max(5, scaled_vol)
                trial.strategy.regime_filter.ema_len = int(max(10, ema_len))
                trial.strategy.regime_filter.slope_min_bps_per_day = float(slope_bps)

                need = max(
                    max(trial.strategy.lookbacks or [1]),
                    trial.strategy.vol_lookback,
                    trial.strategy.regime_filter.ema_len,
                ) + 50
                trial.exchange.candles_limit = max(trial.exchange.candles_limit, need, 800)

                stats = run_backtest(trial, universe)
                if not stats:
                    continue
                if float(stats.get("max_drawdown", 0.0)) < -abs(drawdown_cap):
                    continue

                row = {
                    "timeframe": tf,
                    "lookbacks": trial.strategy.lookbacks,
                    "vol_lookback": trial.strategy.vol_lookback,
                    "ema_len": trial.strategy.regime_filter.ema_len,
                    "slope_bps_per_day": slope_bps,
                    "total_return": float(stats["total_return"]),
                    "annualized": float(stats["annualized"]),
                    "max_drawdown": float(stats["max_drawdown"]),
                }
                if "gross_turnover_per_year" in stats:
                    row["gross_turnover_per_year"] = float(stats["gross_turnover_per_year"])
                row["calmar"] = _calmar(row, turnover_penalty=1e-3)
                rows.append(row)

                if len(rows) >= max_rows:
                    break
            if len(rows) >= max_rows:
                break
        if len(rows) >= max_rows:
            break

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(["calmar", "annualized", "max_drawdown"], ascending=[False, False, True])
    return df


def main():
    _setup_logging()

    ap = argparse.ArgumentParser(prog="xsmom-auto-opt")
    ap.add_argument("--config", required=True, help="Path to YAML config")
    ap.add_argument("--service", default="xsmom-bot.service", help="Systemd service name to restart on change")
    ap.add_argument("--restart", action="store_true", help="Restart service if a better configuration is found")
    ap.add_argument("--timeframes", default="30m,1h,2h,4h", help="Comma-separated list")
    ap.add_argument("--ema", default="80,120,150,200,260", help="Comma-separated EMA lengths (bars in target TF)")
    ap.add_argument("--slope", default="0,2,4,6,8", help="Comma-separated slope thresholds (bps/day)")
    ap.add_argument("--drawdown_cap", type=float, default=0.45, help="Reject if DD < -cap (decimal, e.g., 0.45)")
    args = ap.parse_args()

    cfg_path = args.config
    cfg = load_config(cfg_path)

    ex = ExchangeWrapper(cfg.exchange)
    try:
        universe = ex.fetch_markets_filtered()
        if not universe:
            log.error("No symbols after filters; aborting optimization.")
            return
        log.info(f"Universe size for optimization: {len(universe)}")
    finally:
        ex.close()

    tfs = [t.strip() for t in args.timeframes.split(",") if t.strip()]
    emas = [int(x.strip()) for x in args.ema.split(",") if x.strip()]
    slopes = [float(x.strip()) for x in args.slope.split(",") if x.strip()]

    df = sweep_timeframe_and_regime(
        cfg, universe, tfs, emas, slopes, drawdown_cap=float(args.drawdown_cap)
    )

    if df.empty:
        log.warning("No results produced by optimizer.")
        return

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    os.makedirs(cfg.paths.logs_dir, exist_ok=True)
    out_csv = os.path.join(cfg.paths.logs_dir, f"auto_opt_results_{ts}.csv")
    try:
        df.to_csv(out_csv, index=False)
        log.info(f"Wrote results: {out_csv}")
    except Exception as e:
        log.warning(f"Could not save results CSV: {e}")

    top = df.iloc[0].to_dict()
    log.info("Best config: %s", top)

    changed = False
    new_cfg = AppConfig(**cfg.model_dump())

    if str(cfg.exchange.timeframe).lower() != str(top["timeframe"]).lower():
        lbs_h, vol_h, ema_h = _current_hours(cfg)
        tf_min = _tf_minutes(top["timeframe"])
        new_cfg.exchange.timeframe = str(top["timeframe"])
        new_cfg.strategy.lookbacks = _scale_list_hours(lbs_h, tf_min)
        new_cfg.strategy.vol_lookback = _scale_bars_from_hours(vol_h, tf_min)
        new_cfg.strategy.regime_filter.ema_len = int(max(10, _scale_bars_from_hours(ema_h, tf_min)))
        changed = True

    if int(new_cfg.strategy.regime_filter.ema_len) != int(top["ema_len"]):
        new_cfg.strategy.regime_filter.ema_len = int(top["ema_len"])
        changed = True

    if float(new_cfg.strategy.regime_filter.slope_min_bps_per_day) != float(top["slope_bps_per_day"]):
        new_cfg.strategy.regime_filter.slope_min_bps_per_day = float(top["slope_bps_per_day"])
        changed = True

    if not changed:
        log.info("No meaningful change vs current config; nothing to write.")
        print(df.head(10).to_string(index=False))
        return

    import yaml
    backup = f"{cfg_path}.bak-{ts}"
    try:
        with open(cfg_path, "r") as f:
            old_txt = f.read()
        with open(backup, "w") as f:
            f.write(old_txt)
        with open(cfg_path, "w") as f:
            yaml.safe_dump(new_cfg.model_dump(), f, sort_keys=False, indent=2)
        log.info(f"Patched config written. Backup saved to {backup}")
    except Exception as e:
        log.error(f"Failed to write patched config: {e}")
        return

    if args.restart:
        _try_restart(args.service)

    print("\n=== TOP RESULTS (Calmar – λ·turnover) ===")
    print(df.head(10).to_string(index=False))


if __name__ == "__main__":
    main()
