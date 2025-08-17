#!/usr/bin/env python3
# multi_pair_xsmom.py
# Cross-Sectional Momentum (XSMOM) multi-pair bot for Bybit USDT-perp
# Hourly rebalance; backtest + live trading in one file

import os
import time
import math
import json
import argparse
import asyncio
import logging
from dataclasses import dataclass, field
from typing import List, Dict, Tuple, Optional
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd
from pydantic import BaseModel
from tenacity import retry, stop_after_attempt, wait_exponential

# Use synchronous CCXT for simplicity (robust enough for hourly loop)
import ccxt

# =============== CONFIG ===============

@dataclass
class BotConfig:
    # Exchange & account
    exchange_id: str = "bybit"
    account_type: str = "swap"           # "swap" or "spot"
    quote: str = "USDT"                  # quote currency filter
    unified_margin: bool = True          # Bybit unified margin accounts
    testnet: bool = False

    # Universe & data
    timeframe: str = "1h"
    candles_limit: int = 1000            # for backtest & warmup
    min_usd_volume_24h: float = 1_000_000  # liquidity filter
    min_price: float = 0.005             # filter dust
    only_perps: bool = True              # USDT perps only
    max_symbols: int = 120               # safety cap (avoid too many)

    # Strategy
    lookbacks: Tuple[int, int, int] = (1, 6, 24)        # hours
    lookback_weights: Tuple[float, float, float] = (1.0, 1.0, 1.0)  # sum arbitrary
    vol_lookback: int = 48                               # hours for stdev/ATR
    top_k: int = 10
    bottom_k: int = 10
    market_neutral: bool = True                          # long top_k, short bottom_k
    gross_leverage: float = 3.0                          # sum of |weights|
    max_weight_per_asset: float = 0.10                   # per-leg cap

    # Risk / stops
    atr_mult_sl: float = 2.5                             # soft stop (ATR multiples)
    atr_mult_tp: float = 4.0                             # optional soft take profit
    use_tp: bool = False

    # Execution
    order_type: str = "market"       # "limit" or "market"
    price_offset_bps: int = 5        # if limit: ±5 bps from mid
    post_only: bool = True
    slippage_bps_guard: int = 20     # if mid-to-last > guard, skip

    # Rebalance
    rebalance_minute: int = 5        # run at hh:05 each hour
    poll_seconds: int = 15           # sleep between checks

    # Modes
    dry_run: bool = True
    verbose: bool = True
    save_state_path: str = "state_xsmom.json"

# =============== HELPERS ===============

def utcnow() -> datetime:
    return datetime.now(timezone.utc)

def to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)

def humanize_ts(ts_ms: int) -> str:
    return datetime.utcfromtimestamp(ts_ms / 1000.0).strftime("%Y-%m-%d %H:%M:%S")

def ensure_leq(x, limit):
    return x if abs(x) <= limit else math.copysign(limit, x)

def safe_div(a, b, default=0.0):
    return a / b if b not in (0, None, np.nan) else default

# =============== EXCHANGE WRAPPER ===============

