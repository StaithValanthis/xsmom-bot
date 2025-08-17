import logging
from typing import List, Dict
import numpy as np
import pandas as pd

from .config import AppConfig
from .exchange import ExchangeWrapper
from .signals import regime_ok
from .sizing import build_targets
from .utils import utcnow

log = logging.getLogger("backtest")

def _costs(turnover_notional: float, maker_ratio: float, cfg: AppConfig) -> float:
    fee_bps = maker_ratio * cfg.costs.maker_fee_bps + (1 - maker_ratio) * cfg.costs.taker_fee_bps
    slip_bps = cfg.costs.slippage_bps
    total_bps = fee_bps + slip_bps
    return -(total_bps / 10_000.0) * turnover_notional

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

    for i in range(max(max(cfg.strategy.lookbacks), cfg.strategy.vol_lookback) + 5, len(idx) - 1):
        window = closes.iloc[:i+1]

        # Optional regime gating
        if cfg.strategy.regime_filter.enabled:
            ok = regime_ok(window.mean(axis=1), cfg.strategy.regime_filter.ema_len, cfg.strategy.regime_filter.slope_min_bps_per_day)
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
            dynamic_k_fn=lambda sc, kmin, kmax: (kmin, kmax),  # fixed for backtest unless you want real dispersion-based
        ).reindex(closes.columns).fillna(0.0)

        prev_w = weights_hist[-1] if len(weights_hist) else pd.Series(0.0, index=closes.columns)
        weights_hist.append(w)

        # one-bar forward return
        port_ret = (rets.iloc[i+1] * w).sum()

        # turnover estimate (very rough): sum abs(delta_w)
        delta = (w - prev_w).abs().sum()
        turnover_notional = delta  # equity assumed 1.0; scale by equity for real
        turnover_hist.append(turnover_notional)

        # apply costs & funding (simple model)
        pnl = equity[-1] * port_ret
        costs = _costs(turnover_notional * equity[-1], maker_ratio, cfg)
        funding = -(cfg.costs.funding_bps_per_day / 10_000.0) * equity[-1] / 24.0  # per hour
        equity.append(equity[-1] + pnl + costs + funding)

    eq = pd.Series(equity, index=idx[-len(equity):])
    total_ret = eq.iloc[-1] - 1.0
    cagr = (eq.iloc[-1]) ** (24*365 / max(1, len(eq))) - 1.0
    dd = (eq / eq.cummax() - 1.0).min()

    log.info("=== BACKTEST (cost-aware) ===")
    log.info(f"Samples: {len(eq)} bars  |  Universe size: {len(closes.columns)}")
    log.info(f"Total Return: {total_ret:.2%}")
    log.info(f"Max Drawdown: {dd:.2%}")
    log.info(f"Rough Annualized: {cagr:.2%}")

    return {
        "total_return": float(total_ret),
        "max_drawdown": float(dd),
        "annualized": float(cagr),
    }
