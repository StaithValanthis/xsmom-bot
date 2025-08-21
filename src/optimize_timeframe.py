import argparse
import logging
from datetime import datetime, timezone
from math import ceil
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

from .config import load_config, AppConfig
from .exchange import ExchangeWrapper
from .backtester import run_backtest

log = logging.getLogger("opt-tf")


# ---------- helpers ----------

def _tf_to_minutes(tf: str) -> float:
    tf = tf.strip().lower()
    if tf.endswith("m"):
        return float(tf[:-1])
    if tf.endswith("h"):
        return float(tf[:-1]) * 60.0
    if tf.endswith("d"):
        return float(tf[:-1]) * 1440.0
    raise ValueError(f"Unsupported timeframe: {tf}")

def _scale_bars_from_hours(hours_list: List[float], tf_minutes: float) -> List[int]:
    bars = []
    for h in hours_list:
        bars.append(int(max(1, ceil((h * 60.0) / tf_minutes))))
    return bars

def _parse_bool(s: str) -> bool:
    return s.strip().lower() in ("1","true","t","yes","y")

def _fetch_bars_for_tf(ex: ExchangeWrapper, symbols: List[str], timeframe: str, limit: int) -> Dict[str, pd.DataFrame]:
    out: Dict[str, pd.DataFrame] = {}
    for s in symbols:
        try:
            raw = ex.fetch_ohlcv(s, timeframe=timeframe, limit=limit)
            if not raw:
                continue
            df = pd.DataFrame(raw, columns=["ts","open","high","low","close","volume"])
            df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
            df.set_index("dt", inplace=True)
            out[s] = df
        except Exception as e:
            log.warning(f"OHLCV failed for {s} @ {timeframe}: {e}")
    return out

def _score_row(bt: Dict[str, float]) -> float:
    cagr = float(bt.get("annualized", 0.0))
    dd = float(bt.get("max_drawdown", 0.0))
    if dd < 0:
        return cagr / abs(dd) if abs(dd) > 1e-9 else 10.0 * cagr
    return -1e9


# ---------- main sweep ----------

def run_timeframe_sweep(
    base_cfg: AppConfig,
    timeframes: List[str],
    scale_lookbacks: bool,
    scale_vol: bool,
    scale_regime_ema: bool,
    margin_bars: int,
    min_candles: int,
) -> pd.DataFrame:

    logging.getLogger().setLevel(logging.INFO)

    # Determine base minutes from the config’s timeframe (e.g., 1h → 60)
    base_tf_min = _tf_to_minutes(base_cfg.exchange.timeframe)
    base_lb_hours = [lb * (base_tf_min / 60.0) for lb in base_cfg.strategy.lookbacks]
    base_vol_hours = base_cfg.strategy.vol_lookback * (base_tf_min / 60.0)
    base_ema_hours = base_cfg.strategy.regime_filter.ema_len * (base_tf_min / 60.0)

    # Fixed universe (independent of TF)
    ex = ExchangeWrapper(base_cfg.exchange)
    try:
        universe = ex.fetch_markets_filtered()
    finally:
        ex.close()

    if not universe:
        raise RuntimeError("No symbols after filters; adjust liquidity gates or exchange settings.")

    rows = []
    for tf in timeframes:
        tf_min = _tf_to_minutes(tf)

        # Clone config and adjust timeframe
        trial = AppConfig(**base_cfg.model_dump())
        trial.exchange.timeframe = tf

        # Scale params (keep the *information window* roughly consistent across TFs)
        if scale_lookbacks:
            trial.strategy.lookbacks = _scale_bars_from_hours(base_lb_hours, tf_min)
        if scale_vol:
            trial.strategy.vol_lookback = int(max(5, _scale_bars_from_hours([base_vol_hours], tf_min)[0]))
        if scale_regime_ema:
            trial.strategy.regime_filter.ema_len = int(max(10, _scale_bars_from_hours([base_ema_hours], tf_min)[0]))

        # Determine candles needed and prefetch
        need = max(
            max(trial.strategy.lookbacks or [1]),
            trial.strategy.vol_lookback,
            trial.strategy.regime_filter.ema_len,
        ) + margin_bars
        trial.exchange.candles_limit = max(trial.exchange.candles_limit, need, min_candles)

        ex_tf = ExchangeWrapper(trial.exchange)
        try:
            bars = _fetch_bars_for_tf(ex_tf, universe, trial.exchange.timeframe, trial.exchange.candles_limit)
        finally:
            ex_tf.close()

        if not bars:
            log.warning(f"No bars fetched for {tf}; skipping.")
            continue

        # Run backtest using prefetched bars
        bt = run_backtest(trial, universe, prefetch_bars=bars, return_curve=False)
        if not bt:
            continue

        rows.append({
            "timeframe": tf,
            "lookbacks_bars": trial.strategy.lookbacks,
            "vol_lookback_bars": trial.strategy.vol_lookback,
            "ema_len_bars": trial.strategy.regime_filter.ema_len,
            "total_return": bt["total_return"],
            "annualized": bt["annualized"],
            "max_drawdown": bt["max_drawdown"],
            "calmar": bt["calmar"],
            "score": _score_row(bt),
        })

    df = pd.DataFrame(rows).sort_values(["score","annualized","max_drawdown"], ascending=[False,False,True])

    # Save CSV
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = f"{base_cfg.paths.logs_dir}/timeframe_sweep_{ts}.csv"
    try:
        import os
        os.makedirs(base_cfg.paths.logs_dir, exist_ok=True)
        df.to_csv(out_path, index=False)
        log.info(f"Saved results to {out_path}")
    except Exception as e:
        log.warning(f"Could not save CSV: {e}")

    return df


def main():
    p = argparse.ArgumentParser(prog="xsmom-timeframe-opt")
    p.add_argument("--config", required=True, help="Path to YAML config")
    p.add_argument("--timeframes", default="15m,30m,1h,2h,4h", help="Comma list, e.g. 15m,30m,1h,2h,4h")
    p.add_argument("--scale_lookbacks", default="true", help="Scale lookbacks by TF (keep hours constant)")
    p.add_argument("--scale_vol", default="true", help="Scale vol_lookback by TF")
    p.add_argument("--scale_regime_ema", default="true", help="Scale regime EMA length by TF")
    p.add_argument("--margin_bars", type=int, default=50, help="Extra bars beyond min requirements")
    p.add_argument("--min_candles", type=int, default=800, help="Lower bound on candles_limit per TF")
    args = p.parse_args()

    cfg = load_config(args.config)

    tfs = [t.strip() for t in args.timeframes.split(",") if t.strip()]
    df = run_timeframe_sweep(
        cfg,
        tfs,
        _parse_bool(args.scale_lookbacks),
        _parse_bool(args.scale_vol),
        _parse_bool(args.scale_regime_ema),
        args.margin_bars,
        args.min_candles,
    )

    print("\nTop 10 timeframes (by score):")
    if not df.empty:
        print(df.head(10).to_string(index=False))
    else:
        print("No results — check your exchange config or liquidity gates.")

if __name__ == "__main__":
    main()
