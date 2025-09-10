from __future__ import annotations
import argparse, json
from typing import Optional, Dict
import pandas as pd

from .config import load_config
from .exchange import ExchangeWrapper
from .optimizer_purged_wf import walk_forward_optimize, WFConfig

def _parse_grid(s: str) -> Dict:
    try:
        return json.loads(s)
    except Exception:
        import ast
        try:
            return ast.literal_eval(s)
        except Exception as e:
            raise ValueError(f"Could not parse --grid. Got: {s!r}; error: {e}")

def _get_symbols(ex: ExchangeWrapper) -> list[str]:
    # Prefer wrapper's filtered universe; fall back to raw markets if needed
    try:
        syms = ex.fetch_markets_filtered()
        if syms:
            return syms
    except AttributeError:
        pass
    # Fallback: fetch_markets then keep active spot/perp symbols
    mkts = ex.fetch_markets()
    out = []
    for m in mkts:
        if isinstance(m, dict) and m.get("active", True) and m.get("symbol"):
            out.append(m["symbol"])
    return out

def _load_prices(ex: ExchangeWrapper, cfg, max_symbols: Optional[int] = None) -> pd.DataFrame:
    syms = _get_symbols(ex)
    if not syms:
        raise RuntimeError("No symbols after market filters (check allow/deny list, volume filter, etc.).")
    if max_symbols:
        syms = syms[:int(max_symbols)]

    frames: Dict[str, pd.Series] = {}
    tf = cfg.exchange.timeframe
    lim = cfg.exchange.candles_limit

    for s in syms:
        try:
            raw = ex.fetch_ohlcv(s, timeframe=tf, limit=lim)
            if not raw or len(raw) < 10:
                continue
            df = pd.DataFrame(raw, columns=["ts","open","high","low","close","volume"])
            df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
            df = df.set_index("ts").sort_index()
            frames[s] = df["close"].rename(s)
        except Exception:
            continue

    if not frames:
        raise RuntimeError("No price data loaded for optimization (all symbols failed or empty).")

    closes = pd.concat(frames.values(), axis=1).sort_index().ffill().bfill()
    return closes

def main():
    ap = argparse.ArgumentParser(description="Purged-CV Walk-Forward Optimizer")
    ap.add_argument("--config", required=True, help="Path to config.yaml")
    ap.add_argument("--objective", default="sharpe", choices=["sharpe","sortino","pnl"])
    ap.add_argument("--splits", type=int, default=6)
    ap.add_argument("--embargo", type=float, default=0.02)
    ap.add_argument("--max-params", type=int, default=256)
    ap.add_argument("--max-symbols", type=int, default=None)
    ap.add_argument("--grid", default='{"k":[2,4,6,8],"gross":[0.8,1.0,1.2]}', help="JSON dict of param grid")
    args = ap.parse_args()

    cfg = load_config(args.config)
    ex = ExchangeWrapper(cfg.exchange)
    try:
        closes = _load_prices(ex, cfg, max_symbols=args.max_symbols)
    finally:
        try:
            ex.close()
        except Exception:
            pass

    grid = _parse_grid(args.grid)

    best, cv = walk_forward_optimize(
        closes,
        param_grid=grid,
        cfg=WFConfig(n_splits=args.splits, embargo_frac=args.embargo, objective=args.objective, max_params=args.max_params),
    )
    print("Best params:", best)
    grp = cv.groupby(cv["params"].apply(lambda d: tuple(sorted(d.items()))))["score"].mean()
    print(grp.sort_values(ascending=False).head(10))

if __name__ == "__main__":
    main()
    