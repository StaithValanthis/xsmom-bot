import logging
import time
from time import perf_counter
from datetime import datetime, timezone, timedelta
from typing import Dict

import numpy as np
import pandas as pd

from .config import AppConfig
from .exchange import ExchangeWrapper
from .signals import regime_ok, compute_atr
from .sizing import build_targets, apply_liquidity_caps
from .risk import (
    per_symbol_stops,
    check_soft_stop,
    kill_switch_should_trigger,
    resume_time_after_kill,
)
from .utils import utcnow, read_json, write_json

log = logging.getLogger("live")


def wait_until_minute(minute, poll_seconds: int):
    while True:
        now = utcnow()
        log.debug(f"Heartbeat: waiting for rebalance minute={minute}, now={now.isoformat()}")
        if isinstance(minute, str) and minute == "*":
            if now.second < poll_seconds:
                return
        else:
            if now.minute == int(minute) and now.second < poll_seconds:
                return
        time.sleep(poll_seconds)


def compute_qtys(targets, prices: Dict[str, float], equity_usdt: float):
    out = {}
    items = targets.items() if isinstance(targets, dict) else targets.to_dict().items()
    for s, w in items:
        try:
            px = prices.get(s, None)
            if px is None or px <= 0:
                continue
            out[s] = (float(w) * float(equity_usdt)) / float(px)
        except Exception as e:
            log.debug(f"Qty calc skipped for {s}: {e}")
            continue
    return out


def next_rebalance_at(minute):
    now = utcnow().replace(second=0, microsecond=0)
    if isinstance(minute, str) and minute == "*":
        return now + timedelta(minutes=1)
    try:
        m = int(minute)
    except Exception:
        m = 0
    candidate = now.replace(minute=m)
    if candidate <= now:
        candidate += timedelta(hours=1)
    return candidate


def refresh_universe(ex: ExchangeWrapper, state: dict, state_path: str):
    try:
        syms = ex.fetch_markets_filtered()
        state["universe"] = syms
        state["last_universe_refresh"] = utcnow().date().isoformat()
        write_json(state_path, state)
        preview = ", ".join(syms)
        log.info(f"Universe refreshed: {len(syms)} symbols: {preview}")
    except Exception as e:
        log.error(f"Universe refresh failed: {e}")


