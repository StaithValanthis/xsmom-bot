# v1.3.1 – turnover-aware stats + optional symbols
import logging
from typing import List, Dict, Optional, Any
import numpy as np
import pandas as pd

from .config import AppConfig
from .exchange import ExchangeWrapper
from .signals import regime_ok, dynamic_k
from .sizing import build_targets
from .utils import utcnow

log = logging.getLogger("backtest")

def _symbols_from_cfg(cfg: Any) -> List[str]:
    """Derive symbols list from cfg if not explicitly provided."""
    # attribute path attempts
    for path in [
        ("strategy", "symbols"),
        ("exchange", "symbols"),
        ("universe", "symbols"),
    ]:
        try:
            cur = cfg
            for p in path:
                cur = getattr(cur, p)
            if cur:
                if isinstance(cur, str):
                    return [s.strip() for s in cur.split(",") if s.strip()]
                if isinstance(cur, (list, tuple)):
                    return [str(s) for s in cur if s]
        except Exception:
            pass
    # dict path attempts
    try:
        d = cfg if isinstance(cfg, dict) else {}
        for key in ("strategy", "exchange", "universe"):
            v = (d.get(key) or {}).get("symbols")
            if v:
                if isinstance(v, str):
                    return [s.strip() for s in v.split(",") if s.strip()]
                if isinstance(v, (list, tuple)):
                    return [str(s) for s in v if s]
    except Exception:
        pass
    return ["BTC/USDT", "ETH/USDT"]

def _costs(turnover_notional: float, maker_ratio: float, cfg: AppConfig) -> float:
    fee_bps = maker_ratio * cfg.costs.maker_fee_bps + (1 - maker_ratio) * cfg.costs.taker_fee_bps
    slip_bps = float(getattr(cfg.costs, "slippage_bps", 0.0))
    borrow_bps = float(getattr(cfg.costs, "borrow_bps", 0.0))
    total_bps = fee_bps + slip_bps + borrow_bps
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

def run_backtest(
    cfg: AppConfig,
    symbols: Optional[List[str]] = None,
    prefetch_bars: Optional[Dict[str, pd.DataFrame]] = None,
    return_curve: bool = False,
) -> Dict[str, float]:
    """Optimizer-friendly entrypoint. If symbols is None, derive from cfg."""
    if symbols is None:
        symbols = _symbols_from_cfg(cfg)

    log.info("=== BACKTEST (cost-aware) ===")
    log.info(f"Start: {utcnow().isoformat()} | timeframe={cfg.exchange.timeframe}")

    bars: Dict[str, pd.DataFrame] = {}
    funding_map: Dict[str, float] = {}

    if prefetch_bars is not None:
        for s, df in (prefetch_bars or {}).items():
            if isinstance(df, pd.DataFrame) and len(df) > 0:
                bars[s] = df.copy()
    else:
        ex = ExchangeWrapper(cfg.exchange, risk_cfg=None, data_cfg=cfg.data)  # Backtester doesn't need circuit breaker
        try:
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
            try:
                if getattr(cfg.strategy.funding_tilt, "enabled", False):
                    funding_map = ex.fetch_funding_rates(list(bars.keys())) or {}
            except Exception as e:
                log.debug(f"Funding rates fetch failed in BT: {e}")
                funding_map = {}
        finally:
            try:
                ex.close()
            except Exception:
                pass

    if not bars:
        log.error("No bars available; backtest aborted.")
        return {}

    closes = pd.concat({s: bars[s]["close"] for s in bars}, axis=1).dropna(how="all")
    idx = closes.index
    rets = closes.pct_change().fillna(0.0)

    maker_ratio = float(getattr(cfg.costs, "maker_fill_ratio", 0.6))

    equity = [1.0]
    weights_hist = []
    turnover_hist = []

    warmup = max(max(cfg.strategy.lookbacks), cfg.strategy.vol_lookback) + 5
    for i in range(warmup, len(idx) - 1):
        window = closes.iloc[:i+1]

        eligible_cols = list(window.columns)
        if cfg.strategy.regime_filter.enabled:
            ema_len = int(cfg.strategy.regime_filter.ema_len)
            thr = float(cfg.strategy.regime_filter.slope_min_bps_per_day)
            use_abs = bool(getattr(cfg.strategy.regime_filter, "use_abs", False))

            eligible_cols = []
            for s in window.columns:
                ser = window[s].dropna()
                try:
                    ok = regime_ok(ser, ema_len, thr, use_abs=use_abs)
                except Exception:
                    ok = True
                if ok:
                    eligible_cols.append(s)

            if len(eligible_cols) == 0:
                weights_hist.append(pd.Series(0.0, index=closes.columns))
                equity.append(equity[-1])
                turnover_hist.append(0.0)
                continue
            window = window[eligible_cols]

        w = build_targets(
            prices=window,
            equity=equity[-1],
            strategy_cfg=cfg.strategy,
            prev_weights=weights_hist[-1] if len(weights_hist) else None,
            returns=rets,
            weights_history=pd.DataFrame(weights_hist) if len(weights_hist) > 0 else None,
        ).reindex(closes.columns).fillna(0.0)

        prev_w = weights_hist[-1] if len(weights_hist) else pd.Series(0.0, index=closes.columns)
        weights_hist.append(w)

        port_ret = (rets.iloc[i+1] * w).sum()

        delta = (w - prev_w).abs().sum()
        turnover_notional = delta
        turnover_hist.append(turnover_notional)

        pnl = equity[-1] * port_ret
        costs = _costs(turnover_notional * equity[-1], maker_ratio, cfg)
        funding = 0.0
        if getattr(cfg.strategy.funding_tilt, "enabled", False) and funding_map:
            funding = float((w * pd.Series(funding_map).reindex(closes.columns).fillna(0.0)).sum()) / (365*24*10_000.0)
            funding *= equity[-1]

        equity.append(equity[-1] + pnl + costs + funding)

    eq = pd.Series(equity, index=idx[-len(equity):])
    stats = _perf_stats(eq)

    if turnover_hist:
        bars_per_year = 24 * 365
        stats = dict(stats)
        stats["avg_turnover_per_bar"] = float(np.mean(turnover_hist))
        stats["gross_turnover_per_year"] = float(np.mean(turnover_hist) * bars_per_year)

    # Count trades (non-zero weight changes)
    trade_count = 0
    if len(weights_hist) > 1:
        for i in range(1, len(weights_hist)):
            delta = (weights_hist[i] - weights_hist[i-1]).abs()
            if delta.sum() > 1e-6:  # Significant weight change
                trade_count += 1
    
    log.info("=== BACKTEST (cost-aware) ===")
    log.info(f"Samples: {len(eq)} bars  |  Universe size: {len(closes.columns)} | Trades: {trade_count}")
    log.info(f"Total Return: {stats['total_return']:.2%} | Annualized: {stats['annualized']:.2%} | Sharpe: {stats.get('sharpe',0):.2f}")
    log.info(f"Max Drawdown: {stats['max_drawdown']:.2%} | Calmar: {stats['calmar']:.2f}")
    
    # Add trade count to stats
    stats["trades"] = trade_count
    
    # Warn if no trades
    if trade_count == 0:
        log.warning(
            f"No trades executed in backtest. Possible causes: "
            f"all symbols filtered by regime/ADX filters, "
            f"entry thresholds too high (entry_zscore_min={getattr(cfg.strategy, 'entry_zscore_min', 0.0)}), "
            f"or parameter combination produces no signals."
        )

    if return_curve:
        stats["equity_curve"] = eq

    return stats
