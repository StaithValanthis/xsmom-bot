# v1.6.1 – 2025-08-24
# - Configurable profit-lock ladder (+R) and breakeven fee buffer
# - Tighten trailing after partial TP; age-based tightening
# - Green-day soft win lock (blocks new entries after daily gain target)
# - Close-only stop logic with optional confirmation bars
# - Hold timer + catastrophic ATR stop
# - Cooldown on stop; apply cooldowns to target weights
# - Intent-based limit-order reconciliation & stale cleanup (feature-gated via execution.stale_orders)
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
        self._last_closed_bar: Dict[str, pd.DataFrame] = {}  # store N recent CLOSED bars for confirm logic
        self._atr_cache: Dict[str, float] = {}

        self.state.setdefault("perpos", {})
        self.state.setdefault("cooldowns", {})
        self.state.setdefault("enter_bar_time", {})
        self.state.setdefault("locked_r", {})

        # risk knobs
        self.stop_close_only: bool = bool(getattr(self.cfg.risk, "stop_on_close_only", True))
        self.stop_confirm_bars: int = int(getattr(self.cfg.risk, "stop_confirm_bars", 0))
        self.min_hold_minutes: int = int(getattr(self.cfg.risk, "min_hold_minutes", 0))
        self.catastrophic_mult: float = float(getattr(self.cfg.risk, "catastrophic_atr_mult", 3.5))
        self.stop_buffer_bps: float = float(getattr(self.cfg.risk, "stop_buffer_bps", 0.0))
        self.trail_mult: float = float(getattr(self.cfg.risk, "trail_atr_mult", 0.0) or 0.0)

    @staticmethod
    def _to_float(x, default: float = None) -> Optional[float]:
        try:
            if isinstance(x, (pd.Series, pd.Index, np.ndarray)) and hasattr(x, "item"):
                return float(np.asarray(x).astype("float64").ravel()[-1])
            return float(x)
        except Exception:
            return default

    def _minutes_held(self, symbol: str) -> float:
        entered_iso = self.state.get("enter_bar_time", {}).get(symbol)
        if not entered_iso:
            return 0.0
        try:
            return (pd.Timestamp.utcnow() - pd.Timestamp(entered_iso)).total_seconds() / 60.0
        except Exception:
            return 0.0

    def _init_or_update_perpos(self, symbol: str, cur_qty: float, last_closed: pd.Series, atr_val: float, entry_price: Optional[float]):
        sign = 1 if float(cur_qty) > 0 else -1
        perpos = self.state.setdefault("perpos", {})
        pinfo = perpos.get(symbol)

        close_v = self._to_float(last_closed.get("close"))
        high_v = self._to_float(last_closed.get("high"))
        low_v  = self._to_float(last_closed.get("low"))
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
            self.state.setdefault("locked_r", {})[symbol] = 0.0
        else:
            # update trail anchors with CLOSED bar extremes
            if sign > 0:
                pinfo["trail_hh"] = float(max(self._to_float(pinfo.get("trail_hh"), high_v), high_v))
            else:
                pinfo["trail_ll"] = float(min(self._to_float(pinfo.get("trail_ll"), low_v), low_v))

    def _compute_stop_px(self, symbol: str, last_closed: pd.Series) -> Tuple[Optional[float], Optional[float], float, float, int]:
        """
        Returns: (normal_stop_px, catastrophic_stop_px, entry_price, R_unit, sign)
        """
        pinfo = self.state.get("perpos", {}).get(symbol) or {}
        sign = int(pinfo.get("sign", 0) or 0)
        entry_price = self._to_float(pinfo.get("entry_price"))
        entry_atr   = self._to_float(pinfo.get("entry_atr"))
        if entry_price is None or entry_atr is None or entry_atr <= 0 or sign == 0:
            return None, None, 0.0, 0.0, 0

        atr_mult_sl = float(getattr(self.cfg.risk, "atr_mult_sl", 2.0))
        trailing_enabled = bool(getattr(self.cfg.risk, "trailing_enabled", True))

        # ----- profit-lock ladder (configurable via risk.profit_lock_steps) -----
        steps_cfg = getattr(self.cfg.risk, "profit_lock_steps", None)
        if not steps_cfg:
            steps = [(1.0, 0.0), (1.8, 0.8), (3.0, 1.8)]
        else:
            steps = [(float(t), float(l)) for (t, l) in steps_cfg]

        # breakeven extras
        be_after = float(getattr(self.cfg.risk, "breakeven_after_r", 0.0))
        be_extra_bps = float(getattr(self.cfg.risk, "breakeven_extra_bps", 0.0) or 0.0)
        be_extra = be_extra_bps / 10_000.0

        # ----- dynamic trail multiplier adjustments -----
        # base
        trail_k = float(getattr(self.cfg.risk, "trail_atr_mult", 0.0) or 0.0)

        # after partial tighten
        if pinfo.get("partial_done"):
            trail_k_after = float(getattr(self.cfg.risk, "trail_after_partial_mult", 0.0) or 0.0)
            if trail_k_after > 1e-12:
                trail_k = trail_k_after

        # age-based tighten
        age_cfg = getattr(self.cfg.risk, "age_tighten", None)
        if age_cfg:
            try:
                hrs = float(age_cfg.get("hours", 0) or 0)
                t_mult = float(age_cfg.get("trail_atr_mult", 0.0) or 0.0)
                if hrs > 0 and t_mult > 0:
                    enter_iso = self.state.get("enter_bar_time", {}).get(symbol)
                    if enter_iso:
                        age_h = (pd.Timestamp.utcnow() - pd.Timestamp(enter_iso)).total_seconds() / 3600.0
                        if age_h >= hrs:
                            trail_k = t_mult
            except Exception:
                pass

        # base stop from entry
        init_sl = entry_price - atr_mult_sl * entry_atr if sign > 0 else entry_price + atr_mult_sl * entry_atr

        cur_atr = self._atr_cache.get(symbol, entry_atr)
        hh = self._to_float(pinfo.get("trail_hh"), self._to_float(last_closed.get("high")))
        ll = self._to_float(pinfo.get("trail_ll"), self._to_float(last_closed.get("low")))

        stop_px = init_sl
        if trailing_enabled and trail_k > 1e-12 and cur_atr > 0:
            trail_sl = (hh - trail_k * cur_atr) if sign > 0 else (ll + trail_k * cur_atr)
            stop_px = max(init_sl, trail_sl) if sign > 0 else min(init_sl, trail_sl)

        # breakeven bump (+bps) only when BE is armed
        last_close = self._to_float(last_closed.get("close"), entry_price)
        R_unit = float(entry_atr * atr_mult_sl)
        if be_after > 0 and last_close is not None:
            if sign > 0 and last_close >= entry_price + be_after * R_unit:
                be_px = entry_price * (1.0 + be_extra)
                stop_px = max(stop_px, be_px)
            elif sign < 0 and last_close <= entry_price - be_after * R_unit:
                be_px = entry_price * (1.0 - be_extra)
                stop_px = min(stop_px, be_px)

        # profit-lock ladder
        locked = float(self.state.get("locked_r", {}).get(symbol, 0.0) or 0.0)
        for trig, lock in steps:
            if locked < lock:
                if sign > 0 and last_close >= entry_price + trig * R_unit:
                    stop_px = max(stop_px, entry_price + lock * R_unit)
                    locked = lock
                elif sign < 0 and last_close <= entry_price - trig * R_unit:
                    stop_px = min(stop_px, entry_price - lock * R_unit)
                    locked = lock
        if locked > 0:
            self.state.setdefault("locked_r", {})[symbol] = locked

        # buffer (bps)
        buf = self.stop_buffer_bps / 10_000.0
        if buf > 0:
            stop_px = stop_px * (1.0 - buf) if sign > 0 else stop_px * (1.0 + buf)

        # catastrophic
        cat_px = entry_price - self.catastrophic_mult * entry_atr if sign > 0 else entry_price + self.catastrophic_mult * entry_atr
        return float(stop_px), float(cat_px), float(entry_price), float(R_unit), sign

    def _maybe_partial_tp(self, symbol: str, qty: float, rr_now: float) -> None:
        if not self.cfg.risk.partial_tp_enabled:
            return
        pinfo = self.state.get("perpos", {}).get(symbol) or {}
        if pinfo.get("partial_done"):
            return
        if rr_now >= float(getattr(self.cfg.risk, "partial_tp_r", 2.0)):
            take = float(getattr(self.cfg.risk, "partial_tp_size", 0.5))
            side = "sell" if qty > 0 else "buy"
            q = abs(qty) * take
            if q <= 0:
                return
            try:
                if self.dry:
                    log.info(f"[DRY] Partial TP {symbol} {side} {q}")
                else:
                    self.ex.create_order_safe(symbol, side, q, None, post_only=False, reduce_only=True)
                pinfo["partial_done"] = True
                self.state.setdefault("perpos", {})[symbol] = pinfo
            except Exception as e:
                log.warning(f"Partial TP error {symbol}: {e}")

    def _place_exit(self, symbol: str, qty: float, reason: str):
        side_close = "sell" if qty > 0 else "buy"
        q = abs(qty)
        if q <= 0:
            return
        try:
            if self.dry:
                log.info(f"[DRY-{reason}] {symbol}: {side_close} {q} @ mkt")
            else:
                self.ex.create_order_safe(symbol, side_close, q, None, post_only=False, reduce_only=True)
                log.info(f"[LIVE-{reason}] {symbol}: {side_close} {q} @ mkt")
        finally:
            # cooldown after stop/exit
            cdm = int(getattr(self.cfg.risk, "cooldown_minutes_after_stop", 0))
            if cdm > 0:
                until = (pd.Timestamp.utcnow() + pd.Timedelta(minutes=cdm)).isoformat()
                self.state.setdefault("cooldowns", {})[symbol] = until
            # clear tracking
            self.state.get("perpos", {}).pop(symbol, None)
            self.state.get("enter_bar_time", {}).pop(symbol, None)
            self.state.get("locked_r", {}).pop(symbol, None)

    def run(self):
        fast_s = int(getattr(self.cfg.risk, "fast_check_seconds", 0))
        if fast_s <= 0:
            return
        tf = getattr(self.cfg.risk, "stop_timeframe", "5m")
        atr_len = int(getattr(self.cfg.risk, "atr_len", 28))

        while not self.stop_event.is_set():
            try:
                time.sleep(max(1, fast_s))
                positions = self.ex.fetch_positions() or {}
                if not positions:
                    continue

                for sym, pdct in positions.items():
                    qty = float(pdct.get("net_qty") or 0.0)
                    if qty == 0.0:
                        # clear if any residual state
                        self.state.get("perpos", {}).pop(sym, None)
                        self.state.get("enter_bar_time", {}).pop(sym, None)
                        self.state.get("locked_r", {}).pop(sym, None)
                        continue

                    nowts = float(time.time())
                    if nowts - float(self._last_ohlcv_ts.get(sym, 0.0)) > 55.0:
                        try:
                            raw = self.ex.fetch_ohlcv(sym, tf, limit=max(60, atr_len + 10))
                            if not raw:
                                continue
                            df = pd.DataFrame(raw, columns=["ts","open","high","low","close","volume"])
                            df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
                            df.set_index("dt", inplace=True)
                            # treat last rows as CLOSED bars (ccxt OHLCV returns closed bars)
                            self._last_closed_bar[sym] = df.tail(max(3, self.stop_confirm_bars + 1)).copy()
                            self._atr_cache[sym] = float(self._to_float(compute_atr(df, n=atr_len, method="rma").iloc[-1], default=np.nan))
                            self._last_ohlcv_ts[sym] = nowts
                        except Exception as e:
                            log.debug(f"fast fetch_ohlcv {sym} failed: {e}")
                            continue

                    closed_tail = self._last_closed_bar.get(sym)
                    if closed_tail is None or closed_tail.empty:
                        continue
                    last_closed = closed_tail.iloc[-1]

                    entry_px = float(pdct.get("entryPrice") or last_closed["close"])
                    self._init_or_update_perpos(sym, qty, last_closed, self._atr_cache.get(sym, 0.0), entry_px)

                    # compute R now (closed bar)
                    pinfo = self.state["perpos"].get(sym, {})
                    ep = float(pinfo.get("entry_price") or entry_px)
                    atr0 = float(pinfo.get("entry_atr") or self._atr_cache.get(sym, 0.0) or 1.0)
                    last_c = float(last_closed["close"])
                    R_unit = atr0 * float(getattr(self.cfg.risk, "atr_mult_sl", 2.0))
                    rr_now = abs(last_c - ep) / (R_unit if R_unit > 1e-12 else 1.0)

                    # partials on closed bar progress
                    self._maybe_partial_tp(sym, qty, rr_now)

                    # time-based exit
                    max_hours = int(getattr(self.cfg.risk, "max_hours_in_trade", 0) or 0)
                    if max_hours > 0:
                        enter_iso = self.state.get("enter_bar_time", {}).get(sym)
                        if enter_iso:
                            age_h = (pd.Timestamp.utcnow() - pd.Timestamp(enter_iso)).total_seconds() / 3600.0
                            if age_h >= max_hours:
                                self._place_exit(sym, qty, "TIME-EXIT")
                                continue

                    # compute stops
                    normal_px, cat_px, ep, R_unit, sign = self._compute_stop_px(sym, last_closed)
                    if sign == 0:
                        continue

                    # hold gate for NORMAL stop (catastrophic always live)
                    allow_normal = (self.min_hold_minutes <= 0) or (self._minutes_held(sym) >= self.min_hold_minutes)

                    # confirm logic on CLOSED bars
                    hit_normal = False
                    hit_cat = False
                    if normal_px is not None and allow_normal:
                        if self.stop_confirm_bars <= 0:
                            if (sign > 0 and last_closed["close"] <= normal_px) or (sign < 0 and last_closed["close"] >= normal_px):
                                hit_normal = True
                        else:
                            # require N consecutive closes beyond the stop
                            tail = closed_tail.tail(self.stop_confirm_bars)
                            if sign > 0:
                                hit_normal = bool((tail["close"] <= normal_px).all())
                            else:
                                hit_normal = bool((tail["close"] >= normal_px).all())

                    if cat_px is not None:
                        if (sign > 0 and last_closed["low"] <= cat_px) or (sign < 0 and last_closed["high"] >= cat_px):
                            hit_cat = True

                    # trailing breach (based on closed price)
                    if bool(getattr(self.cfg.risk, "trailing_enabled", True)) and self.trail_mult > 0 and not hit_cat and not hit_normal:
                        if sign > 0:
                            trail = float(max(pinfo.get("trail_hh", last_c), last_c)) - self.trail_mult * (self._atr_cache.get(sym, atr0) or atr0)
                            if last_closed["close"] <= trail:
                                hit_normal = True
                        else:
                            trail = float(min(pinfo.get("trail_ll", last_c), last_c)) + self.trail_mult * (self._atr_cache.get(sym, atr0) or atr0)
                            if last_closed["close"] >= trail:
                                hit_normal = True

                    if hit_cat:
                        self._place_exit(sym, qty, "CAT-STOP")
                        continue
                    if hit_normal:
                        self._place_exit(sym, qty, "STOP")
                        continue

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
            raw = ex.fetch_ohlcv(sym, tf, limit=max(60, atr_len + 10))
            if not raw:
                continue
            df = pd.DataFrame(raw, columns=["ts","open","high","low","close","volume"])
            df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
            df.set_index("dt", inplace=True)
            lr = df.iloc[-1]
            atr_val = float(compute_atr(df, n=atr_len, method="rma").iloc[-1])
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
            state.setdefault("locked_r", {})[sym] = float(state.get("locked_r", {}).get(sym, 0.0) or 0.0)
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
        state.get("locked_r", {}).pop(sym, None)

    write_json(getattr(cfg.paths, "state_path", "state/state.json"), state)
    if live_syms:
        log.info(f"Startup reconcile: attached to {len(live_syms)} live positions: {', '.join(sorted(live_syms))}")
    else:
        log.info("Startup reconcile: no live positions found on exchange.")

