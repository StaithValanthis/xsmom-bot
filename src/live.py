# v1.4.2 – 2025-08-22 (startup cancel-all hook behind a flag; CCXT-only backend)
from __future__ import annotations
import logging
import threading
import time
from time import perf_counter
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from .config import AppConfig
from .exchange import ExchangeWrapper
from .signals import compute_atr, regime_ok, dynamic_k
from .sizing import build_targets, apply_liquidity_caps
from .risk import per_symbol_stops, kill_switch_should_trigger, resume_time_after_kill
from .utils import utcnow, read_json, write_json

log = logging.getLogger("live")

# -------------------- FAST EVENT-DRIVEN SL/TP THREAD --------------------

class FastSLTPThread(threading.Thread):
    def __init__(self, ex: ExchangeWrapper, cfg: AppConfig, state: dict, dry: bool, stop_event: threading.Event):
        super().__init__(daemon=True)
        self.ex = ex
        self.cfg = cfg
        self.state = state
        self.dry = dry
        self.stop_event = stop_event
        self._last_ohlcv_ts: Dict[str, float] = {}
        self._last_bar_cache: Dict[str, pd.Series] = {}

        self.state.setdefault("perpos", {})
        self.state.setdefault("cooldowns", {})
        self.state.setdefault("enter_bar_time", {})

    def _now_ts(self) -> float:
        return float(time.time())

    @staticmethod
    def _to_float(x, default: float = None) -> Optional[float]:
        try:
            if isinstance(x, (pd.Series, pd.Index, np.ndarray)) and hasattr(x, "item"):
                return float(np.asarray(x).astype("float64").ravel()[-1])
            return float(x)
        except Exception:
            return default

    def _init_or_update_perpos(self, symbol: str, cur_qty: float, lr: pd.Series, atr_val: float, entry_price: Optional[float]):
        sign = 1 if float(cur_qty) > 0 else -1
        perpos = self.state.setdefault("perpos", {})
        pinfo = perpos.get(symbol)

        close_v = self._to_float(lr.get("close"))
        high_v = self._to_float(lr.get("high"))
        low_v  = self._to_float(lr.get("low"))
        atr_v  = self._to_float(atr_val)
        entry_v = self._to_float(entry_price, close_v)

        if (not isinstance(pinfo, dict)) or (int(pinfo.get("sign", 0) or 0) != sign):
            perpos[symbol] = {
                "sign": sign,
                "entry_price": float(entry_v),
                "entry_atr": float(atr_v),
                "trail_hh": float(high_v),
                "trail_ll": float(low_v),
                "partial_done": False,
            }
            self.state.setdefault("enter_bar_time", {})[symbol] = pd.Timestamp.utcnow().isoformat()
        else:
            if sign > 0:
                pinfo["trail_hh"] = float(max(self._to_float(pinfo.get("trail_hh"), high_v), high_v))
            else:
                pinfo["trail_ll"] = float(min(self._to_float(pinfo.get("trail_ll"), low_v), low_v))

    def _maybe_partial_tp(self, symbol: str, qty: float, rr_now: float) -> None:
        if not self.cfg.risk.partial_tp_enabled:
            return
        perpos = self.state.get("perpos", {})
        pinfo = perpos.get(symbol) or {}
        if pinfo.get("partial_done"):
            return
        if rr_now >= float(getattr(self.cfg.risk, "partial_tp_r", 2.0)):
            take = float(getattr(self.cfg.risk, "partial_tp_size", 0.5))
            side = "sell" if qty > 0 else "buy"
            q = abs(qty) * take
            try:
                if self.dry:
                    log.info(f"[DRY] Partial TP {symbol} {side} {q}")
                else:
                    self.ex.create_order_safe(symbol, side, q, None, post_only=False, reduce_only=True)
                pinfo["partial_done"] = True
            except Exception as e:
                log.warning(f"Partial TP error {symbol}: {e}")

    def run(self):
        if self.cfg.risk.fast_check_seconds <= 0:
            return
        tf = getattr(self.cfg.risk, "stop_timeframe", "5m")

        while not self.stop_event.is_set():
            try:
                time.sleep(max(1, int(self.cfg.risk.fast_check_seconds)))
                pos = self.ex.fetch_positions()
                if not pos:
                    continue

                for sym, pdct in pos.items():
                    qty = float(pdct.get("net_qty") or 0.0)
                    if qty == 0.0:
                        continue
                    side = "long" if qty > 0 else "short"

                    nowts = float(time.time())
                    if nowts - float(self._last_ohlcv_ts.get(sym, 0.0)) > 55.0:
                        try:
                            bars = self.ex.fetch_ohlcv(sym, tf, limit=max(50, self.cfg.risk.atr_len + 5))
                            df = pd.DataFrame(bars, columns=["ts","open","high","low","close","volume"])
                            df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
                            df.set_index("dt", inplace=True)
                            self._last_bar_cache[sym] = df.iloc[-1]
                            _ = compute_atr(df, n=self.cfg.risk.atr_len, method="rma").iloc[-1]
                            self._last_ohlcv_ts[sym] = nowts
                        except Exception as e:
                            log.debug(f"fast fetch_ohlcv {sym} failed: {e}")
                            continue
                    lr = self._last_bar_cache.get(sym)
                    if lr is None:
                        continue

                    entry_price = float(pdct.get("entryPrice") or 0.0) or float(lr["close"])
                    atr_val = compute_atr(
                        pd.DataFrame([lr]).rename_axis("dt"),
                        n=self.cfg.risk.atr_len,
                        method="rma",
                    ).iloc[-1]
                    self._init_or_update_perpos(sym, qty, lr, atr_val, entry_price)

                    pinfo = self.state["perpos"].get(sym, {})
                    ep = float(pinfo.get("entry_price") or entry_price)
                    atr = float(pinfo.get("entry_atr") or atr_val)
                    last = float(lr["close"])
                    R = abs(last - ep) / (atr if atr > 1e-12 else 1.0)

                    if self.cfg.risk.breakeven_after_r > 0 and R >= self.cfg.risk.breakeven_after_r:
                        pass

                    self._maybe_partial_tp(sym, qty, R)

                    max_hours = int(getattr(self.cfg.risk, "max_hours_in_trade", 0) or 0)
                    if max_hours > 0:
                        enter_iso = self.state.get("enter_bar_time", {}).get(sym)
                        if enter_iso:
                            age_h = (pd.Timestamp.utcnow() - pd.Timestamp(enter_iso)).total_seconds() / 3600.0
                            if age_h >= max_hours:
                                side_close = "sell" if qty > 0 else "buy"
                                try:
                                    if self.dry:
                                        log.info(f"[DRY] Time exit {sym} {side_close} {abs(qty)}")
                                    else:
                                        self.ex.create_order_safe(sym, side_close, abs(qty), None, post_only=False, reduce_only=True)
                                    self.state["perpos"].pop(sym, None)
                                    self.state["enter_bar_time"].pop(sym, None)
                                    continue
                                except Exception as e:
                                    log.warning(f"Time exit error {sym}: {e}")

                    trail_mult = float(getattr(self.cfg.risk, "trail_atr_mult", 0.0) or 0.0)
                    if self.cfg.risk.trailing_enabled and trail_mult > 0 and atr > 0:
                        if side == "long":
                            trail = float(max(pinfo.get("trail_hh", last), last)) - trail_mult * atr
                            trigger = (last <= trail) if not self.cfg.risk.stop_on_close_only else (lr["close"] <= trail)
                        else:
                            trail = float(min(pinfo.get("trail_ll", last), last)) + trail_mult * atr
                            trigger = (last >= trail) if not self.cfg.risk.stop_on_close_only else (lr["close"] >= trail)

                        if trigger:
                            side_close = "sell" if qty > 0 else "buy"
                            try:
                                if self.dry:
                                    log.info(f"[DRY] Trail stop {sym} {side_close} {abs(qty)} @ ~{last}")
                                else:
                                    self.ex.create_order_safe(sym, side_close, abs(qty), None, post_only=False, reduce_only=True)
                                self.state["perpos"].pop(sym, None)
                                self.state["enter_bar_time"].pop(sym, None)
                            except Exception as e:
                                log.warning(f"Trail stop error {sym}: {e}")

            except Exception as e:
                log.debug(f"Fast thread loop error: {e}")

