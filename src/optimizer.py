# v1.1.0 – 2025-08-21
import logging
from copy import deepcopy
from typing import Dict, List, Tuple

from .config import AppConfig
from .exchange import ExchangeWrapper
from .backtester import run_backtest

log = logging.getLogger("opt")

# A compact, high-signal parameter grid (kept intentionally small)
def _param_grid() -> List[Dict]:
    grids = []
    for slope_bps in [2.0, 3.0, 4.0]:
        for kmin, kmax in [(4, 10), (6, 12)]:
            for gl in [1.2, 1.4, 1.6]:
                for entry_z in [0.0, 0.4]:
                    for vt in [False, True]:
                        for div in [False, True]:
                            grids.append({
                                "regime_abs": True,
                                "regime_bps": slope_bps,
                                "ema_len": 200,
                                "lookbacks": [1, 6, 24, 72],
                                "weights": [0.5, 1.0, 1.0, 0.5],
                                "vol_lookback": 72,
                                "k_min": kmin,
                                "k_max": kmax,
                                "gross_leverage": gl,
                                "max_weight_per_asset": 0.16,
                                "entry_zscore_min": entry_z,
                                "funding_weight": 0.15,
                                "diversify": div,
                                "corr_lookback": 96,
                                "max_pair_corr": 0.8,
                                "vol_target": vt,
                                "target_daily_vol_bps": 100,
                                "vt_min": 0.6,
                                "vt_max": 1.6,
                            })
    return grids

def _apply_params(dst: AppConfig, p: Dict) -> AppConfig:
    cfg = dst.model_copy(deep=True)
    cfg.strategy.lookbacks = p["lookbacks"]
    cfg.strategy.lookback_weights = p["weights"]
    cfg.strategy.vol_lookback = p["vol_lookback"]
    cfg.strategy.k_min = p["k_min"]
    cfg.strategy.k_max = p["k_max"]
    cfg.strategy.gross_leverage = p["gross_leverage"]
    cfg.strategy.max_weight_per_asset = p["max_weight_per_asset"]
    cfg.strategy.entry_zscore_min = p["entry_zscore_min"]

    cfg.strategy.regime_filter.enabled = True
    cfg.strategy.regime_filter.ema_len = p["ema_len"]
    cfg.strategy.regime_filter.slope_min_bps_per_day = p["regime_bps"]
    cfg.strategy.regime_filter.use_abs = p["regime_abs"]

    cfg.strategy.funding_tilt.enabled = True
    cfg.strategy.funding_tilt.weight = p["funding_weight"]

    cfg.strategy.diversify.enabled = p["diversify"]
    cfg.strategy.diversify.corr_lookback = p["corr_lookback"]
    cfg.strategy.diversify.max_pair_corr = p["max_pair_corr"]

    cfg.strategy.vol_target.enabled = p["vol_target"]
    cfg.strategy.vol_target.target_daily_vol_bps = p["target_daily_vol_bps"]
    cfg.strategy.vol_target.min_scale = p["vt_min"]
    cfg.strategy.vol_target.max_scale = p["vt_max"]
    return cfg

def optimize(cfg: AppConfig) -> Tuple[Dict, Dict[str, float]]:
    ex = ExchangeWrapper(cfg.exchange)
    try:
        syms = ex.fetch_markets_filtered()
    finally:
        ex.close()

    if not syms:
        log.error("No symbols after filters; cannot optimize.")
        return {}, {}

    log.info(f"Optimization universe size: {len(syms)} (example: {', '.join(syms[:8])}{'...' if len(syms) > 8 else ''})")

    grid = _param_grid()
    results: List[Tuple[Dict, Dict[str, float]]] = []

    for i, p in enumerate(grid, 1):
        test_cfg = _apply_params(cfg, p)
        try:
            stats = run_backtest(test_cfg, syms)
        except Exception as e:
            log.warning(f"Backtest failed for set {i}/{len(grid)}: {e}")
            continue
        results.append((p, stats))
        log.info(f"[{i}/{len(grid)}] Calmar={stats.get('calmar',0):.2f} | Sharpe={stats.get('sharpe',0):.2f} | Ann={stats.get('annualized',0):.2%} | DD={stats.get('max_drawdown',0):.2%}")

    if not results:
        log.error("No successful runs in optimization.")
        return {}, {}

    # Rank: first by Calmar, then Sharpe, then Annualized
    results.sort(key=lambda t: (t[1].get("calmar",0), t[1].get("sharpe",0), t[1].get("annualized",0)), reverse=True)
    best_params, best_stats = results[0]

    log.info("\n=== TOP 5 PARAM SETS (Calmar, Sharpe, Ann, DD) ===")
    for j, (p, s) in enumerate(results[:5], 1):
        log.info(f"#{j}  Calmar={s['calmar']:.2f}  Sharpe={s['sharpe']:.2f}  Ann={s['annualized']:.2%}  DD={s['max_drawdown']:.2%}  {p}")

    log.info("\n=== RECOMMENDED PARAMS ===")
    log.info(str(best_params))
    return best_params, best_stats
