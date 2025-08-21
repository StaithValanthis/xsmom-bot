# v1.1.0 – 2025-08-21
import logging
import threading
import time
from time import perf_counter
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional

import numpy as np
import pandas as pd

from .config import AppConfig
from .exchange import ExchangeWrapper
from .signals import regime_ok, compute_atr, dynamic_k
from .sizing import build_targets, apply_liquidity_caps
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
            if px is None or float(px) <= 0:
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


# -------------------- FAST EVENT-DRIVEN SL/TP THREAD --------------------

class FastSLTPThread(threading.Thread):
    """
    Poll-based 'event-driven' SL/TP enforcement:
      - Every fast_check_seconds: read open positions, last price, compare to stop levels.
      - Every ~60s (per symbol): refresh 1m/3m OHLCV to recompute ATR & trailing anchors.

    Maintains state["perpos"][sym] = {sign, entry_price, entry_atr, trail_hh, trail_ll, partial_done}
    Also uses state["enter_bar_time"][sym] for time-based exits.
    """
    def __init__(self, ex: ExchangeWrapper, cfg: AppConfig, state: dict, dry: bool, stop_event: threading.Event):
        super().__init__(daemon=True)
        self.ex = ex
        self.cfg = cfg
        self.state = state
        self.dry = dry
        self.stop_event = stop_event
        self._last_ohlcv_ts: Dict[str, float] = {}  # symbol -> unix ts of last ATR refresh
        self._last_bar_cache: Dict[str, pd.Series] = {}

        # ensure required state buckets exist
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

        close_v = self._to_float(lr.get("close"), default=None)
        high_v = self._to_float(lr.get("high"), default=None)
        low_v  = self._to_float(lr.get("low"), default=None)
        atr_v  = self._to_float(atr_val, default=None)
        entry_v = self._to_float(entry_price, default=None)

        if entry_v is None:
            entry_v = close_v

        # new position or side flip → (re)init trails and start timer
        if (not isinstance(pinfo, dict)) or (int(pinfo.get("sign", 0) or 0) != sign):
            perpos[symbol] = {
                "sign": sign,
                "entry_price": float(entry_v),
                "entry_atr": float(atr_v),
                "trail_hh": float(high_v),
                "trail_ll": float(low_v),
                "partial_done": False,
            }
            self.state.setdefault("enter_bar_time", {})[symbol] = pd.Timestamp.utcnow()
        else:
            # update trails
            if sign > 0:
                pinfo["trail_hh"] = float(max(self._to_float(pinfo.get("trail_hh"), high_v), float(high_v)))
            else:
                pinfo["trail_ll"] = float(min(self._to_float(pinfo.get("trail_ll"), low_v), float(low_v)))
            perpos[symbol] = pinfo

    def _compute_stop_px(self, symbol: str, lr: pd.Series) -> Optional[float]:
        cfg = self.cfg
        pinfo = self.state.get("perpos", {}).get(symbol)
        if not isinstance(pinfo, dict):
            return None

        sign = int(pinfo.get("sign", 0) or 0)
        entry_price = self._to_float(pinfo.get("entry_price"), default=None)
        entry_atr   = self._to_float(pinfo.get("entry_atr"), default=None)
        if entry_price is None or entry_atr is None or entry_atr <= 0:
            return None

        atr_mult_sl = float(cfg.risk.atr_mult_sl)
        trail_enabled = bool(cfg.risk.trailing_enabled)
        trail_k = float(cfg.risk.trail_atr_mult)

        # initial stop from entry
        init_sl = float(entry_price - atr_mult_sl * entry_atr) if sign > 0 else float(entry_price + atr_mult_sl * entry_atr)

        # ATR from last refresh
        cur_atr = self._to_float(lr.get("_atr"), default=None)
        if cur_atr is None or cur_atr <= 0:
            return float(init_sl)

        # trail anchors
        hh = self._to_float(pinfo.get("trail_hh"), default=self._to_float(lr.get("high"), default=None))
        ll = self._to_float(pinfo.get("trail_ll"), default=self._to_float(lr.get("low"), default=None))

        if trail_enabled:
            if sign > 0:
                trail_sl = float(hh - trail_k * cur_atr)
                stop_price = max(init_sl, trail_sl)
            else:
                trail_sl = float(ll + trail_k * cur_atr)
                stop_price = min(init_sl, trail_sl)
        else:
            stop_price = init_sl

        # Breakeven bump after +R
        R = float(entry_atr * atr_mult_sl)
        be_after = float(cfg.risk.breakeven_after_r)
        if be_after > 0.0:
            high_v = self._to_float(lr.get("high"), default=None)
            low_v  = self._to_float(lr.get("low"), default=None)
            if sign > 0:
                be_trigger = float(entry_price + be_after * R)
                if high_v is not None and high_v >= be_trigger:
                    stop_price = max(stop_price, float(entry_price))
            else:
                be_trigger = float(entry_price - be_after * R)
                if low_v is not None and low_v <= be_trigger:
                    stop_price = min(stop_price, float(entry_price))

        return float(stop_price)

    def _maybe_partial_tp(self, symbol: str, cur_qty: float, lr: pd.Series):
        cfg = self.cfg
        if not bool(cfg.risk.partial_tp_enabled):
            return
        perpos = self.state.get("perpos", {})
        pinfo = perpos.get(symbol)
        if not isinstance(pinfo, dict) or pinfo.get("partial_done", False):
            return

        sign = int(pinfo.get("sign", 0) or 0)
        entry_price = self._to_float(pinfo.get("entry_price"), default=None)
        entry_atr   = self._to_float(pinfo.get("entry_atr"), default=None)
        last_px     = self._to_float(lr.get("last"), default=self._to_float(lr.get("close"), default=None))
        if entry_price is None or entry_atr is None or last_px is None:
            return

        R = float(entry_atr * float(cfg.risk.atr_mult_sl))
        ptp_r = float(cfg.risk.partial_tp_r)
        ptp_px = float(entry_price + (ptp_r * R if sign > 0 else -ptp_r * R))
        hit = (last_px >= ptp_px) if sign > 0 else (last_px <= ptp_px)
        if bool(hit):
            size_frac = float(cfg.risk.partial_tp_size)
            q = abs(float(cur_qty)) * size_frac
            q, _ = self.ex.quantize(symbol, q, None)
            if q > 0:
                if self.dry:
                    log.info(f"[DRY-PARTIAL] {symbol}: {'sell' if sign>0 else 'buy'} {q} @ mkt")
                else:
                    self.ex.create_order_safe(symbol, "sell" if sign > 0 else "buy", q, None, post_only=False, reduce_only=True)
                    log.info(f"[LIVE-PARTIAL] {symbol}: {'sell' if sign>0 else 'buy'} {q} @ mkt")
            pinfo["partial_done"] = True
            perpos[symbol] = pinfo
            self.state["perpos"] = perpos

    def run(self):
        cfg = self.cfg
        fast_s = int(getattr(cfg.risk, "fast_check_seconds", 0))
        if fast_s <= 0:
            log.info("Fast SL/TP loop disabled (fast_check_seconds <= 0).")
            return

        log.info(f"Fast SL/TP loop starting: check every {fast_s}s on timeframe={cfg.risk.stop_timeframe}")
        stop_tf = getattr(cfg.risk, "stop_timeframe", cfg.exchange.timeframe) or cfg.exchange.timeframe
        atr_len = int(cfg.risk.atr_len)

        while not self.stop_event.is_set():
            try:
                # 1) fetch positions
                pos_map = self.ex.fetch_positions_map() or {}
                if not pos_map:
                    # Clear all perpos & timers if nothing is open
                    if self.state.get("perpos"):
                        self.state["perpos"] = {}
                    if self.state.get("enter_bar_time"):
                        self.state["enter_bar_time"] = {}
                    time.sleep(fast_s)
                    continue

                # 2) iterate open positions
                for sym, p in list(pos_map.items()):
                    # derive signed qty (long +, short -)
                    try:
                        qty_raw = p.get("contracts") or p.get("positionAmt") or p.get("contractsSize") or 0.0
                        qty = self._to_float(qty_raw, default=0.0)
                        side = p.get("side")
                        if side == "short" and qty > 0:
                            qty = -qty
                    except Exception:
                        qty = 0.0
                    if float(qty) == 0.0:
                        # clear state if any
                        if self.state.get("perpos", {}).get(sym):
                            self.state["perpos"].pop(sym, None)
                        if self.state.get("enter_bar_time", {}).get(sym):
                            self.state["enter_bar_time"].pop(sym, None)
                        continue

                    # 3) refresh OHLCV/ATR for this symbol at most once per ~60s
                    last_ts = float(self._last_ohlcv_ts.get(sym, 0.0) or 0.0)
                    need_ohlcv = (self._now_ts() - last_ts) > 55.0
                    if need_ohlcv:
                        raw = self.ex.fetch_ohlcv(sym, timeframe=stop_tf, limit=max(60, atr_len * 5))
                        if not raw:
                            continue
                        df = pd.DataFrame(raw, columns=["ts","open","high","low","close","volume"])
                        df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
                        df.set_index("dt", inplace=True)
                        if len(df) < max(20, atr_len + 5):
                            continue
                        atr_series = compute_atr(df, n=atr_len, method="rma")
                        last_bar = df.iloc[-1].copy()
                        last_bar["_atr"] = float(self._to_float(atr_series.iloc[-1], default=np.nan))
                        self._last_ohlcv_ts[sym] = self._now_ts()
                        self._last_bar_cache[sym] = last_bar
                    else:
                        last_bar = self._last_bar_cache.get(sym)
                        if last_bar is None:
                            # force refresh if cache empty
                            self._last_ohlcv_ts[sym] = 0.0
                            continue

                    # 4) embed latest last price (fetch_ticker)
                    try:
                        t = self.ex.x.fetch_ticker(sym)
                        last_px = self._to_float(t.get("last") or t.get("close"), default=None)
                    except Exception:
                        last_px = None
                    if last_px is None:
                        last_px = self._to_float(last_bar.get("close"), default=None)
                    if last_px is None:
                        # cannot evaluate stops w/o a price
                        continue

                    lr = last_bar.copy()
                    lr["last"] = float(last_px)

                    # entry price
                    try:
                        entry_px = p.get("entryPrice") or p.get("averagePrice") or p.get("avgPrice")
                        entry_px = self._to_float(entry_px, default=None)
                    except Exception:
                        entry_px = None

                    # 5) ensure perpos exists/updated
                    self._init_or_update_perpos(sym, float(qty), lr, float(lr.get("_atr", np.nan)), entry_px)

                    # 6) compute stop price, then check live price cross
                    stop_px = self._compute_stop_px(sym, lr)
                    if stop_px is not None:
                        long_pos = float(qty) > 0.0
                        hit = (float(last_px) <= float(stop_px)) if long_pos else (float(last_px) >= float(stop_px))
                        if bool(hit):
                            q = abs(float(qty))
                            q, _ = self.ex.quantize(sym, q, None)
                            if q > 0:
                                if self.dry:
                                    log.info(f"[DRY-FAST-STOP] {sym}: {'sell' if long_pos else 'buy'} {q} @ mkt (SL {stop_px:.6g})")
                                else:
                                    self.ex.create_order_safe(sym, "sell" if long_pos else "buy", q, None, post_only=False, reduce_only=True)
                                    log.info(f"[LIVE-FAST-STOP] {sym}: {'sell' if long_pos else 'buy'} {q} @ mkt (SL {stop_px:.6g})")
                            # clear perpos after exit
                            self.state.get("perpos", {}).pop(sym, None)
                            self.state.get("enter_bar_time", {}).pop(sym, None)
                            # move on to next position
                            continue

                    # 7) partial take profit check
                    self._maybe_partial_tp(sym, float(qty), lr)

                    # 8) time-based exit
                    try:
                        max_hours_in_trade = int(getattr(self.cfg.risk, "max_hours_in_trade", 0))
                        entered = self.state.get("enter_bar_time", {}).get(sym, None)
                        if max_hours_in_trade > 0 and entered is not None:
                            hours = (pd.Timestamp.utcnow() - pd.Timestamp(entered)).total_seconds() / 3600.0
                            if hours >= max_hours_in_trade:
                                q = abs(float(qty))
                                q, _ = self.ex.quantize(sym, q, None)
                                if q > 0:
                                    if self.dry:
                                        log.info(f"[DRY-TIME-EXIT] {sym}: close {q} @ mkt after {hours:.1f}h")
                                    else:
                                        self.ex.create_order_safe(sym, "sell" if qty > 0 else "buy", q, None, post_only=False, reduce_only=True)
                                        log.info(f"[LIVE-TIME-EXIT] {sym}: close {q} @ mkt after {hours:.1f}h")
                                self.state.get("perpos", {}).pop(sym, None)
                                self.state.get("enter_bar_time", {}).pop(sym, None)
                                continue
                    except Exception:
                        pass

                time.sleep(fast_s)
            except Exception:
                log.exception("Fast SL/TP loop error")
                time.sleep(max(1, fast_s))