class ExchangeWrapper:
    def __init__(self, cfg: BotConfig):
        api_key = os.getenv("BYBIT_API_KEY", "")
        secret = os.getenv("BYBIT_API_SECRET", "")

        klass = getattr(ccxt, cfg.exchange_id)
        self.exchange = klass({
            "apiKey": api_key,
            "secret": secret,
            "enableRateLimit": True,
            "options": {
                "defaultType": "swap" if cfg.account_type == "swap" else "spot",
                "createOrder": {
                    "reduceOnly": True,  # supported on bybit
                },
            },
        })
        if cfg.testnet and hasattr(self.exchange, "set_sandbox_mode"):
            self.exchange.set_sandbox_mode(True)

        self.cfg = cfg

    def close(self):
        try:
            self.exchange.close()
        except Exception:
            pass

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    def load_markets(self):
        return self.exchange.load_markets()

    def fetch_markets_filtered(self) -> List[str]:
        markets = self.load_markets()
        symbols = []
        for sym, m in markets.items():
            if m.get("active") is not True:
                continue
            if self.cfg.only_perps:
                if (m.get("swap") is True) and m.get("quote") == self.cfg.quote:
                    if m.get("type") == "swap" or m.get("contract", False):
                        if m.get("settle") in (self.cfg.quote, None) and m.get("linear", True):
                            symbols.append(sym)
            else:
                if m.get("spot") is True and m.get("quote") == self.cfg.quote:
                    symbols.append(sym)
        # Liquidity filter using tickers (24h notional)
        if len(symbols) == 0:
            return symbols

        tick = self.exchange.fetch_tickers(symbols)
        keep = []
        for s in symbols:
            t = tick.get(s, {})
            last = t.get("last") or t.get("close") or 0.0
            quote_vol = t.get("quoteVolume", 0.0)  # 24h quote volume in quote currency
            if (last or 0) >= self.cfg.min_price and (quote_vol or 0) >= self.cfg.min_usd_volume_24h:
                keep.append(s)
        keep = sorted(keep)
        if self.cfg.max_symbols and len(keep) > self.cfg.max_symbols:
            keep = keep[: self.cfg.max_symbols]
        return keep

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> List[List[float]]:
        return self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)

    def fetch_positions_map(self) -> Dict[str, dict]:
        try:
            pos = self.exchange.fetch_positions()
        except Exception:
            pos = []
        out = {}
        for p in pos:
            sym = p.get("symbol")
            if not sym:
                continue
            out[sym] = p
        return out

    def fetch_price(self, symbol: str) -> Optional[float]:
        try:
            t = self.exchange.fetch_ticker(symbol)
            return t.get("last") or t.get("close")
        except Exception:
            return None

    def get_precision(self, symbol: str) -> Tuple[float, float]:
        m = self.exchange.market(symbol)
        amt = m.get("precision", {}).get("amount", None)
        price = m.get("precision", {}).get("price", None)
        return amt or 0.0001, price or 0.0001

    def quantize(self, symbol: str, amount: float, price: Optional[float]) -> Tuple[float, Optional[float]]:
        m = self.exchange.market(symbol)
        amt_step = m.get("limits", {}).get("amount", {}).get("min") or 0.0
        px_step = m.get("precision", {}).get("price", 4)
        # CCXT will usually round correctly. Here just ensure non-zero and positive.
        q_amount = float(self.exchange.amount_to_precision(symbol, amount))
        q_price = None if price is None else float(self.exchange.price_to_precision(symbol, price))
        if amt_step and abs(q_amount) < amt_step:
            q_amount = 0.0
        return q_amount, q_price

    def create_order_safe(self, symbol: str, side: str, amount: float, price: Optional[float], reduce_only=False):
        params = {}
        if reduce_only:
            params["reduceOnly"] = True

        if self.cfg.order_type == "limit" and price is not None:
            params["timeInForce"] = "PostOnly" if self.cfg.post_only else "GTC"
            return self.exchange.create_limit_order(symbol, side, amount, price, params)
        else:
            # Fallback to market
            return self.exchange.create_market_order(symbol, side, amount, params)

# =============== STRATEGY CORE ===============

def compute_atr(df: pd.DataFrame, n=14) -> pd.Series:
    # df must have columns: ['open','high','low','close']
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low).abs(),
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(n).mean()

def compute_scores(prices: pd.DataFrame,
                   lookbacks=(1,6,24),
                   weights=(1.0,1.0,1.0)) -> pd.Series:
    """
    prices: wide DataFrame, columns=symbols, index=timestamp, values=close
    returns: momentum score per symbol (last row, Series)
    """
    rets = prices.pct_change()
    score = pd.Series(0.0, index=prices.columns)
    for lb, w in zip(lookbacks, weights):
        lb_ret = (prices / prices.shift(lb) - 1.0).iloc[-1]
        score = score.add(w * lb_ret, fill_value=0.0)
    return score

