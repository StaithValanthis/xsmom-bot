# v1.1.0 – 2025-08-21
import logging
from typing import List, Dict, Tuple
import numpy as np
import pandas as pd

from .config import AppConfig
from .exchange import ExchangeWrapper
from .signals import regime_ok, dynamic_k
from .sizing import build_targets
from .utils import utcnow

log = logging.getLogger("backtest")

def _costs(turnover_notional: float, maker_ratio: float, cfg: AppConfig) -> float:
    fee_bps = maker_ratio * cfg.costs.maker_fee_bps + (1 - maker_ratio) * cfg.costs.taker_fee_bps
    slip_bps = cfg.costs.slippage_bps
    total_bps = fee_bps + slip_bps
    return -(total_bps / 10_000.0) * turnover_notional

def _perf_stats(eq: pd.Series) -> Dict[str, float]:
    rets = eq.pct_change().dropna()
    if rets.empty:
        return {"total_return": 0.0, "max_drawdown": 0.0, "annualized": 0.0, "sharpe": 0.0, "calmar": 0.0}
    hours_per_year = 24 * 365
    ann = (1.0 + rets.mean()) ** hours_per_year - 1.0
    vol_ann = rets.std() * np.sqrt(hours_per_year)
    sharpe = ann / vol_ann if vol_ann > 0 else 0.0
    dd = (eq / eq.cummax() - 1.0).min()
    total = float(eq.iloc[-1] - 1.0)
    calmar = (ann / abs(dd)) if dd < 0 else 0.0
    return {
        "total_return": float(total),
        "max_drawdown": float(dd),
        "annualized": float(ann),
        "sharpe": float(sharpe),
        "calmar": float(calmar),
    }

def run_backtest(cfg: AppConfig, symbols: List[str]) -> Dict[str, float]:
    ex = ExchangeWrapper(cfg.exchange)
    bars = {}
    for s in symbols:
        try:
            raw = ex.fetch_ohlcv(s, timeframe=cfg.exchange.timeframe, limit=cfg.exchange.candles_limit)
            df = pd.DataFrame(raw, columns=["ts","open","high","low","close","volume"])
            df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
            df.set_index("dt", inplace=True)
            if len(df) > 0:
                bars[s] = df
        except Exception as e:
            log.warning(f"Failed OHLCV {s}: {e}")

    # Funding tilt snapshot (static in BT)
    funding_map: Dict[str, float] = {}
    try:
        if getattr(cfg.strategy.funding_tilt, "enabled", False):
            funding_map = ex.fetch_funding_rates(list(bars.keys())) or {}
    except Exception as e:
        log.debug(f"Funding rates fetch failed in BT: {e}")
        funding_map = {}

    ex.close()

    if not bars:
        log.error("No bars fetched; backtest aborted.")
        return {}

    closes = pd.concat({s: bars[s]["close"] for s in bars}, axis=1).dropna(how="all")
    idx = closes.index
    rets = closes.pct_change().fillna(0.0)

    equity = [1.0]
    maker_ratio = 0.5  # assume half maker, half taker
    weights_hist = []
    turnover_hist = []

    warmup = max(max(cfg.strategy.lookbacks), cfg.strategy.vol_lookback) + 5
    for i in range(warmup, len(idx) - 1):
        window = closes.iloc[:i+1]

        # Optional regime gating
        if cfg.strategy.regime_filter.enabled:
            ok = regime_ok(
                window.mean(axis=1),
                cfg.strategy.regime_filter.ema_len,
                cfg.strategy.regime_filter.slope_min_bps_per_day,
                use_abs=bool(getattr(cfg.strategy.regime_filter, "use_abs", False)),
            )
            if not ok:
                weights_hist.append(pd.Series(0.0, index=closes.columns))
                equity.append(equity[-1])  # flat
                turnover_hist.append(0.0)
                continue

        w = build_targets(
            window,
            cfg.strategy.lookbacks,
            cfg.strategy.lookback_weights,
            cfg.strategy.vol_lookback,
            cfg.strategy.k_min,
            cfg.strategy.k_max,
            cfg.strategy.market_neutral,
            cfg.strategy.gross_leverage,
            cfg.strategy.max_weight_per_asset,
            dynamic_k_fn=dynamic_k,  # dispersion-sensitive K
            funding_tilt=funding_map if getattr(cfg.strategy.funding_tilt, "enabled", False) else None,
            funding_weight=float(getattr(cfg.strategy.funding_tilt, "weight", 0.0)) if getattr(cfg.strategy.funding_tilt, "enabled", False) else 0.0,
            entry_zscore_min=float(getattr(cfg.strategy, "entry_zscore_min", 0.0)),
            diversify_enabled=bool(getattr(cfg.strategy.diversify, "enabled", False)),
            corr_lookback=int(getattr(cfg.strategy.diversify, "corr_lookback", 48)),
            max_pair_corr=float(getattr(cfg.strategy.diversify, "max_pair_corr", 0.9)),
            vol_target_enabled=bool(getattr(cfg.strategy.vol_target, "enabled", False)),
            target_daily_vol_bps=float(getattr(cfg.strategy.vol_target, "target_daily_vol_bps", 0.0)),
            vol_target_min_scale=float(getattr(cfg.strategy.vol_target, "min_scale", 0.5)),
            vol_target_max_scale=float(getattr(cfg.strategy.vol_target, "max_scale", 2.0)),
        ).reindex(closes.columns).fillna(0.0)

        prev_w = weights_hist[-1] if len(weights_hist) else pd.Series(0.0, index=closes.columns)
        weights_hist.append(w)

        # one-bar forward return
        port_ret = (rets.iloc[i+1] * w).sum()

        # turnover estimate: sum abs(delta_w)
        delta = (w - prev_w).abs().sum()
        turnover_notional = delta  # equity assumed 1.0; scale by equity for real
        turnover_hist.append(turnover_notional)

        # apply costs & funding (simple model)
        pnl = equity[-1] * port_ret
        costs = _costs(turnover_notional * equity[-1], maker_ratio, cfg)
        funding = -(cfg.costs.funding_bps_per_day / 10_000.0) * equity[-1] / 24.0  # per hour
        equity.append(equity[-1] + pnl + costs + funding)

    eq = pd.Series(equity, index=idx[-len(equity):])
    stats = _perf_stats(eq)

    log.info("=== BACKTEST (cost-aware) ===")
    log.info(f"Samples: {len(eq)} bars  |  Universe size: {len(closes.columns)}")
    log.info(f"Total Return: {stats['total_return']:.2%} | Annualized: {stats['annualized']:.2%} | Sharpe: {stats['sharpe']:.2f}")
    log.info(f"Max Drawdown: {stats['max_drawdown']:.2%} | Calmar: {stats['calmar']:.2f}")

    return stats