# -------------------- STARTUP RECONCILE --------------------

def _reconcile_positions_on_start(ex: ExchangeWrapper, cfg: AppConfig, state: dict) -> None:
    try:
        positions = ex.fetch_positions() or {}
    except Exception as e:
        log.error(f"Startup reconcile: fetch_positions failed: {e}")
        return

    tf = getattr(cfg.risk, "stop_timeframe", "5m")
    atr_len = int(getattr(cfg.risk, "atr_len", 28))

    live_syms = []
    for sym, pdct in positions.items():
        net_qty = float(pdct.get("net_qty") or 0.0)
        if abs(net_qty) <= 0.0:
            continue
        try:
            bars = ex.fetch_ohlcv(sym, tf, limit=max(50, atr_len + 5))
            if not bars:
                continue
            df = pd.DataFrame(bars, columns=["ts","open","high","low","close","volume"])
            df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
            df.set_index("dt", inplace=True)
            lr = df.iloc[-1]
            atr_val = compute_atr(df, n=atr_len, method="rma").iloc[-1]
            sign = 1 if net_qty > 0 else -1
            entry_price = float(pdct.get("entryPrice") or lr["close"])
            state.setdefault("perpos", {})[sym] = {
                "sign": sign,
                "entry_price": float(entry_price),
                "entry_atr": float(atr_val),
                "trail_hh": float(lr["high"]),
                "trail_ll": float(lr["low"]),
                "partial_done": bool(state.get("perpos", {}).get(sym, {}).get("partial_done", False)),
            }
            state.setdefault("enter_bar_time", {})[sym] = state.get("enter_bar_time", {}).get(sym) or pd.Timestamp.utcnow().isoformat()
            live_syms.append(sym)
        except Exception as e:
            log.warning(f"Startup reconcile: OHLCV/ATR failed for {sym}: {e}")

    # prune stale
    stale = []
    for sym in list(state.get("perpos", {}).keys()):
        if sym not in positions or float(positions.get(sym, {}).get("net_qty") or 0.0) == 0.0:
            stale.append(sym)
    for sym in stale:
        state["perpos"].pop(sym, None)
        state.get("enter_bar_time", {}).pop(sym, None)

    write_json(getattr(cfg.paths, "state_path", "state/state.json"), state)
    if live_syms:
        log.info(f"Startup reconcile: attached to {len(live_syms)} live positions: {', '.join(sorted(live_syms))}")
    else:
        log.info("Startup reconcile: no live positions found on exchange.")

