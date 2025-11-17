
#!/usr/bin/env python3
"""
Robust Bybit PnL ingester for optimizer:
- Requires valid BYBIT apiKey/secret (or keys in config(exchange.api_key/api_secret)).
- Avoids load_markets(); pulls private executions across ALL symbols via category='linear'.
- Computes approximate realized PnL per symbol using FIFO/avg-cost.
- Always writes a CSV (even if zero rows) so downstream optimizer can proceed/debug.
"""
import argparse, os, sys, time, json
from datetime import datetime, timezone
from typing import List, Dict
import pandas as pd

try:
    import yaml
except Exception as e:
    print("ERROR: PyYAML not installed. pip install pyyaml", file=sys.stderr)
    sys.exit(2)

try:
    import ccxt
except Exception as e:
    print("ERROR: ccxt not installed. pip install ccxt", file=sys.stderr)
    sys.exit(2)

def load_cfg(path: str) -> dict:
    with open(path, "r") as f:
        return yaml.safe_load(f)

def connect_bybit(cfg: dict):
    ex_cfg = (cfg.get("exchange") or {})
    api_key = ex_cfg.get("api_key") or os.getenv("BYBIT_API_KEY")
    api_secret = ex_cfg.get("api_secret") or os.getenv("BYBIT_API_SECRET")
    if not api_key or not api_secret:
        print("ERROR: missing BYBIT apiKey/apiSecret (env or config).", file=sys.stderr)
        sys.exit(3)
    params = ex_cfg.get("params") or {}
    options = {
        **params,
        "defaultType": params.get("defaultType", "swap"),
        "defaultSubType": params.get("defaultSubType", "linear"),
        "accountType": params.get("accountType", "UNIFIED"),
        "unifiedMargin": True,
    }
    # Optional overrides via env
    if os.getenv("BYBIT_ACCOUNT_TYPE"):
        options["accountType"] = os.getenv("BYBIT_ACCOUNT_TYPE")
    if os.getenv("BYBIT_SUBACCOUNT_ID"):
        options["subAccountId"] = os.getenv("BYBIT_SUBACCOUNT_ID")
    ex = ccxt.bybit({
        "apiKey": api_key,
        "secret": api_secret,
        "enableRateLimit": True,
        "options": options,
    })
    # Testnet toggle via cfg or env
    testnet = bool(ex_cfg.get("testnet") or os.getenv("BYBIT_TESTNET"))
    try:
        ex.set_sandbox_mode(testnet)
    except Exception:
        pass
    return ex

def fetch_all_trades_linear(ex, since_ms: int, until_ms: int, limit: int = 200) -> List[dict]:
    """Fetch private executions across ALL symbols in 'linear' category."""
    out: List[dict] = []
    loops = 0
    cursor = None
    params = {"category": "linear"}
    while True:
        loops += 1
        p = dict(params)
        if cursor:
            p["cursor"] = cursor
        res = ex.fetch_my_trades(symbol=None, since=since_ms, limit=limit, params=p)
        if not res:
            break
        out.extend(res)
        last_ts = res[-1].get("timestamp")
        if not last_ts or last_ts <= since_ms:
            break
        since_ms = last_ts + 1
        if len(res) < limit or loops > 1000:
            break
    # trim to until_ms
    out = [t for t in out if (t.get("timestamp") is not None and t["timestamp"] <= until_ms)]
    return out

def fifo_realized(trades: List[dict]) -> List[dict]:
    out = []
    pos: Dict[str, Dict[str, float]] = {}
    for t in trades:
        s = t.get("symbol") or (t.get("info", {}) or {}).get("symbol")
        if not s:
            continue
        ts = t.get("timestamp")
        side = t.get("side")
        price = float(t.get("price") or 0.0)
        amount = float(t.get("amount") or 0.0)
        if not ts or side not in ("buy", "sell") or amount <= 0 or price <= 0:
            continue
        book = pos.setdefault(s, {"qty": 0.0, "cost": 0.0})
        dq = amount if side == "buy" else -amount
        if book["qty"] == 0 or (book["qty"] > 0 and dq > 0) or (book["qty"] < 0 and dq < 0):
            book["cost"] += dq * price
            book["qty"] += dq
            continue
        remaining = dq
        realize_qty = -book["qty"] if abs(remaining) >= abs(book["qty"]) else -remaining
        if realize_qty != 0:
            avg_px = (book["cost"] / book["qty"]) if book["qty"] != 0 else price
            if book["qty"] > 0 and remaining < 0:
                realized = (price - avg_px) * abs(realize_qty)
            elif book["qty"] < 0 and remaining > 0:
                realized = (avg_px - price) * abs(realize_qty)
            else:
                realized = 0.0
            out.append({
                "symbol": s,
                "timestamp": ts,
                "iso": datetime.fromtimestamp(ts/1000, tz=timezone.utc).isoformat(),
                "price": price,
                "realized_qty": abs(realize_qty),
                "realized_pnl_usdt": realized,
            })
            book["qty"] += realize_qty
            book["cost"] = avg_px * book["qty"]
            remaining -= realize_qty
        if remaining != 0:
            book["cost"] += remaining * price
            book["qty"] += remaining
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--since-hours", type=int, default=72)
    ap.add_argument("--outdir", default="/opt/xsmom-bot/reports")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    cfg = load_cfg(args.config)
    ex = connect_bybit(cfg)

    end = int(time.time() * 1000)
    start = end - args.since_hours * 3600 * 1000

    if args.debug:
        try:
            bal = ex.fetch_balance(params={"accountType": ex.options.get("accountType", "UNIFIED")})
            print(json.dumps({"auth_ok": True, "nonzero_assets": [k for k,v in (bal.get("total") or {}).items() if v]}))
        except Exception as e:
            print(json.dumps({"auth_ok": False, "error": str(e)}))
            sys.exit(4)

    try:
        trades = fetch_all_trades_linear(ex, start, end, limit=max(50, min(1000, args.limit)))
    except Exception as e:
        os.makedirs(args.outdir, exist_ok=True)
        out_path = os.path.join(args.outdir, f"Bybit-AllPerp-ClosedPNL-{int(start/1000)}-{int(end/1000)}.csv")
        pd.DataFrame(columns=["timestamp","iso","symbol","realized_pnl_usdt"]).to_csv(out_path, index=False)
        print(json.dumps({"wrote": out_path, "rows": 0, "error": f"fetch_my_trades failed: {e}"}))
        sys.exit(0)

    for t in trades:
        if not t.get("symbol"):
            sym = (t.get("info", {}) or {}).get("symbol")
            if sym:
                t["symbol"] = sym

    realized_rows = fifo_realized(trades)

    os.makedirs(args.outdir, exist_ok=True)
    out_path = os.path.join(args.outdir, f"Bybit-AllPerp-ClosedPNL-{int(start/1000)}-{int(end/1000)}.csv")

    if not realized_rows:
        pd.DataFrame(columns=["timestamp","iso","symbol","realized_pnl_usdt"]).to_csv(out_path, index=False)
        print(json.dumps({"wrote": out_path, "rows": 0, "reason": "no realized pnl in window"}))
        return

    df = pd.DataFrame(realized_rows)
    df[["timestamp","iso","symbol","realized_pnl_usdt"]].to_csv(out_path, index=False)
    print(json.dumps({"wrote": out_path, "rows": int(df.shape[0])}))

if __name__ == "__main__":
    main()