# -------------------- ORDER BOOK RECONCILIATION (stale limits) --------------------

def _mid_price(tkr: dict) -> Optional[float]:
    if not isinstance(tkr, dict):
        return None
    bid = tkr.get("bid") or tkr.get("bidPrice")
    ask = tkr.get("ask") or tkr.get("askPrice")
    last = tkr.get("last") or tkr.get("close")
    try:
        if bid and ask and float(bid) > 0 and float(ask) > 0:
            return (float(bid) + float(ask)) / 2.0
        return float(last) if last else None
    except Exception:
        return None

def _desired_side_from_weight(w: float) -> Optional[str]:
    if w is None or abs(w) < 1e-12:
        return None
    return "buy" if w > 0 else "sell"

def _reconcile_open_orders(
    ex: ExchangeWrapper,
    cfg: AppConfig,
    targets: pd.Series,          # symbol -> weight
    positions: dict,             # symbol -> current position dict (optional use)
    tickers: dict,               # symbol -> ticker dict
    eligible_syms: set,          # symbols still allowed by regime/cooldowns
    state: dict,
    now_ts: float,
):
    st = getattr(cfg.execution, "stale_orders", None)
    if not st or not getattr(st, "enabled", False):
        return

    # Throttle cleanup frequency
    last_ts = float(state.get("last_stale_cleanup_ts", 0.0) or 0.0)
    if (now_ts - last_ts) < int(getattr(st, "cleanup_interval_sec", 60)):
        return

    max_age = int(getattr(st, "max_age_sec", 180))
    far_bps = float(getattr(st, "reprice_if_far_bps", 15.0))
    cancel_if_not_targeted = bool(getattr(st, "cancel_if_not_targeted", True))
    keep_reduce_only = bool(getattr(st, "keep_reduce_only", True))

    # Try bulk fetch; fall back to per-symbol if not supported
    open_by_sym: Dict[str, List[dict]] = {}
    try:
        all_open = ex.fetch_open_orders(None)
        for od in (all_open or []):
            s = od.get("symbol")
            if s:
                open_by_sym.setdefault(s, []).append(od)
    except Exception:
        for s in list(targets.index):
            try:
                open_by_sym[s] = ex.fetch_open_orders(s) or []
            except Exception:
                pass

    for s, odlist in open_by_sym.items():
        if not odlist:
            continue

        tkr = tickers.get(s, {}) or {}
        mid = _mid_price(tkr)
        tgt_w = float(targets.get(s, 0.0) or 0.0)
        desired_side = _desired_side_from_weight(tgt_w)
        still_eligible = (s in eligible_syms)

        # If symbol not eligible (regime/cooldown) or not targeted, cancel (except reduce-only if we keep them)
        if cancel_if_not_targeted and (desired_side is None or not still_eligible):
            try:
                if keep_reduce_only:
                    for od in odlist:
                        if od.get("reduceOnly") or od.get("reduce_only"):
                            continue
                        try:
                            ex.cancel_order(od.get("id"), s)
                        except Exception:
                            pass
                else:
                    ex.cancel_all_orders(s)
                log.info(f"[CLEANUP] {s}: canceled non-RO orders (not targeted/eligible).")
            except Exception as e:
                log.debug(f"cleanup cancel {s} failed: {e}")
            continue

        # Otherwise, cancel/replace orders that:
        # - are on the opposite side vs desired
        # - are too old
        # - are too far from mid
        to_cancel = []
        for od in odlist:
            if keep_reduce_only and (od.get("reduceOnly") or od.get("reduce_only")):
                continue

            od_side = (od.get("side") or "").lower()
            if desired_side and od_side and od_side != desired_side:
                to_cancel.append(od.get("id"))
                continue

            # age check
            age_bad = False
            created = od.get("timestamp") or od.get("time") or od.get("created")
            if created:
                try:
                    created_s = float(created) / (1000.0 if float(created) > 10_000_000_000 else 1.0)
                    age_bad = (now_ts - created_s) > max_age
                except Exception:
                    age_bad = False

            # distance check
            far_bad = False
            if far_bps > 0 and mid:
                try:
                    opx = float(od.get("price") or od.get("limit") or 0.0) or None
                except Exception:
                    opx = None
                if opx:
                    drift_bps = abs(opx - mid) / mid * 10_000.0
                    far_bad = drift_bps >= far_bps

            if age_bad or far_bad:
                to_cancel.append(od.get("id"))

        # Execute cancels
        if to_cancel:
            canceled = 0
            for oid in to_cancel:
                if not oid:
                    continue
                try:
                    ex.cancel_order(oid, s)
                    canceled += 1
                except Exception:
                    # fallback: if id cancel fails, nuke all for symbol once
                    try:
                        ex.cancel_all_orders(s)
                        canceled = len(to_cancel)
                        break
                    except Exception:
                        pass
            if canceled:
                log.info(f"[CLEANUP] {s}: canceled {canceled} stale/opposite/away orders for reprice.")

    state["last_stale_cleanup_ts"] = now_ts

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
    state.setdefault("locked_r", {})
    state.setdefault("last_trade_ts", {})
    state.setdefault("day_date", None)
    state.setdefault("day_high_equity", 0.0)
    state.setdefault("last_stale_cleanup_ts", 0.0)

    day_start_equity = float(state.get("day_start_equity", 0.0))
    day_high_equity = float(state.get("day_high_equity", 0.0))
    disable_until_ts = float(state.get("disable_until_ts", 0.0))
    soft_block_until_ts = float(state.get("soft_block_until_ts", 0.0))

    ex = ExchangeWrapper(cfg.exchange)

    # Safety: cancel all open orders on boot if configured
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
            try:
                ex.set_leverage(s, int(getattr(cfg.execution, "set_leverage", 1)))
            except Exception:
                pass

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

            # Soft WIN lock: block new entries after daily gain target
            swl = getattr(cfg.strategy, "soft_win_lock", None)
            if swl and getattr(swl, "enabled", False) and eq > 0 and day_start_equity > 0:
                gain_from_start = 100.0 * max(0.0, (eq - day_start_equity) / day_start_equity)
                if gain_from_start >= float(getattr(swl, "daily_gain_pct", 0.0) or 0.0):
                    allow_new_entries = False
                    state["soft_win_lock_active"] = True
                    write_json(state_path, state)
                    log.info(f"SOFT WIN LOCK: daily gain {gain_from_start:.2f}% ≥ target; new entries disabled for the day.")

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

            # 2) Regime (per-asset)
            eligible_syms = list(closes.columns)
            if cfg.strategy.regime_filter.enabled:
                ema_len = int(cfg.strategy.regime_filter.ema_len)
                thr = float(cfg.strategy.regime_filter.slope_min_bps_per_day)
                use_abs = bool(getattr(cfg.strategy.regime_filter, "use_abs", False))
                blocked = 0
                eligible_syms = []
                for s in closes.columns:
                    ser = closes[s].dropna()
                    try:
                        ok = regime_ok(ser, ema_len, thr, use_abs=use_abs)
                    except Exception:
                        ok = True
                    if ok:
                        eligible_syms.append(s)
                    else:
                        blocked += 1
                if blocked > 0:
                    log.info(f"Regime gate: {blocked}/{len(closes.columns)} symbols blocked this cycle.")
                if len(eligible_syms) == 0:
                    log.info("Regime gate blocked all symbols; staying flat.")
                    time.sleep(max(1, int(getattr(cfg.execution, "poll_seconds", 5))))
                    continue

            closes_used = closes[eligible_syms] if cfg.strategy.regime_filter.enabled else closes

            # 3) Targets
            funding_map: Dict[str, float] = {}
            if getattr(cfg.strategy.funding_tilt, "enabled", False):
                try:
                    funding_map = ex.fetch_funding_rates(list(bars.keys())) or {}
                except Exception:
                    pass

            targets = build_targets(
                closes_used,
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

            # Reindex to full universe so blocked symbols have 0 weight
            targets = targets.reindex(closes.columns).fillna(0.0)

            # 3.5) Apply cooldowns: zero weights while cooldown active
            try:
                cds = state.get("cooldowns", {}) if isinstance(state, dict) else {}
                if cds:
                    now_ts = pd.Timestamp.utcnow()
                    for s in list(targets.index):
                        until_iso = cds.get(s)
                        if until_iso:
                            try:
                                if pd.Timestamp(until_iso) > now_ts:
                                    targets.loc[s] = 0.0
                                else:
                                    # cooldown expired
                                    state["cooldowns"].pop(s, None)
                            except Exception:
                                pass
            except Exception:
                pass

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

            # 4.5) Reconcile & clean stale/open orders BEFORE creating new ones
            positions = ex.fetch_positions() or {}
            _reconcile_open_orders(
                ex=ex,
                cfg=cfg,
                targets=targets,
                positions=positions,
                tickers=tickers or {},
                eligible_syms=set(eligible_syms) if cfg.strategy.regime_filter.enabled else set(targets.index),
                state=state,
                now_ts=time.time(),
            )

            # 5) Orders
            min_notional = float(getattr(cfg.execution, "min_notional_per_order_usdt", 5.0))
            min_delta_bps = float(getattr(cfg.execution, "min_rebalance_delta_bps", 1.0))

            open_symbols = [s for s, p in positions.items() if not _is_new_position(p)]
            remaining_slots = max(0, getattr(cfg.strategy.entry_throttle, "max_open_positions", 999) - len(open_symbols))
            new_entries_cap = min(getattr(cfg.strategy.entry_throttle, "max_new_positions_per_cycle", 999), remaining_slots)

            now_ts_fn = time.time
            last_ts_map: Dict[str, float] = state.get("last_trade_ts", {})

            # throttle: honor per-symbol trade cooldown; use weight magnitude as tiebreaker
            candidates: List[Tuple[str, float]] = []
            for s, w in targets.items():
                pos = positions.get(s, {}) or {}
                if _is_new_position(pos) and w != 0.0:
                    if not allow_new_entries:
                        continue
                    last_t = float(last_ts_map.get(s, 0.0))
                    if (now_ts_fn() - last_t) < 60.0 * getattr(cfg.strategy.entry_throttle, "per_symbol_trade_cooldown_min", 0):
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