# -------------------- MAIN LIVE LOOP --------------------

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
            "perpos": {},
            "cooldowns": {},
            "enter_bar_time": {},
        },
    )
    # Ensure buckets exist even if state file was older
    state.setdefault("perpos", {})
    state.setdefault("cooldowns", {})
    state.setdefault("enter_bar_time", {})

    # start fast SL/TP thread if enabled
    stop_evt = threading.Event()
    fast_thread: Optional[FastSLTPThread] = None
    try:
        if int(getattr(cfg.risk, "fast_check_seconds", 0)) > 0:
            fast_thread = FastSLTPThread(ex, cfg, state, dry, stop_evt)
            fast_thread.start()

        # Bootstrap universe at startup
        refresh_universe(ex, state, state_path)
        log.info(f"Initial universe: {len(state.get('universe', []))} symbols")

        first_cycle = True
        # Keep original gross leverage for auto-delever reference
        orig_gl = float(cfg.strategy.gross_leverage)

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

            # Equity & kill switch + auto-delever
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

                from .risk import kill_switch_should_trigger, resume_time_after_kill
                if kill_switch_should_trigger(state.get("day_start_equity") or 0.0, equity, cfg.risk.max_daily_loss_pct):
                    pause_until = resume_time_after_kill(now, cfg.risk.trade_disable_minutes)
                    state["trading_paused_until"] = pause_until.isoformat()
                    write_json(state_path, state)
                    log.error(f"Kill switch triggered. Pausing until {pause_until.isoformat()}")
                    log.info(f"=== Cycle end (kill switch) {utcnow().isoformat()} ===")
                    continue

                # Auto-delever based on intraday drawdown vs day_start_equity
                try:
                    day_start = float(state.get("day_start_equity") or equity)
                    dd_pct = 100.0 * max(0.0, (day_start - equity) / max(1e-9, day_start))
                    scale = 1.0
                    if dd_pct >= 1.0:
                        scale = 0.85
                    if dd_pct >= 2.0:
                        scale = 0.7
                    if dd_pct >= 3.0:
                        scale = 0.5
                    new_gl = max(0.8, float(orig_gl) * float(scale))
                    if abs(new_gl - float(cfg.strategy.gross_leverage)) > 1e-9:
                        cfg.strategy.gross_leverage = new_gl
                        log.info(f"Auto-delever: intraday DD {dd_pct:.2f}% → gross_leverage={cfg.strategy.gross_leverage:.2f}")
                except Exception as e:
                    log.debug(f"auto-delever skipped: {e}")

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

            # Regime filter (UPDATED: pass use_abs)
            try:
                if cfg.strategy.regime_filter.enabled:
                    ok = regime_ok(
                        closes.mean(axis=1),
                        cfg.strategy.regime_filter.ema_len,
                        cfg.strategy.regime_filter.slope_min_bps_per_day,
                        use_abs=bool(getattr(cfg.strategy.regime_filter, "use_abs", False)),
                    )
                    if not ok:
                        log.info("Regime filter blocking new entries this cycle.")
                        log.info(f"=== Cycle end (regime) {utcnow().isoformat()} ===")
                        continue
            except Exception as e:
                log.warning(f"Regime filter calc failed (not blocking): {e}")

            # Targets (now using dynamic_k and advanced knobs)
            try:
                t_targets_start = perf_counter()

                # Optional: funding tilt snapshot (live)
                if getattr(cfg.strategy.funding_tilt, "enabled", False):
                    try:
                        funding_map = ex.fetch_funding_rates(list(closes.columns)) or {}
                    except Exception:
                        funding_map = {}
                else:
                    funding_map = None

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
                    funding_tilt=funding_map,
                    funding_weight=float(getattr(cfg.strategy.funding_tilt, "weight", 0.0))
                        if getattr(cfg.strategy.funding_tilt, "enabled", False) else 0.0,
                    entry_zscore_min=float(getattr(cfg.strategy, "entry_zscore_min", 0.0)),
                    diversify_enabled=bool(getattr(cfg.strategy.diversify, "enabled", False)),
                    corr_lookback=int(getattr(cfg.strategy.diversify, "corr_lookback", 48)),
                    max_pair_corr=float(getattr(cfg.strategy.diversify, "max_pair_corr", 0.9)),
                    vol_target_enabled=bool(getattr(cfg.strategy.vol_target, "enabled", False)),
                    target_daily_vol_bps=float(getattr(cfg.strategy.vol_target, "target_daily_vol_bps", 0.0)),
                    vol_target_min_scale=float(getattr(cfg.strategy.vol_target, "min_scale", 0.5)),
                    vol_target_max_scale=float(getattr(cfg.strategy.vol_target, "max_scale", 2.0)),
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

            # Per-symbol leverage setting
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

            # Orders for rebalance (respect PnL gate + skip dust/tiny rebalances)
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

                    # Skip tiny rebalances and dust orders for small accounts
                    try:
                        min_delta = float(cfg.execution.min_rebalance_delta_bps) / 10_000.0 * float(equity)
                        est_notional = abs(diff) * float(prices.get(s, 0.0) or 0.0)
                        if est_notional < max(min_delta, float(cfg.execution.min_notional_per_order_usdt)):
                            continue
                    except Exception:
                        pass

                    # PnL gate for reductions/flips
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
            if stop_evt and isinstance(stop_evt, threading.Event):
                stop_evt.set()
            if fast_thread and fast_thread.is_alive():
                fast_thread.join(timeout=2.0)
        except Exception:
            pass
        try:
            ex.close()
        except Exception:
            pass
