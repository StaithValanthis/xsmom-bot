import logging
import time
from datetime import datetime, timezone
from typing import Dict, List

import numpy as np
import pandas as pd

from .config import AppConfig
from .exchange import ExchangeWrapper
from .signals import regime_ok
from .sizing import build_targets, apply_liquidity_caps
from .risk import per_symbol_stops, check_soft_stop, kill_switch_should_trigger, resume_time_after_kill
from .utils import utcnow, read_json, write_json

log = logging.getLogger("live")

def wait_until_minute(minute: int, poll_seconds: int):
    while True:
        now = utcnow()
        if now.minute == minute and now.second < poll_seconds:
            return
        time.sleep(poll_seconds)

def compute_qtys(targets, prices: Dict[str, float], equity_usdt: float):
    out = {}
    for s, w in targets.items():
        px = prices.get(s, None)
        if px is None or px <= 0:
            continue
        out[s] = (w * equity_usdt) / px
    return out

def run_live(cfg: AppConfig, dry: bool):
    ex = ExchangeWrapper(cfg.exchange)
    state_path = cfg.paths.state_path
    state = read_json(state_path, default={
        "last_targets": {},
        "day_start_equity": None,
        "trading_paused_until": None,
        "universe": [],
    })

    try:
        syms = ex.fetch_markets_filtered()
        state["universe"] = syms
        write_json(state_path, state)
        log.info(f"Initial universe: {len(syms)} symbols")

        while True:
            wait_until_minute(cfg.execution.rebalance_minute, cfg.execution.poll_seconds)
            now = utcnow()
            if state.get("trading_paused_until"):
                until = datetime.fromisoformat(state["trading_paused_until"])
                if now.replace(tzinfo=timezone.utc) < until.replace(tzinfo=timezone.utc):
                    log.warning(f"Trading paused until {until.isoformat()}")
                    continue
                else:
                    state["trading_paused_until"] = None
                    write_json(state_path, state)

            # equity & day start equity
            equity = ex.fetch_balance_usdt()
            if state.get("day_start_equity") is None or now.hour == 0 and now.minute < 10:
                state["day_start_equity"] = equity
                write_json(state_path, state)

            if kill_switch_should_trigger(state.get("day_start_equity") or 0.0, equity, cfg.risk.max_daily_loss_pct):
                pause_until = resume_time_after_kill(now, cfg.risk.trade_disable_minutes)
                state["trading_paused_until"] = pause_until.isoformat()
                write_json(state_path, state)
                log.error(f"Kill switch triggered. Pausing until {pause_until.isoformat()}")
                continue

            # Fetch OHLCV for signals
            closes = {}
            latest_bars = {}
            for s in state["universe"]:
                raw = ex.fetch_ohlcv(s, timeframe=cfg.exchange.timeframe, limit=max(cfg.exchange.candles_limit, cfg.strategy.vol_lookback + 50))
                df = pd.DataFrame(raw, columns=["ts","open","high","low","close","volume"])
                df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
                df.set_index("dt", inplace=True)
                if len(df) < (max(max(cfg.strategy.lookbacks), cfg.strategy.vol_lookback) + 10):
                    continue
                closes[s] = df["close"]
                latest_bars[s] = df.iloc[-1]

            if not closes:
                log.warning("No closes collected; skipping.")
                continue

            closes = pd.concat(closes, axis=1)

            # Optional regime filter (on average basket)
            if cfg.strategy.regime_filter.enabled:
                ok = regime_ok(closes.mean(axis=1), cfg.strategy.regime_filter.ema_len, cfg.strategy.regime_filter.slope_min_bps_per_day)
                if not ok:
                    log.info("Regime filter blocking new entries this hour.")
                    continue

            # Build targets
            targets = build_targets(
                closes,
                cfg.strategy.lookbacks,
                cfg.strategy.lookback_weights,
                cfg.strategy.vol_lookback,
                cfg.strategy.k_min,
                cfg.strategy.k_max,
                cfg.strategy.market_neutral,
                cfg.strategy.gross_leverage,
                cfg.strategy.max_weight_per_asset,
                dynamic_k_fn=lambda sc, kmin, kmax: (kmin, kmax),  # can replace with true dispersion fn if desired
            )

            # Liquidity caps
            ticks = ex.fetch_tickers(list(targets.index))
            targets = apply_liquidity_caps(
                targets,
                equity,
                ticks,
                cfg.liquidity.adv_cap_pct,
                cfg.liquidity.notional_cap_usdt,
            )

            # Prices
            prices = {s: ex.fetch_price(s) for s in targets.index}
            qtys = compute_qtys(targets, prices, equity)

            # Current positions
            pos_map = ex.fetch_positions_map()
            current_qtys = {}
            for s in targets.index:
                p = pos_map.get(s)
                cur = 0.0
                if p:
                    qty = float(p.get("contracts") or p.get("positionAmt") or 0.0)
                    side = p.get("side")
                    if side == "short" and qty > 0:
                        qty = -qty
                    cur = qty
                current_qtys[s] = cur

            # set leverage (best-effort)
            lev = cfg.execution.set_leverage
            for s in targets.index:
                if lev and lev > 0:
                    ex.try_set_leverage(s, lev)

            # Order diffs
            orders = []
            for s in targets.index:
                tgt = qtys.get(s, 0.0)
                cur = current_qtys.get(s, 0.0)
                diff = tgt - cur
                if abs(diff) < 1e-8:
                    continue
                side = "buy" if diff > 0 else "sell"
                px = prices.get(s)
                order_px = None
                if cfg.execution.order_type.lower() == "limit" and px:
                    sign = 1 if side == "buy" else -1
                    order_px = px * (1.0 + sign * (cfg.execution.price_offset_bps / 10_000))
                q_diff, q_px = ex.quantize(s, diff, order_px)
                if q_diff != 0.0:
                    orders.append((s, side, q_diff, q_px))

            for (s, side, q, px) in orders:
                try:
                    if dry:
                        log.info(f"[DRY] {s}: {side} {q} @ {px or 'mkt'}")
                    else:
                        ex.create_order_safe(s, side, q, px, post_only=cfg.execution.post_only, reduce_only=False)
                        log.info(f"[LIVE] {s}: {side} {q} @ {px or 'mkt'}")
                except Exception as e:
                    log.error(f"Order error {s} {side} {q}: {e}")

            # Soft stops per symbol
            for s in targets.index:
                try:
                    raw = ex.fetch_ohlcv(s, timeframe=cfg.exchange.timeframe, limit=60)
                    df = pd.DataFrame(raw, columns=["ts","open","high","low","close","volume"])
                    df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
                    df.set_index("dt", inplace=True)
                    if len(df) < 20:
                        continue
                    sl_long, sl_short, tp_long, tp_short = per_symbol_stops(df, cfg.risk.atr_mult_sl, cfg.risk.atr_mult_tp, cfg.risk.use_tp)
                    lr = df.iloc[-1]

                    tgt = qtys.get(s, 0.0)
                    cur = current_qtys.get(s, 0.0)
                    if cur == 0.0:
                        continue
                    if cur > 0:  # long
                        if check_soft_stop(lr, "long", sl_long):
                            q = cur
                            q, _ = ex.quantize(s, q, None)
                            if q > 0:
                                if dry:
                                    log.info(f"[DRY-STOP] {s}: sell {q} @ mkt (long SL)")
                                else:
                                    ex.create_order_safe(s, "sell", q, None, post_only=False, reduce_only=True)
                        elif cfg.risk.use_tp and check_soft_stop(lr, "short", tp_long):  # use high>=tp
                            q = cur
                            q, _ = ex.quantize(s, q, None)
                            if q > 0:
                                if dry:
                                    log.info(f"[DRY-TP] {s}: sell {q} @ mkt (long TP)")
                                else:
                                    ex.create_order_safe(s, "sell", q, None, post_only=False, reduce_only=True)
                    elif cur < 0:  # short
                        if check_soft_stop(lr, "short", sl_short):
                            q = abs(cur)
                            q, _ = ex.quantize(s, q, None)
                            if q > 0:
                                if dry:
                                    log.info(f"[DRY-STOP] {s}: buy {q} @ mkt (short SL)")
                                else:
                                    ex.create_order_safe(s, "buy", q, None, post_only=False, reduce_only=True)
                        elif cfg.risk.use_tp and check_soft_stop(lr, "long", tp_short):  # low<=tp (mirror)
                            q = abs(cur)
                            q, _ = ex.quantize(s, q, None)
                            if q > 0:
                                if dry:
                                    log.info(f"[DRY-TP] {s}: buy {q} @ mkt (short TP)")
                                else:
                                    ex.create_order_safe(s, "buy", q, None, post_only=False, reduce_only=True)

            state["last_targets"] = targets.to_dict()
            # keep day_start_equity; trading_paused_until if set
            write_json(state_path, state)

    finally:
        ex.close()