def inverse_vol_weights(prices: pd.DataFrame, vol_lookback=48) -> pd.Series:
    rets = prices.pct_change().iloc[-vol_lookback:]
    vol = rets.std()
    iv = 1.0 / vol.replace(0, np.nan)
    iv = iv / iv.sum()
    iv = iv.replace(np.nan, 0.0)
    return iv

def build_targets(prices: pd.DataFrame,
                  lookbacks=(1,6,24),
                  weights=(1.0,1.0,1.0),
                  vol_lookback=48,
                  top_k=10, bottom_k=10,
                  market_neutral=True,
                  gross_leverage=2.0,
                  max_weight_per_asset=0.10) -> pd.Series:
    """
    Returns target weights per symbol summing to +/- gross_leverage (market-neutral) or <= gross_leverage long-only.
    """
    assert prices.shape[0] > max(max(lookbacks), vol_lookback) + 5, "Not enough bars"
    last_prices = prices.iloc[-1]
    # Filter out stalled or nan prices
    valid_cols = last_prices[last_prices.notna() & (last_prices > 0)].index
    prices = prices[valid_cols]

    score = compute_scores(prices, lookbacks, weights)
    iv = inverse_vol_weights(prices, vol_lookback)

    ranked = score.sort_values(ascending=False)
    longs = ranked.index[:top_k]
    shorts = ranked.index[-bottom_k:]

    w = pd.Series(0.0, index=prices.columns)
    if market_neutral:
        # long leg weights ∝ score*iv, normalized to +gross/2; short leg to -gross/2
        long_raw = (score.loc[longs].clip(lower=0.0)) * iv.loc[longs]
        short_raw = (-score.loc[shorts].clip(upper=0.0)) * iv.loc[shorts]
        long_w = long_raw / long_raw.sum() if long_raw.sum() > 0 else pd.Series(0.0, index=longs)
        short_w = short_raw / short_raw.sum() if short_raw.sum() > 0 else pd.Series(0.0, index=shorts)
        long_w = (gross_leverage / 2.0) * long_w
        short_w = -(gross_leverage / 2.0) * short_w
        w.loc[long_w.index] = long_w
        w.loc[short_w.index] = short_w
    else:
        pos_raw = (score.clip(lower=0.0)) * iv
        pos_w = pos_raw / pos_raw.sum() if pos_raw.sum() > 0 else pd.Series(0.0, index=prices.columns)
        w = gross_leverage * pos_w

    # Cap per-asset
    w = w.apply(lambda x: ensure_leq(x, max_weight_per_asset))
    # Re-normalize to match gross leverage when market-neutral
    if market_neutral:
        gross = w.abs().sum()
        if gross > 0:
            w = w * (gross_leverage / gross)
    else:
        gross = w.sum()
        if gross > gross_leverage:
            w = w * (gross_leverage / gross)

    # Drop 0s
    w = w[w.abs() > 1e-8]
    return w.round(6)

def build_soft_levels(df: pd.DataFrame, atr_mult_sl=2.5, atr_mult_tp=4.0) -> Tuple[pd.Series, pd.Series]:
    atr = compute_atr(df, n=14)
    close = df["close"]
    sl_long = close - atr_mult_sl * atr
    tp_long = close + atr_mult_tp * atr
    sl_short = close + atr_mult_sl * atr
    tp_short = close - atr_mult_tp * atr
    return (sl_long.combine(sl_short, max), tp_long.combine(tp_short, min))  # not used directly; illustrate concept

# =============== BACKTEST ===============

