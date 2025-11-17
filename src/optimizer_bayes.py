# src/optimizer_bayes.py
from __future__ import annotations
"""
Bayesian-style (TPE-like) optimizer wrapper that reuses src.optimizer_cli for evaluation.
Proposes points inside --space, calls optimizer_cli with a single-point grid, and picks the best by occurrence.
Writes optimizer/best_params.json for the existing auto-merge flow.
"""
import argparse, json, os, re, subprocess, sys, random
from pathlib import Path
from datetime import datetime, timezone

VERSION = "2025-09-11a"

def _now_iso():
    return datetime.now(timezone.utc).isoformat()

def _parse_best(stdout: str):
    m = re.search(r"Best params:\s*(\{.*?\})", stdout)
    if not m:
        return None
    try:
        return eval(m.group(1), {}, {})  # self-output from optimizer_cli
    except Exception:
        return None

def _grid_for_point(point: dict) -> str:
    return json.dumps({k:[v] for k,v in point.items()})

def _rand_in(bounds):
    out = {}
    for k, b in bounds.items():
        if isinstance(b, list) and all(isinstance(x,(int,float)) for x in b):
            lo, hi = b[0], b[-1]
            if isinstance(lo, int) and isinstance(hi, int):
                out[k] = random.randint(lo, hi)
            else:
                out[k] = lo + random.random()*(hi-lo)
        elif isinstance(b, list):
            out[k] = random.choice(b)
        else:
            out[k] = b
    return out

def _perturb(best: dict, bounds: dict, scale=0.25):
    out = {}
    for k,v in best.items():
        b = bounds.get(k)
        if b is None:
            out[k] = v; continue
        if isinstance(b, list) and all(isinstance(x,(int,float)) for x in b):
            lo, hi = b[0], b[-1]
            rng = hi - lo
            if isinstance(v, int):
                step = max(1, int(rng*scale))
                out[k] = max(lo, min(hi, v + random.randint(-step, step)))
            else:
                step = rng*scale
                out[k] = max(lo, min(hi, v + random.uniform(-step, step)))
        elif isinstance(b, list):
            try:
                idx = b.index(v)
                choices = [x for x in (idx-1, idx, idx+1) if 0 <= x < len(b)]
                out[k] = b[random.choice(choices)]
            except Exception:
                out[k] = random.choice(b)
        else:
            out[k] = v
    return out

def _evaluate(point: dict, args) -> dict:
    grid = _grid_for_point(point)
    cmd = [
        sys.executable, "-m", "src.optimizer_cli",
        "--config", args.config,
        "--objective", args.objective,
        "--splits", str(args.splits),
        "--embargo", str(args.embargo),
        "--max-symbols", str(args.max_symbols),
        "--grid", grid,
    ]
    p = subprocess.run(cmd, capture_output=True, text=True, env=os.environ.copy())
    if p.returncode != 0:
        raise RuntimeError(f"optimizer_cli failed: {p.returncode}\nSTDOUT:\n{p.stdout}\nSTDERR:\n{p.stderr}")
    bp = _parse_best(p.stdout)
    if not bp:
        raise RuntimeError(f"Could not parse Best params from optimizer_cli output:\n{p.stdout}")
    return bp

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--objective", default="sharpe")
    ap.add_argument("--splits", type=int, default=3)
    ap.add_argument("--embargo", type=float, default=0.02)
    ap.add_argument("--max-symbols", type=int, default=10)
    ap.add_argument("--space", required=True, help='JSON param space, e.g. {"k":[2,12],"gross":[0.6,1.6],"entry_z":[1.0,2.5]}')
    ap.add_argument("--init", type=int, default=8, help="random init points")
    ap.add_argument("--iters", type=int, default=24, help="bayesian steps")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default=None, help="override best_params.json path")
    args = ap.parse_args()

    random.seed(args.seed)
    bounds = json.loads(args.space)

    samples = []
    for _ in range(args.init):
        pt = _rand_in(bounds)
        bpt = _evaluate(pt, args)
        samples.append(bpt)

    for _ in range(args.iters):
        center = samples[-1] if samples else _rand_in(bounds)
        cand = _perturb(center, bounds, scale=0.25)
        bpt = _evaluate(cand, args)
        samples.append(bpt)

    # pick the mode (most frequently returned best)
    from collections import Counter
    c = Counter(json.dumps(x, sort_keys=True) for x in samples)
    mode_json, _ = c.most_common(1)[0]
    best_params = json.loads(mode_json)

    repo = Path(__file__).resolve().parents[1]
    out_path = Path(args.out) if args.out else (repo / "optimizer" / "best_params.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"best_params": best_params, "saved_at": _now_iso(), "source": "bayes-wrapper"}, indent=2))
    print(f"Bayes wrapper complete. Best params: {best_params}")
    print(f"Saved {out_path}")

if __name__ == "__main__":
    main()