# -------------------- MAIN LOOP --------------------

def _minute_aligned(minute: int) -> bool:
    if minute <= 0:
        return True
    now = utcnow()
    return (now.minute % minute) == 0

def _dd_pct(ref: float, noweq: float) -> float:
    if ref is None or ref <= 0 or noweq is None or noweq <= 0:
        return 0.0
    return 100.0 * max(0.0, (ref - noweq) / ref)

def _is_new_position(pos: dict) -> bool:
    return abs(float(pos.get("net_qty") or 0.0)) < 1e-9

def run_live(cfg: AppConfig, dry: bool = False):
    log.info(f"Fast SL/TP loop starting: check every {cfg.risk.fast_check_seconds}s on timeframe={cfg.risk.stop_timeframe}")

    state_path = cfg.paths.state_path
    state = read_json(state_path, default={}) or {}
    state.setdefault("perpos", {})
    state.setdefault("cooldowns", {})
    state.setdefault("enter_bar_time", {})
    state.setdefault("last_trade_ts", {})
    state.setdefault("day_date", None)
    state.setdefault("day_high_equity", 0.0)

    day_start_equity = float(state.get("day_start_equity", 0.0))
    day_high_equity = float(state.get("day_high_equity", 0.0))
    disable_until_ts = float(state.get("disable_until_ts", 0.0))
    soft_block_until_ts = float(state.get("soft_block_until_ts", 0.0))

    ex = ExchangeWrapper(cfg.exchange)

    # --- ONE-LINER STARTUP CANCEL (behind flag) ---
    if getattr(cfg.execution, "cancel_open_orders_on_start", False):
        try:
            ex.cancel_all_orders(None)
            log.info("Startup safety: cancel_all_orders executed (per config flag).")
        except Exception as e:
            log.warning(f"Startup safety: cancel_all_orders failed (continuing): {e}")

    # Reconcile with live positions
    try:
        _reconcile_positions_on_start(ex, cfg, state)
    except Exception as e:
        log.warning(f"Startup reconcile failed (non-fatal): {e}")

    stop_evt = threading.Event()
    fast_thread = FastSLTPThread(ex, cfg, state, dry, stop_evt)
    fast_thread.start()

    try:
        syms = ex.fetch_markets_filtered()
        if not syms:
            log.error("Empty universe; exiting.")
            return

        for s in syms:
            ex.set_leverage(s, int(getattr(cfg.execution, "set_leverage", 1)))

        eq_now = ex.get_equity_usdt()
        cur_day = utcnow().date().isoformat()

        if day_start_equity <= 0 and eq_now > 0:
            day_start_equity = eq_now
            state["day_start_equity"] = day_start_equity

        if day_high_equity <= 0 and eq_now > 0:
            day_high_equity = eq_now
            state["day_high_equity"] = day_high_equity

        if not state.get("day_date"):
            state["day_date"] = cur_day

        write_json(state_path, state)

        did_first_cycle = False
        last_pause_log = 0.0
        last_align_log = 0.0

        while True:
            if disable_until_ts and time.time() < disable_until_ts:
                if time.time() - last_pause_log > 30:
                    resume_dt = pd.to_datetime(disable_until_ts, unit="s", utc=True)
                    log.warning(f"PAUSED by kill-switch until {resume_dt.isoformat()}")
                    last_pause_log = time.time()
                time.sleep(2)
                continue
            elif disable_until_ts and time.time() >= disable_until_ts:
                disable_until_ts = 0.0
                state["disable_until_ts"] = 0.0
                write_json(state_path, state)
                log.info("Kill-switch pause expired; trading re-enabled.")

            if did_first_cycle and not _minute_aligned(getattr(cfg.execution, "rebalance_minute", 1)):
                if time.time() - last_align_log > 15:
                    log.debug("Waiting for minute alignment...")
                    last_align_log = time.time()
                time.sleep(1.0)
                continue

            cycle_started_at = utcnow()
            log.info(f"=== Cycle start {cycle_started_at.isoformat()} ===")
            did_first_cycle = True

            # Day rollover (UTC)
            eq = ex.get_equity_usdt()
            cur_day = utcnow().date().isoformat()
            if state.get("day_date") != cur_day and eq > 0:
                state["day_date"] = cur_day
                state["day_start_equity"] = eq
                state["day_high_equity"] = eq
                day_start_equity = eq
                day_high_equity = eq
                write_json(state_path, state)
                log.info(f"New UTC day: reset day_start_equity and day_high_equity to {eq:.2f}")

            # Update intraday equity high
            if eq > 0 and eq > day_high_equity:
                day_high_equity = eq
                state["day_high_equity"] = day_high_equity
                write_json(state_path, state)

            # Kill switch (supports trailing from config)
            use_trailing = bool(getattr(cfg.risk, "use_trailing_killswitch", True))
            if eq > 0 and (day_start_equity > 0 or day_high_equity > 0):
                if kill_switch_should_trigger(day_start_equity, day_high_equity, eq, cfg.risk.max_daily_loss_pct, use_trailing=use_trailing):
                    ref = day_high_equity if use_trailing else day_start_equity
                    dd = _dd_pct(ref, eq)
                    resume = resume_time_after_kill(utcnow(), cfg.risk.trade_disable_minutes)
                    state["disable_until_ts"] = resume.timestamp()
                    write_json(state_path, state)
                    lbl = "from intraday HIGH" if use_trailing else "from day START"
                    log.error(f"KILL SWITCH: dd={dd:.2f}% {lbl}; pausing trading until {resume.isoformat()}")
                    time.sleep(5)
                    continue

            # SOFT kill (optional; blocks new entries only)
            allow_new_entries = True
            if getattr(cfg.strategy, "soft_kill", None) and cfg.strategy.soft_kill.enabled and eq > 0 and day_start_equity > 0:
                dd_start = _dd_pct(day_start_equity, eq)
                if time.time() < soft_block_until_ts:
                    allow_new_entries = False
                    if time.time() - last_pause_log > 30:
                        resume_dt = pd.to_datetime(soft_block_until_ts, unit="s", utc=True)
                        log.warning(f"SOFT BLOCK active; new entries disabled until {resume_dt.isoformat()}")
                        last_pause_log = time.time()
                elif dd_start >= cfg.strategy.soft_kill.soft_daily_loss_pct:
                    allow_new_entries = False
                    soft_resume = resume_time_after_kill(utcnow(), cfg.strategy.soft_kill.resume_after_minutes)
                    state["soft_block_until_ts"] = soft_resume.timestamp()
                    soft_block_until_ts = state["soft_block_until_ts"]
                    write_json(state_path, state)
                    log.warning(f"SOFT KILL: dd_from_start={dd_start:.2f}% ; blocking new entries until {soft_resume.isoformat()}")

            # 1) OHLCV
            bars: Dict[str, pd.DataFrame] = {}
            syms = ex.fetch_markets_filtered()
            for s in syms:
                try:
                    raw = ex.fetch_ohlcv(s, cfg.exchange.timeframe, limit=cfg.exchange.candles_limit)
                    df = pd.DataFrame(raw, columns=["ts","open","high","low","close","volume"])
                    df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
                    df.set_index("dt", inplace=True)
                    if len(df) > 0:
                        bars[s] = df
                except Exception as e:
                    log.warning(f"OHLCV {s} failed: {e}")

            if not bars:
                log.error("No bars fetched this cycle; sleeping.")
                time.sleep(max(1, int(getattr(cfg.execution, "poll_seconds", 5))))
                continue

            closes = pd.concat({s: bars[s]["close"] for s in bars}, axis=1).dropna(how="all")
            mean_close = closes.mean(axis=1)

            # 2) Regime
            if cfg.strategy.regime_filter.enabled:
                ema_len = cfg.strategy.regime_filter.ema_len
                thr = cfg.strategy.regime_filter.slope_min_bps_per_day
                use_abs = bool(getattr(cfg.strategy.regime_filter, "use_abs", False))
                try:
                    ema_dbg = mean_close.ewm(span=ema_len, adjust=False).mean()
                    slope_dbg = ema_dbg.diff().tail(ema_len).mean()
                    last_close_dbg = float(mean_close.iloc[-1]) if len(mean_close) else float("nan")
                    slope_bps_dbg = 10_000 * float(slope_dbg) / last_close_dbg if last_close_dbg > 0 else float("nan")
                    log.debug(f"Regime gate diag: ema_len={ema_len}, slope_min_bps={thr}, use_abs={use_abs}, slope_bps_now={slope_bps_dbg:.3f}")
                except Exception:
                    pass

                ok = regime_ok(mean_close, ema_len, thr, use_abs=use_abs)
                if not ok:
                    log.info("Regime gate blocked this cycle; staying flat.")
                    time.sleep(max(1, int(getattr(cfg.execution, "poll_seconds", 5))))
                    continue

            # 3) Targets
            funding_map: Dict[str, float] = {}
            if getattr(cfg.strategy.funding_tilt, "enabled", False):
                try:
                    funding_map = ex.fetch_funding_rates(list(bars.keys())) or {}
                except Exception:
                    pass

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
                dynamic_k_fn=dynamic_k,
                funding_tilt=funding_map if getattr(cfg.strategy.funding_tilt, "enabled", False) else None,
                funding_weight=float(getattr(cfg.strategy.funding_tilt, "weight", 0.0)) if getattr(cfg.strategy.funding_tilt, "enabled", False) else 0.0,
                entry_zscore_min=float(getattr(cfg.strategy, "entry_zscore_min", 0.0)),
                diversify_enabled=bool(getattr(cfg.strategy.diversify, "enabled", False)),
                corr_lookback=int(getattr(cfg.strategy.diversify, "corr_lookback", 48)),
                max_pair_corr=float(getattr(cfg.strategy.diversify, "max_pair_corr", 0.9)),
                vol_target_enabled=bool(getattr(cfg.strategy.vol_target, "enabled", False)),
                target_daily_vol_bps=float(getattr(cfg.strategy.vol_target, "target_daily_vol_bps", 0.0)),
                vol_target_min_scale=float(getattr(cfg.strategy.vol_target, "min_scale", 0.5)),
                vol_target_max_scale=float(getattr(cfg.strategy.vol_target, "max_scale", 2.0)),
            )

            # 4) Liquidity caps
            tickers = ex.fetch_tickers(list(targets.index))
            eq = ex.get_equity_usdt()
            targets = apply_liquidity_caps(
                targets,
                equity_usdt=eq,
                tickers=tickers or {},
                adv_cap_pct=cfg.liquidity.adv_cap_pct,
                notional_cap_usdt=cfg.liquidity.notional_cap_usdt,
            )

            # 5) Orders
            positions = ex.fetch_positions()
            min_notional = float(getattr(cfg.execution, "min_notional_per_order_usdt", 5.0))
            min_delta_bps = float(getattr(cfg.execution, "min_rebalance_delta_bps", 1.0))

            open_symbols = [s for s, p in positions.items() if not _is_new_position(p)]
            remaining_slots = max(0, getattr(cfg.strategy.entry_throttle, "max_open_positions", 999) - len(open_symbols))
            new_entries_cap = min(getattr(cfg.strategy.entry_throttle, "max_new_positions_per_cycle", 999), remaining_slots)

            now_ts = time.time
            last_ts_map: Dict[str, float] = state.get("last_trade_ts", {})

            candidates: List[Tuple[str, float]] = []
            for s, w in targets.items():
                pos = positions.get(s, {}) or {}
                if _is_new_position(pos) and w != 0.0:
                    last_t = float(last_ts_map.get(s, 0.0))
                    if (now_ts() - last_t) < 60.0 * getattr(cfg.strategy.entry_throttle, "per_symbol_trade_cooldown_min", 0):
                        continue
                    min_z = float(getattr(cfg.strategy.entry_throttle, "min_entry_zscore", 0.0))
                    if abs(float(w)) < (min_z * 0.01):
                        continue
                    candidates.append((s, abs(float(w))))
            candidates.sort(key=lambda x: x[1], reverse=True)
            allow_syms_new = set([s for s, _ in candidates[: new_entries_cap]])

            orders: List[Dict] = []
            for s, w in targets.items():
                t = (tickers.get(s, {}) or {})
                px = t.get("last") or t.get("close")
                if px is None:
                    continue
                px = float(px)
                target_notional = w * eq

                pos = positions.get(s, {}) or {}
                long_qty = float(pos.get("long_qty") or 0.0)
                short_qty = float(pos.get("short_qty") or 0.0)
                net_qty = float(pos.get("net_qty") or 0.0)
                cur_notional = net_qty * px
                delta_notional = target_notional - cur_notional

                if abs(delta_notional) < min_notional:
                    continue
                if eq > 0 and (abs(delta_notional) / eq * 10_000.0) < min_delta_bps:
                    continue

                want_buy = delta_notional > 0
                want_sell = delta_notional < 0

                is_new = _is_new_position(pos)
                if is_new:
                    if not allow_new_entries:
                        continue
                    if s not in allow_syms_new:
                        continue

                if want_buy and short_qty > 0:
                    qty_to_close = min(abs(delta_notional) / px, short_qty)
                    if qty_to_close > 0:
                        orders.append({"symbol": s, "side": "buy", "qty": qty_to_close, "price": None, "reduceOnly": True})
                    delta_notional -= min(abs(delta_notional), qty_to_close * px)

                if want_sell and long_qty > 0:
                    qty_to_close = min(abs(delta_notional) / px, long_qty)
                    if qty_to_close > 0:
                        orders.append({"symbol": s, "side": "sell", "qty": qty_to_close, "price": None, "reduceOnly": True})
                    delta_notional += min(abs(delta_notional), qty_to_close * px)

                if abs(delta_notional) >= min_notional:
                    side = "buy" if delta_notional > 0 else "sell"
                    size = abs(delta_notional) / px
                    price = None
                    if getattr(cfg.execution, "order_type", "market").lower() == "limit":
                        off_bps = getattr(cfg.execution, "price_offset_bps", 0)
                        off = (off_bps / 10_000.0) * px
                        price = (px - off) if side == "buy" else (px + off)
                    orders.append({"symbol": s, "side": side, "qty": size, "price": price, "reduceOnly": False})

            t_ord_start = perf_counter()
            for od in orders:
                s = od["symbol"]; side = od["side"]; q = float(od["qty"]); px = od["price"]; ro = bool(od.get("reduceOnly", False))
                notional = q * (tickers.get(s, {}) or {}).get("last", 0.0)
                try:
                    if dry:
                        tag = "REDUCE" if ro else "OPEN"
                        log.info(f"[DRY] {tag} {s}: {side} {q} @ {px or 'mkt'} (~{notional:.2f} USDT)")
                    else:
                        ex.create_order_safe(s, side, q, px, post_only=(getattr(cfg.execution, "post_only", False) and not ro), reduce_only=ro)
                        tag = "REDUCE" if ro else "OPEN"
                        log.info(f"[LIVE] {tag} {s}: {side} {q} @ {px or 'mkt'} (~{notional:.2f} USDT)")
                        state.setdefault("last_trade_ts", {})[s] = time.time()
                except Exception as e:
                    log.error(f"Order error {s} {side} {q}: {e}")
            t_ord = perf_counter() - t_ord_start
            log.info(f"Orders processed (n={len(orders)}) in {t_ord:.2f}s")

            try:
                state["last_targets"] = {k: float(v) for k, v in targets.items()}
                write_json(state_path, state)
            except Exception as e:
                log.warning(f"State persist failed: {e}")

            cycle_time = (utcnow() - cycle_started_at).total_seconds()
            log.info(f"Cycle complete in {cycle_time:.2f}s")
            log.info(f"=== Cycle end {utcnow().isoformat()} ===")

            time.sleep(max(1, int(getattr(cfg.execution, "poll_seconds", 5))))

    finally:
        try:
            stop_evt.set()
        except Exception:
            pass
        try:
            if fast_thread and fast_thread.is_alive():
                fast_thread.join(timeout=2.0)
        except Exception:
            pass
        try:
            ex.close()
        except Exception:
            pass
