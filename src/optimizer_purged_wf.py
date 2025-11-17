
from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, Any, List, Tuple, Callable, Iterable
import numpy as np
import pandas as pd
import itertools

@dataclass
class WFConfig:
    n_splits: int = 6
    embargo_frac: float = 0.02
    lookback_bars: int = 600
    objective: str = "sharpe"
    max_params: int = 256

def _split_purged_indices(n: int, k: int, embargo: int) -> List[Tuple[np.ndarray, np.ndarray]]:
    fold_sizes = [n // k + (1 if i < n % k else 0) for i in range(k)]
    idx = np.arange(n)
    res = []
    start = 0
    for i, fs in enumerate(fold_sizes):
        test_idx = idx[start:start+fs]
        left = max(0, start - embargo)
        right = min(n, start+fs + embargo)
        train_idx = np.concatenate([idx[:left], idx[right:]])
        res.append((train_idx, test_idx))
        start += fs
    return res

def _objective(pnl: np.ndarray, obj: str) -> float:
    ret = np.array(pnl, dtype=float)
    mu = ret.mean()
    sd = ret.std(ddof=0) if ret.size else 1e-9
    if obj == "sharpe":
        return float(mu / (sd + 1e-12)) if sd > 0 else 0.0
    if obj == "sortino":
        downside = ret[ret < 0].std(ddof=0) if (ret < 0).any() else 1e-9
        return float(mu / (downside + 1e-12))
    if obj == "pnl":
        return float(ret.sum())
    return float(mu / (sd + 1e-12))

def _simulate_basic(px: pd.DataFrame, params: Dict[str, Any]) -> np.ndarray:
    rets = np.log(px/px.shift(1)).fillna(0.0)
    zs = (rets - rets.mean(axis=1).values.reshape(-1,1)) / (rets.std(axis=1).values.reshape(-1,1) + 1e-9)
    k = int(params.get("k", 4))
    gross = float(params.get("gross", 1.0))
    long_mask = zs.apply(lambda row: row.nlargest(k).index, axis=1)
    short_mask = zs.apply(lambda row: row.nsmallest(k).index, axis=1)
    pnl = []
    for t, row in rets.iterrows():
        longs = long_mask.loc[t]
        shorts = short_mask.loc[t]
        w = pd.Series(0.0, index=rets.columns)
        if len(longs): w.loc[longs] =  0.5 * gross / max(1, len(longs))
        if len(shorts): w.loc[shorts] = -0.5 * gross / max(1, len(shorts))
        pnl.append(float((w * row).sum()))
    return np.array(pnl, dtype=float)

def walk_forward_optimize(
    prices: pd.DataFrame,
    param_grid: Dict[str, Iterable],
    cfg: WFConfig = WFConfig(),
    simulate_fn: Callable[[pd.DataFrame, Dict[str, Any]], np.ndarray] = _simulate_basic
) -> Tuple[Dict[str, Any], pd.DataFrame]:
    px = prices.ffill().bfill()
    n = px.shape[0]
    embargo = max(0, int(round(n * float(cfg.embargo_frac))))
    splits = _split_purged_indices(n, int(cfg.n_splits), embargo)

    import itertools
    grid = list(itertools.islice(itertools.product(*param_grid.values()), 0, int(cfg.max_params)))
    keys = list(param_grid.keys())

    rows = []
    for si, (train_idx, test_idx) in enumerate(splits):
        train = px.iloc[train_idx]
        test = px.iloc[test_idx]
        for vals in grid:
            params = {k: v for k, v in zip(keys, vals)}
            pnl = simulate_fn(train, params)
            pnl_test = simulate_fn(test, params)
            score = _objective(pnl_test, cfg.objective)
            rows.append({"split": si, "params": params, "score": score})

    import pandas as pd
    cv_table = pd.DataFrame(rows)
    agg = cv_table.groupby(cv_table["params"].apply(lambda d: tuple(sorted(d.items()))))["score"].mean()
    best_key = agg.idxmax()
    best_params = dict(best_key)
    return best_params, cv_table