def backtest_ccxt(exchange: ExchangeWrapper, cfg: BotConfig, symbols: List[str]) -> None:
    # Pull OHLCV for each symbol, align into wide frames
    tf = cfg.timeframe
    limit = cfg.candles_limit

    bars = {}
    for s in symbols:
        try:
            raw = exchange.fetch_ohlcv(s, timeframe=tf, limit=limit)
            df = pd.DataFrame(raw, columns=["ts","open","high","low","close","volume"])
            df["datetime"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
            df.set_index("datetime", inplace=True)
            bars[s] = df
        except Exception as e:
            print(f"[WARN] Failed OHLCV {s}: {e}")

    if not bars:
        print("No bars fetched; backtest aborted.")
        return

    # Align closes
    closes = pd.concat({s: bars[s]["close"] for s in bars}, axis=1).dropna(how="all")
    # Simple hourly rebalance walk-forward
    equity = [1.0]
    idx = closes.index
    weights_hist = []
    prev_weights = pd.Series(0.0, index=closes.columns)

    # Precompute returns
    rets = closes.pct_change().fillna(0.0)

    for i in range(max(max(cfg.lookbacks), cfg.vol_lookback) + 5, len(idx) - 1):
        window = closes.iloc[:i+1]
        w = build_targets(window,
                          lookbacks=cfg.lookbacks,
                          weights=cfg.lookback_weights,
                          vol_lookback=cfg.vol_lookback,
                          top_k=cfg.top_k, bottom_k=cfg.bottom_k,
                          market_neutral=cfg.market_neutral,
                          gross_leverage=cfg.gross_leverage,
                          max_weight_per_asset=cfg.max_weight_per_asset)
        weights_hist.append(w.reindex(closes.columns).fillna(0.0))
        # one-bar forward return of the *portfolio*
        port_ret = (rets.iloc[i+1] * weights_hist[-1]).sum()
        # no explicit fee/slippage modeling here (add later)
        equity.append(equity[-1] * (1.0 + port_ret))

    eq = pd.Series(equity, index=idx[-len(equity):])
    total_ret = eq.iloc[-1] - 1.0
    cagr = (eq.iloc[-1]) ** (24*365 / max(1, len(eq))) - 1.0  # rough (hourly → annual)
    dd = (eq / eq.cummax() - 1.0).min()

    print("\n=== BACKTEST RESULTS (rough, no fees/slippage) ===")
    print(f"Samples: {len(eq)} bars  |  Universe size: {len(closes.columns)}")
    print(f"Total Return: {total_ret:.2%}")
    print(f"Max Drawdown: {dd:.2%}")
    print(f"Rough Annualized: {cagr:.2%}")
    print("=================================================\n")

# =============== LIVE LOOP ===============

class LiveState(BaseModel):
    last_rebalance_ts: Optional[int] = None
    last_targets: Dict[str, float] = {}
    universe: List[str] = []

def load_state(path: str) -> LiveState:
    if not os.path.exists(path):
        return LiveState()
    with open(path, "r") as f:
        return LiveState(**json.load(f))

def save_state(path: str, state: LiveState):
    with open(path, "w") as f:
        json.dump(state.model_dump(), f, indent=2)

def wait_until_minute(minute: int, poll_seconds=15):
    while True:
        now = utcnow()
        if now.minute == minute and now.second < poll_seconds:
            return
        time.sleep(poll_seconds)

def compute_target_qtys(exchange: ExchangeWrapper, cfg: BotConfig, targets: pd.Series,
                        prices: Dict[str, float], equity_usdt: float) -> Dict[str, float]:
    # For perps: position size in base contracts ~ (weight * equity) / price
    qtys = {}
    for sym, w in targets.items():
        px = prices.get(sym)
        if px is None or px <= 0:
            continue
        notional = w * equity_usdt
        qty = notional / px
        # Quantize
        q_qty, _ = exchange.quantize(sym, qty, None)
        if q_qty != 0.0:
            qtys[sym] = q_qty
    return qtys

def live_once(exchange: ExchangeWrapper, cfg: BotConfig, state: LiveState):
    # 1) Universe
    if not state.universe:
        syms = exchange.fetch_markets_filtered()
        if cfg.verbose:
            print(f"[Universe] {len(syms)} symbols after filters.")
        state.universe = syms

    if not state.universe:
        print("Empty universe; skipping cycle.")
        return state

    # 2) Fetch OHLCV (close) for signals
    closes = {}
    latest_bars = {}
    for s in state.universe:
        raw = exchange.fetch_ohlcv(s, timeframe=cfg.timeframe, limit=max(cfg.candles_limit, cfg.vol_lookback+50))
        df = pd.DataFrame(raw, columns=["ts","open","high","low","close","volume"])
        df["datetime"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        df.set_index("datetime", inplace=True)
        if len(df) < (max(max(cfg.lookbacks), cfg.vol_lookback) + 10):
            continue
        closes[s] = df["close"]
        latest_bars[s] = df.iloc[-1]  # for ATR/stop if needed

    if not closes:
        print("No closes collected; skipping.")
        return state

    closes = pd.concat(closes, axis=1)
    # 3) Build targets
    targets = build_targets(closes,
                            lookbacks=cfg.lookbacks,
                            weights=cfg.lookback_weights,
                            vol_lookback=cfg.vol_lookback,
                            top_k=cfg.top_k, bottom_k=cfg.bottom_k,
                            market_neutral=cfg.market_neutral,
                            gross_leverage=cfg.gross_leverage,
                            max_weight_per_asset=cfg.max_weight_per_asset)

    # 4) Prices for execution
    prices = {}
    for s in targets.index:
        prices[s] = exchange.fetch_price(s)

    # 5) Equity (assume USDT balance; for unified margin you might sum wallet balances)
    equity_usdt = None
    try:
        bal = exchange.exchange.fetch_balance()
        # Use total or free USDT; pick something conservative
        usdt_info = bal.get(cfg.quote, {})
        equity_usdt = float(usdt_info.get("total") or usdt_info.get("free") or 0.0)
    except Exception:
        equity_usdt = 0.0

    if cfg.verbose:
        print(f"[Equity] {equity_usdt:.2f} {cfg.quote} | Targets: {len(targets)}")

    # 6) Convert targets to qty
    target_qtys = compute_target_qtys(exchange, cfg, targets, prices, equity_usdt)

    # 7) Current positions
    pos_map = exchange.fetch_positions_map()
    current_qtys = {}
    for s in targets.index:
        p = pos_map.get(s)
        if p:
            qty = float(p.get("contracts") or p.get("positionAmt") or 0.0)
            # Bybit long>0, short<0 typically; normalize sign
            side = p.get("side")
            if side == "short" and qty > 0:
                qty = -qty
            current_qtys[s] = qty
        else:
            current_qtys[s] = 0.0

    # 8) Diff → orders
    orders = []
    for s in targets.index:
        tgt = target_qtys.get(s, 0.0)
        cur = current_qtys.get(s, 0.0)
        diff = tgt - cur
        if abs(diff) < 1e-8:
            continue
        side = "buy" if diff > 0 else "sell"

        px = prices.get(s)
        order_px = None
        if cfg.order_type == "limit" and px:
            sign = 1 if side == "buy" else -1
            order_px = px * (1.0 + sign * (cfg.price_offset_bps / 10_000))
        q_diff, q_px = exchange.quantize(s, diff, order_px)
        if q_diff == 0.0:
            continue
        orders.append((s, side, q_diff, q_px))

    # 9) Place
    for (s, side, q, px) in orders:
        try:
            if cfg.dry_run:
                print(f"[DRY] {s}: {side} {q} @ {px or 'mkt'}")
            else:
                exchange.create_order_safe(s, side, abs(q), px, reduce_only=False)
                print(f"[LIVE] {s}: {side} {q} @ {px or 'mkt'}")
        except Exception as e:
            print(f"[ERROR] order {s} {side} {q}: {e}")

    # 10) Optional soft stops (hourly check)
    if cfg.atr_mult_sl > 0:
        for s in targets.index:
            df = None
            try:
                raw = exchange.fetch_ohlcv(s, timeframe=cfg.timeframe, limit=60)
                df = pd.DataFrame(raw, columns=["ts","open","high","low","close","volume"])
            except Exception:
                continue
            if df is None or len(df) < 20:
                continue
            df["datetime"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
            df.set_index("datetime", inplace=True)
            atr = compute_atr(df, 14)
            close = df["close"].iloc[-1]
            last_atr = atr.iloc[-1]
            tgt = target_qtys.get(s, 0.0)
            if tgt == 0.0:
                continue
            # Check stop per side
            if tgt > 0:
                stop = close - cfg.atr_mult_sl * last_atr
                # If last bar low <= stop → exit
                if df["low"].iloc[-1] <= stop:
                    # reduce-only sell to flat
                    q = current_qtys.get(s, 0.0)
                    if q > 0:
                        q, _ = exchange.quantize(s, q, None)
                        if q > 0:
                            try:
                                if cfg.dry_run:
                                    print(f"[DRY-STOP] {s}: sell {q} @ mkt (long stop)")
                                else:
                                    exchange.create_order_safe(s, "sell", q, None, reduce_only=True)
                            except Exception as e:
                                print(f"[ERROR stop] {s}: {e}")
            elif tgt < 0:
                stop = close + cfg.atr_mult_sl * last_atr
                if df["high"].iloc[-1] >= stop:
                    q = current_qtys.get(s, 0.0)
                    if q < 0:
                        q = abs(q)
                        q, _ = exchange.quantize(s, q, None)
                        if q > 0:
                            try:
                                if cfg.dry_run:
                                    print(f"[DRY-STOP] {s}: buy {q} @ mkt (short stop)")
                                else:
                                    exchange.create_order_safe(s, "buy", q, None, reduce_only=True)
                            except Exception as e:
                                print(f"[ERROR stop] {s}: {e}")

    state.last_rebalance_ts = int(time.time() * 1000)
    state.last_targets = targets.to_dict()
    return state

def live_loop(cfg: BotConfig):
    logging.basicConfig(level=logging.INFO if not cfg.verbose else logging.DEBUG)
    ex = ExchangeWrapper(cfg)
    try:
        state = load_state(cfg.save_state_path)
        # Universe preview
        syms = ex.fetch_markets_filtered()
        print(f"Initial universe: {len(syms)} symbols")
        state.universe = syms

        while True:
            wait_until_minute(cfg.rebalance_minute, cfg.poll_seconds)
            print(f"\n[{utcnow().strftime('%Y-%m-%d %H:%M:%S')}] Rebalance start")
            state = live_once(ex, cfg, state)
            save_state(cfg.save_state_path, state)
    finally:
        ex.close()

# =============== MAIN ===============

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backtest", type=int, default=0, help="Backtest candles limit (0 to skip)")
    parser.add_argument("--live", action="store_true", help="Run live loop")
    parser.add_argument("--dry", action="store_true", help="Dry run (no real orders)")
    parser.add_argument("--limit", type=int, default=None, help="Override candles_limit")
    parser.add_argument("--topk", type=int, default=None)
    parser.add_argument("--bottomk", type=int, default=None)
    parser.add_argument("--gross", type=float, default=None)
    parser.add_argument("--neutral", action="store_true", help="Force market-neutral")
    parser.add_argument("--longonly", action="store_true", help="Force long-only")
    args = parser.parse_args()

    cfg = BotConfig()
    if args.limit:
        cfg.candles_limit = args.limit
    if args.topk is not None:
        cfg.top_k = args.topk
    if args.bottomk is not None:
        cfg.bottom_k = args.bottomk
    if args.gross is not None:
        cfg.gross_leverage = args.gross
    if args.neutral:
        cfg.market_neutral = True
    if args.longonly:
        cfg.market_neutral = False
        cfg.bottom_k = 0
    if args.dry:
        cfg.dry_run = True
    else:
        cfg.dry_run = False

    ex = ExchangeWrapper(cfg)
    try:
        symbols = ex.fetch_markets_filtered()
        if args.backtest > 0:
            cfg.candles_limit = args.backtest
            print(f"Backtesting on {len(symbols)} symbols...")
            backtest_ccxt(ex, cfg, symbols)
        if args.live:
            live_loop(cfg)
    finally:
        ex.close()

if __name__ == "__main__":
    main()
