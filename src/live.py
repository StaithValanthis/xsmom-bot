import logging
import time
from time import perf_counter
from datetime import datetime, timezone, timedelta
from typing import Dict

import numpy as np
import pandas as pd

from .config import AppConfig
from .exchange import ExchangeWrapper
from .signals import regime_ok
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
    """Sleep in poll_seconds increments until wall-clock minute equals `minute`.
    If minute == "*", run every minute (when now.second < poll_seconds).
    Emits a DEBUG heartbeat so operators can see liveness.
    """
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
    """Convert target weights into target contract qty using latest prices and equity."""
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
    """Compute next UTC datetime when we will rebalance (for UX logs). Supports minute="*"."""
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
    """Pull a fresh filtered symbol list and persist it; log the exact selection at INFO."""
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
        },
    )

    try:
        # Bootstrap universe at startup
        refresh_universe(ex, state, state_path)
        log.info(f"Initial universe: {len(state.get('universe', []))} symbols")

        # Run first cycle immediately; subsequent cycles align to rebalance_minute
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

            cycle_started_at = now
            log.info(f"=== Cycle start {cycle_started_at.isoformat()} ===")

            # Gate: only rebalance every N hours; always manage stops each cycle.
            reb_every = max(1, int(getattr(cfg.execution, "rebalance_every_hours", 1)))
            do_rebalance = ((now.hour % reb_every) == 0)

            # Daily universe refresh (first cycle after UTC midnight)
            try:
                last_ref = state.get("last_universe_refresh")
                if last_ref is None or now.date().isoformat() > last_ref:
                    refresh_universe(ex, state, state_path)
            except Exception as e:
                log.warning(f"Daily universe refresh check failed: {e}")

            # Trading pause gate
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
                log.warning(f"Pause gate check failed (continuing): {e}")

            # Equity & daily anchor (+ configured leverage)
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

            # OHLCV backfill & closes matrix (timed)
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

            # Regime filter (optional)
            try:
                if cfg.strategy.regime_filter.enabled:
                    ok = regime_ok(
                        closes.mean(axis=1),
                        cfg.strategy.regime_filter.ema_len,
                        cfg.strategy.regime_filter.slope_min_bps_per_day,
                    )
                    if not ok:
                        log.info("Regime filter blocking new entries this cycle.")
                        # Even if we block entries, still manage stops below
                        do_rebalance = False
            except Exception as e:
                log.warning(f"Regime filter calc failed (not blocking): {e}")

            # Funding rates (optional tilt)
            funding_map = {}
            try:
                if cfg.strategy.funding_tilt.enabled:
                    funding_map = ex.fetch_funding_rates(list(closes.columns))
            except Exception as e:
                log.debug(f"Funding fetch failed (ignored): {e}")

            # Target construction (timed)
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
                    funding_tilt=funding_map if cfg.strategy.funding_tilt.enabled else None,
                    funding_weight=cfg.strategy.funding_tilt.weight if cfg.strategy.funding_tilt.enabled else 0.0,
                )
                t_targets = perf_counter() - t_targets_start
                log.info(f"Targets built for {len(targets)} symbols in {t_targets:.2f}s")
            except Exception as e:
                log.error(f"Target build failed: {e}")
                log.info(f"=== Cycle end (target error) {utcnow().isoformat()} ===")
                continue

            # Liquidity caps (timed)
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

            # Prices & target quantities (timed) + target preview + effective leverage
            try:
                t_px_start = perf_counter()
                prices = {s: ex.fetch_price(s) for s in targets.index}
                qtys = compute_qtys(targets, prices, equity)
                t_px = perf_counter() - t_px_start
                log.info(f"Prices/qtys computed for {len(prices)} symbols in {t_px:.2f}s")

                # Preview top-5 targets by |weight| with price and qty
                try:
                    tdf = targets.to_frame(name="w").assign(px=pd.Series(prices)).assign(qty=pd.Series(qtys))
                    tdf["absw"] = tdf["w"].abs()
                    top = tdf.sort_values("absw", ascending=False).head(5)[["w", "px", "qty"]]
                    log.info("Top targets (w, px, qty):\n" + top.to_string())
                except Exception as e:
                    log.debug(f"Target preview failed: {e}")

                # Effective portfolio gross leverage from intended targets
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

            # Best-effort leverage (per-symbol on exchange)
            try:
                lev = cfg.execution.set_leverage
                if lev and lev > 0 and do_rebalance:
                    for s in targets.index:
                        try:
                            ex.try_set_leverage(s, lev)
                        except Exception as ie:
                            log.debug(f"Leverage set failed for {s}: {ie}")
            except Exception as e:
                log.debug(f"Leverage loop failed (ignored): {e}")

            # Order diff construction + placement (timed)
            orders = []
            t_ord_start = perf_counter()
            try:
                if not do_rebalance:
                    log.info(f"Skipping rebalance this hour (rebalance_every_hours={reb_every}); managing stops only.")
                else:
                    min_frac = float(getattr(cfg.execution, "min_rebalance_frac", 0.0))
                    min_notional = float(getattr(cfg.execution, "min_order_notional_usdt", 0.0))

                    for s in targets.index:
                        tgt = float(qtys.get(s, 0.0))
                        cur = float(current_qtys.get(s, 0.0))
                        diff = tgt - cur
                        if abs(diff) < 1e-8:
                            continue

                        # Skip micro-changes relative to target (but still allow position close)
                        if abs(tgt) > 0 and abs(diff) < (min_frac * abs(tgt)):
                            continue

                        px = prices.get(s)
                        # Guard against tiny notional orders before quantize
                        if px and min_notional > 0.0 and (abs(diff) * float(px) < min_notional):
                            continue

                        side = "buy" if diff > 0 else "sell"
                        order_px = None
                        if isinstance(px, (int, float)) and cfg.execution.order_type.lower() == "limit":
                            sign = 1 if side == "buy" else -1
                            order_px = px * (1.0 + sign * (cfg.execution.price_offset_bps / 10_000))

                        raw_diff = diff
                        q_diff, q_px = ex.quantize(s, diff, order_px)
                        if abs(raw_diff) > 0 and q_diff == 0.0:
                            log.info(f"Rounded to 0 by quantize: {s} raw_diff={raw_diff} order_px={order_px}")
                        if q_diff != 0.0:
                            orders.append((s, side, q_diff, q_px, px))
            except Exception as e:
                log.error(f"Order diff construction failed: {e}")

            for (s, side, q, px, last_px) in orders:
                try:
                    notional = (abs(q) * (px if px is not None else (last_px or 0.0))) if (px or last_px) else 0.0
                    if dry:
                        log.info(f"[DRY] {s}: {side} {q} @ {px or 'mkt'} (~{notional:.2f} USDT)")
                    else:
                        ex.create_order_safe(s, side, q, px, post_only=cfg.execution.post_only, reduce_only=False)
                        log.info(f"[LIVE] {s}: {side} {q} @ {px or 'mkt'} (~{notional:.2f} USDT)")
                except Exception as e:
                    log.error(f"Order error {s} {side} {q}: {e}")
            t_ord = perf_counter() - t_ord_start
            log.info(f"Orders processed (n={len(orders)}) in {t_ord:.2f}s")

            # Soft stops / TP per symbol (timed)
            t_stop_start = perf_counter()
            try:
                # Manage stops for all symbols we have targets for (cheap) – could be broadened to universe.
                for s in targets.index:
                    try:
                        raw = ex.fetch_ohlcv(s, timeframe=cfg.exchange.timeframe, limit=60)
                        if not raw:
                            continue
                        df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
                        df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
                        df.set_index("dt", inplace=True)
                        if len(df) < 20:
                            continue

                        sl_long, sl_short, tp_long, tp_short = per_symbol_stops(
                            df, cfg.risk.atr_mult_sl, cfg.risk.atr_mult_tp, cfg.risk.use_tp
                        )
                        lr = df.iloc[-1]

                        cur = float(current_qtys.get(s, 0.0))
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
                            elif cfg.risk.use_tp and tp_long is not None and check_soft_stop(lr, "short", tp_long):
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
                            elif cfg.risk.use_tp and tp_short is not None and check_soft_stop(lr, "long", tp_short):
                                q = abs(cur)
                                q, _ = ex.quantize(s, q, None)
                                if q > 0:
                                    if dry:
                                        log.info(f"[DRY-TP] {s}: buy {q} @ mkt (short TP)")
                                    else:
                                        ex.create_order_safe(s, "buy", q, None, post_only=False, reduce_only=True)
                    except Exception as ie:
                        log.error(f"Stop/TP check failed for {s}: {ie}")
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

            # Cycle summary
            cycle_time = (utcnow() - cycle_started_at).total_seconds()
            log.info(f"Cycle complete in {cycle_time:.2f}s")
            log.info(f"=== Cycle end {utcnow().isoformat()} ===")

    finally:
        try:
            ex.close()
        except Exception:
            pass
