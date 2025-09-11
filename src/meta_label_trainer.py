# src/meta_label_trainer.py
from __future__ import annotations
"""
Meta-labeler EOD trainer.

- Reads entry "events" from a JSONL file (default: <state_path>/meta_events.jsonl).
- If "features" missing, derives light features from prices.
- Labels with forward return over a horizon (bars) and trains a tiny logistic regression (SGD).
- Saves model to <state_path>/meta_labeler.npz and a readable mirror .json
"""

import argparse, json, sys, math, os
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime, timezone

VERSION = "2025-09-11a"

# Optional imports from project
try:
    from src.config import load_config
except Exception:
    from config import load_config  # type: ignore

try:
    from src.exchange import ExchangeWrapper  # type: ignore
except Exception:
    ExchangeWrapper = None  # type: ignore

def _parse_dt(s: str) -> datetime:
    try:
        return datetime.fromisoformat(s.replace("Z","+00:00"))
    except Exception:
        # best-effort fallback
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S%z")

def _ensure_tz(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

def _fetch_closes(ex, symbol: str, since_ms: int, limit: int, timeframe: str) -> pd.Series:
    ohlcv = ex.fetch_ohlcv(symbol, timeframe=timeframe, since=since_ms, limit=limit)  # ts, open, high, low, close, vol
    if not ohlcv:
        return pd.Series(dtype="float64")
    df = pd.DataFrame(ohlcv, columns=["ts","open","high","low","close","vol"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.set_index("ts")
    return df["close"]

def _compute_features(prices: pd.Series) -> dict[str, float]:
    """Lightweight features: mom1/mom6/mom24, vol(24), breakout(20), ts_sign(ema slope)."""
    out = {}
    if prices is None or prices.shape[0] < 30:
        return {"mom1":0.0,"mom6":0.0,"mom24":0.0,"vol":0.0,"breakout":0.0,"ts_sign":0.0}
    px = prices.ffill().bfill()
    rets = px.pct_change().fillna(0.0)
    out["mom1"]  = float(rets.iloc[-1])
    out["mom6"]  = float(px.iloc[-1]/px.iloc[-6]-1.0) if px.shape[0] >= 6 else 0.0
    out["mom24"] = float(px.iloc[-1]/px.iloc[-24]-1.0) if px.shape[0] >= 24 else 0.0
    out["vol"]   = float(rets.rolling(24).std().iloc[-1] or 0.0)
    if px.shape[0] >= 20:
        w = px.iloc[-20:]
        lo, hi = float(w.min()), float(w.max())
        rng = (hi - lo) or 1e-9
        out["breakout"] = float((px.iloc[-1] - lo)/rng - 0.5)  # centered (-0.5..0.5)
    else:
        out["breakout"] = 0.0
    if px.shape[0] >= 10:
        ema = px.ewm(span=10, adjust=False).mean()
        out["ts_sign"] = float(np.sign(ema.iloc[-1] - ema.iloc[-5]))
    else:
        out["ts_sign"] = 0.0
    return out

def _standardize(X: np.ndarray):
    mu = X.mean(axis=0)
    sd = X.std(axis=0)
    sd[sd == 0] = 1.0
    return (X - mu)/sd, mu, sd

def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-z))

def _train_logreg_sgd(X, y, l2=1e-3, lr=0.1, epochs=8, batch=64, seed=42):
    rs = np.random.RandomState(seed)
    n, d = X.shape
    w = rs.randn(d) * 0.01
    b = 0.0
    idx = np.arange(n)
    for _ in range(epochs):
        rs.shuffle(idx)
        for start in range(0, n, batch):
            sl = idx[start:start+batch]
            xb = X[sl]; yb = y[sl]
            z = xb @ w + b
            p = _sigmoid(z)
            g_w = xb.T @ (p - yb) / len(sl) + l2 * w
            g_b = float((p - yb).mean())
            w -= lr * g_w
            b -= lr * g_b
    return w, b

def _directional_ret(side: str, fwd_ret: float) -> float:
    return fwd_ret if side.lower() == "long" else (-fwd_ret)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, help="config.yaml path")
    ap.add_argument("--events", help="JSONL of entry events (default <state_path>/meta_events.jsonl)")
    ap.add_argument("--timeframe", default=None, help="candle timeframe (default from config or 5m)")
    ap.add_argument("--horizon", type=int, default=None, help="horizon in bars (default from config or 24)")
    ap.add_argument("--threshold", type=float, default=0.0, help="min directional ret to label=1")
    ap.add_argument("--min-samples", type=int, default=100, help="minimum samples to train")
    ap.add_argument("--epochs", type=int, default=8)
    ap.add_argument("--lr", type=float, default=0.1)
    ap.add_argument("--l2", type=float, default=1e-3)
    ap.add_argument("--out", default=None, help="override output path")
    args = ap.parse_args()

    cfg = load_config(args.config)

    # State path
    try:
        state_dir = Path(cfg.paths.state_path)  # type: ignore
    except Exception:
        state_dir = Path(getattr(getattr(cfg, "paths", {}), "state_path", "/opt/xsmom-bot/state"))
    state_dir.mkdir(parents=True, exist_ok=True)

    events_path = Path(args.events) if args.events else (state_dir / "meta_events.jsonl")
    timeframe = args.timeframe or getattr(getattr(cfg, "exchange", {}), "timeframe", "5m")
    horizon = int(args.horizon or getattr(getattr(getattr(cfg, "strategy", {}), "meta_label", {}), "horizon_bars", 24))

    # Exchange
    if ExchangeWrapper is None:
        print("WARN: ExchangeWrapper not importable; cannot fetch OHLCV.", file=sys.stderr)
        ex = None
    else:
        ex = ExchangeWrapper.create(cfg.exchange)  # type: ignore

    if not events_path.exists():
        print(f"No events file at {events_path}; nothing to train.")
        sys.exit(0)

    rows = []
    with events_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if ev.get("type","entry") not in ("entry","open"):
                continue
            sym = ev.get("symbol")
            ts = ev.get("ts") or ev.get("time") or ev.get("timestamp")
            side = ev.get("side","long").lower()
            if not (sym and ts and side in ("long","short")):
                continue
            rows.append((sym, _ensure_tz(_parse_dt(ts)), side, ev.get("features",{})))

    if not rows:
        print("No valid events found; exiting.")
        sys.exit(0)

    feats_list, y_list = [], []
    used = 0

    for sym, ts, side, feats in rows:
        if ex is None:
            continue
        since_ms = int((ts - pd.Timedelta(minutes=500)).timestamp() * 1000)
        try:
            closes = _fetch_closes(ex, sym, since_ms, limit=400, timeframe=timeframe)
        except Exception:
            continue
        if closes.empty:
            continue
        s_after = closes[closes.index >= ts]
        if s_after.empty or s_after.shape[0] <= horizon:
            continue
        entry_px = float(s_after.iloc[0])
        exit_px  = float(s_after.iloc[horizon])
        fwd_ret = (exit_px/entry_px) - 1.0

        if not feats or not isinstance(feats, dict):
            s_hist = closes.loc[:s_after.index[0]]
            feats = _compute_features(s_hist)

        feats_list.append(feats)
        y_list.append(1.0 if _directional_ret(side, fwd_ret) > args.threshold else 0.0)
        used += 1

    if used < args.min_samples:
        print(f"Only {used} usable samples (<{args.min_samples}); skip training.")
        sys.exit(0)

    feat_names = sorted({k for d in feats_list for k in d.keys()})
    X = np.array([[float(d.get(k,0.0)) for k in feat_names] for d in feats_list], dtype=float)
    y = np.array(y_list, dtype=float)

    Xs, mu, sd = _standardize(X)
    w, b = _train_logreg_sgd(Xs, y, l2=args.l2, lr=args.lr, epochs=args.epochs)

    out_path = Path(args.out) if args.out else (state_dir / "meta_labeler.npz")
    np.savez_compressed(out_path, w=w, b=b, mu=mu, sd=sd, feat_names=np.array(feat_names), version=VERSION)
    out_path.with_suffix(".json").write_text(json.dumps({
        "version": VERSION,
        "saved_at": datetime.utcnow().replace(tzinfo=datetime.timezone.utc).isoformat(),
        "feature_names": feat_names,
        "weights": w.tolist(),
        "bias": float(b),
        "mu": mu.tolist(),
        "sd": sd.tolist(),
        "n_samples": int(len(y)),
        "pos_rate": float(y.mean()),
    }, indent=2))

    print(f"Trained meta-labeler on {len(y)} samples; pos={y.mean():.3f}. Saved to {out_path}")

if __name__ == "__main__":
    main()