def run_live(cfg: AppConfig, dry: bool):
    ex = ExchangeWrapper(cfg.exchange)
    state_path = cfg.paths.state_path
    state = read_json(
        state_path,
        default={
            "last_targets": {},
            "day_start_equity": None,
            "trading_paused_until": None,
            "universe": [],
            "last_universe_refresh": None,
            # NEW: per-symbol trailing/entry state
            "perpos": {},  # {sym: {sign, entry_price, entry_atr, trail_hh, trail_ll, partial_done}}
        },
    )

    try:
        refresh_universe(ex, state, state_path)
        log.info(f"Initial universe: {len(state.get('universe', []))} symbols")

        first_cycle = True

        while True:
            if first_cycle:
                log.info("First cycle running immediately; subsequent cycles align to rebalance_minute.")
                first_cycle = False
                now = utcnow()
            else:
                target = next_rebalance_at(cfg.execution.rebalance_minute)
                log.info(f"Waiting until {target.isoformat()} to rebalance...")
                wait_until_minute(cfg.execution.rebalance_minute, cfg.execution.poll_seconds)
                now = utcnow()

            # Optional funding-window deferral
            try:
                align_min = int(getattr(cfg.execution, "align_after_funding_minutes", 0))
                funding_hours = set(getattr(cfg.execution, "funding_hours_utc", [0, 8, 16]) or [])
                if align_min > 0 and now.hour in funding_hours and now.minute < align_min:
                    log.info(f"Funding window deferral: waiting until minute {align_min:02d} this hour...")
                    wait_until_minute(align_min, cfg.execution.poll_seconds)
                    now = utcnow()
            except Exception as e:
                log.debug(f"Funding alignment skipped: {e}")

            cycle_started_at = now
            log.info(f"=== Cycle start {cycle_started_at.isoformat()} ===")

            # Daily universe refresh
            try:
                last_ref = state.get("last_universe_refresh")
                if last_ref is None or now.date().isoformat() > last_ref:
                    refresh_universe(ex, state, state_path)
            except Exception as e:
                log.warning(f"Daily universe refresh check failed: {e}")

            # Pause gate
            try:
                paused_until_iso = state.get("trading_paused_until")
                if paused_until_iso:
                    try:
                        until = datetime.fromisoformat(paused_until_iso)
                    except Exception:
                        until = None
                    if until is not None and now.replace(tzinfo=timezone.utc) < until.replace(tzinfo=timezone.utc):
                        log.warning(f"Trading paused until {until.isoformat()}")
                        log.info(f"=== Cycle end (paused) {utcnow().isoformat()} ===")
                        continue
                    else:
                        state["trading_paused_until"] = None
                        write_json(state_path, state)
            except Exception as e:
                log.warning(f"Pause gate check failed: {e}")

            # Equity & kill switch
            try:
                equity = ex.fetch_balance_usdt()
                log.info(
                    f"Equity snapshot: {equity:.2f} USDT | "
                    f"config.gross_leverage={cfg.strategy.gross_leverage} | "
                    f"config.set_leverage={cfg.execution.set_leverage}"
                )

                if state.get("day_start_equity") is None or (now.hour == 0 and now.minute < 10):
                    state["day_start_equity"] = equity
                    write_json(state_path, state)
                if kill_switch_should_trigger(state.get("day_start_equity") or 0.0, equity, cfg.risk.max_daily_loss_pct):
                    pause_until = resume_time_after_kill(now, cfg.risk.trade_disable_minutes)
                    state["trading_paused_until"] = pause_until.isoformat()
                    write_json(state_path, state)
                    log.error(f"Kill switch triggered. Pausing until {pause_until.isoformat()}")
                    log.info(f"=== Cycle end (kill switch) {utcnow().isoformat()} ===")
                    continue
            except Exception as e:
                log.error(f"Equity/kill-switch step failed: {e}")
                log.info(f"=== Cycle end (error) {utcnow().isoformat()} ===")
                continue

            # OHLCV & closes
            t_ohlcv_start = perf_counter()
            closes = {}
            latest_bars = {}
            skipped_short = 0
            try:
                universe = list(state.get("universe", []))
                if not universe:
                    refresh_universe(ex, state, state_path)
                    universe = list(state.get("universe", []))

                pause_ms = getattr(cfg.exchange, "ohlcv_pause_ms", None)
                need = max(max(cfg.strategy.lookbacks), cfg.strategy.vol_lookback) + 10

                for s in universe:
                    try:
                        raw = ex.fetch_ohlcv(
                            s,
                            timeframe=cfg.exchange.timeframe,
                            limit=max(cfg.exchange.candles_limit, cfg.strategy.vol_lookback + 50),
                        )
                        if not raw:
                            continue
                        df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
                        df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
                        df.set_index("dt", inplace=True)
                        if len(df) < need:
                            skipped_short += 1
                            continue
                        closes[s] = df["close"]
                        latest_bars[s] = df.iloc[-1]
                    finally:
                        if pause_ms and pause_ms > 0:
                            time.sleep(pause_ms / 1000.0)
            except Exception as e:
                log.error(f"OHLCV aggregation failed: {e}")

            t_ohlcv = perf_counter() - t_ohlcv_start
            log.info(
                f"OHLCV: fetched {len(closes)}/{len(state.get('universe', []))} symbols "
                f"(skipped_short={skipped_short}) in {t_ohlcv:.2f}s; limit={cfg.exchange.candles_limit}"
            )

            if not closes:
                log.warning("No closes collected this cycle; skipping signal build.")
                log.info(f"=== Cycle end (no data) {utcnow().isoformat()} ===")
                continue

            try:
                closes = pd.concat(closes, axis=1)
            except Exception as e:
                log.error(f"Failed to concat closes: {e}")
                log.info(f"=== Cycle end (concat error) {utcnow().isoformat()} ===")
                continue

            # Regime filter
            try:
                if cfg.strategy.regime_filter.enabled:
                    ok = regime_ok(
                        closes.mean(axis=1),
                        cfg.strategy.regime_filter.ema_len,
                        cfg.strategy.regime_filter.slope_min_bps_per_day,
                    )
                    if not ok:
                        log.info("Regime filter blocking new entries this cycle.")
                        log.info(f"=== Cycle end (regime) {utcnow().isoformat()} ===")
                        continue
            except Exception as e:
                log.warning(f"Regime filter calc failed (not blocking): {e}")

            # Targets
            try:
                t_targets_start = perf_counter()
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
                    dynamic_k_fn=lambda sc, kmin, kmax: (kmin, kmax),
                )
                t_targets = perf_counter() - t_targets_start
                log.info(f"Targets built for {len(targets)} symbols in {t_targets:.2f}s")
            except Exception as e:
                log.error(f"Target build failed: {e}")
                log.info(f"=== Cycle end (target error) {utcnow().isoformat()} ===")
                continue

            # Liquidity caps
            try:
                t_liq_start = perf_counter()
                ticks = ex.fetch_tickers(list(targets.index))
                targets = apply_liquidity_caps(
                    targets,
                    equity,
                    ticks,
                    cfg.liquidity.adv_cap_pct,
                    cfg.liquidity.notional_cap_usdt,
                )
                t_liq = perf_counter() - t_liq_start
                log.info(f"Liquidity caps applied in {t_liq:.2f}s")
            except Exception as e:
                log.warning(f"Liquidity capping failed; using raw targets: {e}")

            # Prices & qtys
            try:
                t_px_start = perf_counter()
                prices = {s: ex.fetch_price(s) for s in targets.index}
                qtys = compute_qtys(targets, prices, equity)
                t_px = perf_counter() - t_px_start
                log.info(f"Prices/qtys computed for {len(prices)} symbols in {t_px:.2f}s")

                try:
                    tdf = targets.to_frame(name="w").assign(px=pd.Series(prices)).assign(qty=pd.Series(qtys))
                    tdf["absw"] = tdf["w"].abs()
                    top = tdf.sort_values("absw", ascending=False).head(5)[["w", "px", "qty"]]
                    log.info("Top targets (w, px, qty):\n" + top.to_string())
                except Exception as e:
                    log.debug(f"Target preview failed: {e}")

                try:
                    gross_notional = 0.0
                    for s, q in qtys.items():
                        px = prices.get(s)
                        if px and q:
                            gross_notional += abs(float(q)) * float(px)
                    if equity and equity > 0:
                        eff_lev = gross_notional / equity
                        log.info(
                            f"Intended gross notional: {gross_notional:.2f} USDT | "
                            f"effective_leverage≈{eff_lev:.3f}"
                        )
                    else:
                        log.info("Intended gross notional computed but equity <= 0; effective leverage N/A")
                except Exception as e:
                    log.debug(f"Effective leverage calc failed: {e}")
            except Exception as e:
                log.error(f"Price/qty computation failed: {e}")
                log.info(f"=== Cycle end (pricing error) {utcnow().isoformat()} ===")
                continue

            # Current positions snapshot
            current_qtys = {}
            try:
                pos_map = ex.fetch_positions_map()
            except Exception as e:
                log.error(f"Positions fetch failed: {e}")
                pos_map = {}

            for s in targets.index:
                cur = 0.0
                try:
                    p = pos_map.get(s)
                    if p:
                        qty = float(p.get("contracts") or p.get("positionAmt") or p.get("contractsSize") or 0.0)
                        side = p.get("side")
                        if side == "short" and qty > 0:
                            qty = -qty
                        cur = qty
                except Exception:
                    cur = 0.0
                current_qtys[s] = cur

            # Per-symbol leverage setting (best-effort)
            try:
                lev = cfg.execution.set_leverage
                if lev and lev > 0:
                    for s in targets.index:
                        try:
                            ex.try_set_leverage(s, lev)
                        except Exception as ie:
                            log.debug(f"Leverage set failed for {s}: {ie}")
            except Exception as e:
                log.debug(f"Leverage loop failed (ignored): {e}")

            # Orders for rebalance (respect PnL gate)
            t_ord_start = perf_counter()
            orders = []
            try:
                for s in targets.index:
                    tgt = float(qtys.get(s, 0.0))
                    cur = float(current_qtys.get(s, 0.0))
                    diff = tgt - cur
                    if abs(diff) < 1e-8:
                        continue
                    side = "buy" if diff > 0 else "sell"
                    px = prices.get(s)
                    order_px = None
                    if isinstance(px, (int, float)) and cfg.execution.order_type.lower() == "limit":
                        sign = 1 if side == "buy" else -1
                        order_px = px * (1.0 + sign * (cfg.execution.price_offset_bps / 10_000))

                    # PnL gate for reductions/flips (unchanged semantics)
                    try:
                        pnl_gate = float(getattr(cfg.risk, "min_close_pnl_pct", 0.0))
                        if pnl_gate > 0 and cur != 0.0:
                            reducing = (abs(tgt) < abs(cur))
                            flipping = (np.sign(tgt) != np.sign(cur)) and (tgt != 0.0)
                            if reducing or flipping:
                                pinfo = pos_map.get(s, {}) or {}
                                entry = pinfo.get("entryPrice") or pinfo.get("averagePrice") or pinfo.get("avgPrice")
                                px_now = prices.get(s)
                                if entry and entry not in (0, "0", "0.0") and px_now:
                                    entry = float(entry)
                                    px_now = float(px_now)
                                    sign_pos = 1.0 if cur > 0 else -1.0
                                    pnl_pct = (px_now - entry) / entry * sign_pos * 100.0
                                    if abs(pnl_pct) < pnl_gate:
                                        continue
                    except Exception as e:
                        log.debug(f"PnL gate check failed for {s}: {e}")

                    raw_diff = diff
                    q_diff, q_px = ex.quantize(s, diff, order_px)
                    if abs(raw_diff) > 0 and q_diff == 0.0:
                        log.info(f"Rounded to 0 by quantize: {s} raw_diff={raw_diff} order_px={order_px}")
                    if q_diff != 0.0:
                        orders.append((s, side, q_diff, q_px))
            except Exception as e:
                log.error(f"Order diff construction failed: {e}")

            for (s, side, q, px) in orders:
                try:
                    px_eff = px if px is not None else prices.get(s)
                    notional = (abs(float(q)) * float(px_eff)) if (px_eff and q) else 0.0
                    if dry:
                        log.info(f"[DRY] {s}: {side} {q} @ {px or 'mkt'} (~{notional:.2f} USDT)")
                    else:
                        ex.create_order_safe(s, side, q, px, post_only=cfg.execution.post_only, reduce_only=False)
                        log.info(f"[LIVE] {s}: {side} {q} @ {px or 'mkt'} (~{notional:.2f} USDT)")
                except Exception as e:
                    log.error(f"Order error {s} {side} {q}: {e}")
            t_ord = perf_counter() - t_ord_start
            log.info(f"Orders processed (n={len(orders)}) in {t_ord:.2f}s")

            # ======== Enhanced SL/TP / trailing / partials (fast TF) ========
            t_stop_start = perf_counter()
            try:
                stop_tf = getattr(cfg.risk, "stop_timeframe", cfg.exchange.timeframe) or cfg.exchange.timeframe
                atr_len = int(getattr(cfg.risk, "atr_len", 14))
                for s in targets.index:
                    try:
                        cur = float(current_qtys.get(s, 0.0))
                        if cur == 0.0:
                            # Clear any stale perpos state
                            if state["perpos"].get(s):
                                del state["perpos"][s]
                            continue

                        # Fetch faster timeframe for reactive stops
                        raw = ex.fetch_ohlcv(s, timeframe=stop_tf, limit=max(60, atr_len * 5))
                        if not raw:
                            continue
                        df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
                        df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
                        df.set_index("dt", inplace=True)
                        if len(df) < max(20, atr_len + 5):
                            continue

                        # Latest bar and ATR
                        lr = df.iloc[-1]
                        atr = compute_atr(df, n=atr_len, method="rma").iloc[-1]

                        # Ensure per-symbol state is initialized/reset on side change
                        sign = 1 if cur > 0 else -1
                        pinfo = state["perpos"].get(s) or {}
                        pmap = (ex.fetch_positions_map() or {}).get(s, {})  # cheap reuse; ok if empty
                        entry_px = None
                        try:
                            entry_px = pmap.get("entryPrice") or pmap.get("averagePrice") or pmap.get("avgPrice")
                            entry_px = float(entry_px) if entry_px else None
                        except Exception:
                            entry_px = None
                        if entry_px is None:
                            # fallback if exchange didn't give us entry
                            entry_px = float(lr["close"])

                        if not pinfo or pinfo.get("sign") != sign:
                            # New position or flipped side: reset anchors
                            pinfo = {
                                "sign": sign,
                                "entry_price": float(entry_px),
                                "entry_atr": float(atr),
                                "trail_hh": float(lr["high"]),
                                "trail_ll": float(lr["low"]),
                                "partial_done": False,
                            }
                            state["perpos"][s] = pinfo
                        else:
                            # Update trail anchors
                            if sign > 0:
                                pinfo["trail_hh"] = float(max(pinfo.get("trail_hh", lr["high"]), float(lr["high"])))
                            else:
                                pinfo["trail_ll"] = float(min(pinfo.get("trail_ll", lr["low"]), float(lr["low"])))

                        entry_price = float(pinfo["entry_price"])
                        entry_atr = float(pinfo["entry_atr"])
                        R = entry_atr * float(cfg.risk.atr_mult_sl)

                        # Initial (non-trailing) stop from entry
                        if sign > 0:
                            init_sl = entry_price - float(cfg.risk.atr_mult_sl) * entry_atr
                        else:
                            init_sl = entry_price + float(cfg.risk.atr_mult_sl) * entry_atr

                        # Trailing stop (Chandelier style)
                        trail_enabled = bool(getattr(cfg.risk, "trailing_enabled", True))
                        trail_k = float(getattr(cfg.risk, "trail_atr_mult", cfg.risk.atr_mult_sl))
                        if trail_enabled:
                            if sign > 0:
                                trail_sl = float(pinfo["trail_hh"]) - trail_k * float(atr)
                                stop_price = max(init_sl, trail_sl)
                            else:
                                trail_sl = float(pinfo["trail_ll"]) + trail_k * float(atr)
                                stop_price = min(init_sl, trail_sl)
                        else:
                            stop_price = init_sl

                        # Breakeven bump after +1R
                        be_after = float(getattr(cfg.risk, "breakeven_after_r", 0.0))
                        if be_after > 0.0:
                            if sign > 0:
                                be_trigger = entry_price + be_after * R
                                if float(lr["high"]) >= be_trigger:
                                    stop_price = max(stop_price, entry_price)
                            else:
                                be_trigger = entry_price - be_after * R
                                if float(lr["low"]) <= be_trigger:
                                    stop_price = min(stop_price, entry_price)

                        # Partial TP at +partial_tp_r * R (once)
                        if bool(getattr(cfg.risk, "partial_tp_enabled", True)) and (not pinfo.get("partial_done", False)):
                            ptp_r = float(getattr(cfg.risk, "partial_tp_r", 1.5))
                            ptp_px = entry_price + (ptp_r * R if sign > 0 else -ptp_r * R)
                            hit = (float(lr["high"]) >= ptp_px) if sign > 0 else (float(lr["low"]) <= ptp_px)
                            if hit:
                                size_frac = float(getattr(cfg.risk, "partial_tp_size", 0.5))
                                q = abs(cur) * size_frac
                                q, _ = ex.quantize(s, q, None)
                                if q > 0:
                                    if dry:
                                        log.info(f"[DRY-PARTIAL] {s}: {'sell' if sign>0 else 'buy'} {q} @ mkt (~{q * float(lr['close']):.2f} USDT)")
                                    else:
                                        ex.create_order_safe(s, "sell" if sign > 0 else "buy", q, None, post_only=False, reduce_only=True)
                                        log.info(f"[LIVE-PARTIAL] {s}: {'sell' if sign>0 else 'buy'} {q} @ mkt (~{q * float(lr['close']):.2f} USDT)")
                                    pinfo["partial_done"] = True
                                    state["perpos"][s] = pinfo  # persist

                        # Stop trigger check (soft)
                        if sign > 0:
                            stop_hit = float(lr["low"]) <= float(stop_price)
                            if stop_hit:
                                q = abs(cur)
                                q, _ = ex.quantize(s, q, None)
                                if q > 0:
                                    if dry:
                                        log.info(f"[DRY-STOP] {s}: sell {q} @ mkt (SL {stop_price:.6g})")
                                    else:
                                        ex.create_order_safe(s, "sell", q, None, post_only=False, reduce_only=True)
                                        log.info(f"[LIVE-STOP] {s}: sell {q} @ mkt (SL {stop_price:.6g})")
                                # clear state after full exit
                                if s in state["perpos"]:
                                    del state["perpos"][s]
                        else:
                            stop_hit = float(lr["high"]) >= float(stop_price)
                            if stop_hit:
                                q = abs(cur)
                                q, _ = ex.quantize(s, q, None)
                                if q > 0:
                                    if dry:
                                        log.info(f"[DRY-STOP] {s}: buy {q} @ mkt (SL {stop_price:.6g})")
                                    else:
                                        ex.create_order_safe(s, "buy", q, None, post_only=False, reduce_only=True)
                                        log.info(f"[LIVE-STOP] {s}: buy {q} @ mkt (SL {stop_price:.6g})")
                                if s in state["perpos"]:
                                    del state["perpos"][s]

                    except Exception as ie:
                        log.error(f"Enhanced stop/TP check failed for {s}: {ie}")
            except Exception as e:
                log.error(f"Stops/TP loop failed: {e}")
            t_stop = perf_counter() - t_stop_start
            log.info(f"Stops/TP checked for {len(targets)} symbols in {t_stop:.2f}s")

            # Persist state
            try:
                state["last_targets"] = targets.to_dict()
                write_json(state_path, state)
            except Exception as e:
                log.warning(f"State persist failed: {e}")

            cycle_time = (utcnow() - cycle_started_at).total_seconds()
            log.info(f"Cycle complete in {cycle_time:.2f}s")
            log.info(f"=== Cycle end {utcnow().isoformat()} ===")

    finally:
        try:
            ex.close()
        except Exception:
            pass
