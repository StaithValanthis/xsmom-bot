# v1.8.0 – 2025-09-04
# - Added portfolio-level scaler: VolTarget × DD Stepdown × Fractional-Kelly
# - Re-cap weights AFTER multipliers (per-asset and per-symbol notional)
# - Router hardening: whitespace/case tolerant + explicit logging of config mode/raw
# - Keeps: microstructure gate, ADX, regime, majors downweight/block, MTF confirm,
#          Kelly per-symbol scaling, ToD boost, symbol scoring/banlist, stale order cleanup,
#          risk stepdown tiers, funding tilt, diversify, vol target (legacy), etc.
from __future__ import annotations
import logging
import threading
import time
import math
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import re

from .config import AppConfig
from .exchange import ExchangeWrapper
from .signals import compute_atr, regime_ok, dynamic_k
from .sizing import build_targets, apply_liquidity_caps, apply_kelly_scaling
from .regime_router import build_targets_auto, decide_mode
from .risk import kill_switch_should_trigger, resume_time_after_kill
from .utils import utcnow, read_json, write_json
from .carry import (
    parse_carry_cfg,
    build_funding_carry_weights,
    build_basis_carry_weights,
    combine_sleeves,
)

log = logging.getLogger("live")

# -------------------- config bridge for carry --------------------

def _cfg_to_dict(cfg: AppConfig) -> dict:
    """Convert pydantic AppConfig to a plain dict for parse_carry_cfg, with fallbacks."""
    try:
        return cfg.model_dump()  # pydantic v2
    except Exception:
        pass
    try:
        import json as _json
        return _json.loads(cfg.model_dump_json())
    except Exception:
        pass
    def _obj_to_dict(x):
        if isinstance(x, dict):
            return {k: _obj_to_dict(v) for k, v in x.items()}
        if hasattr(x, 'model_dump'):
            try:
                return _obj_to_dict(x.model_dump())
            except Exception:
                pass
        if hasattr(x, 'dict'):
            try:
                return _obj_to_dict(x.dict())
            except Exception:
                pass
        if hasattr(x, '__dict__'):
            try:
                return _obj_to_dict(vars(x))
            except Exception:
                pass
        return x
    return _obj_to_dict(cfg)


# -------------------- microstructure / helpers --------------------

def _micro_ok(cfg: AppConfig, tkr: dict, orderbook: dict | None) -> bool:
    mc = getattr(cfg.execution, "microstructure", None)
    if not mc or not getattr(mc, "enabled", False):
        return True
    try:
        bid = float(tkr.get("bid") or tkr.get("bidPrice") or 0.0)
        ask = float(tkr.get("ask") or tkr.get("askPrice") or 0.0)
        if bid <= 0 or ask <= 0:
            return True
        spread_bps = 10000.0 * (ask - bid) / ((ask + bid) / 2.0)
        if spread_bps > float(getattr(mc, "max_spread_bps", 8.0)):
            return False
        if orderbook and (bids := orderbook.get("bids")) and (asks := orderbook.get("asks")):
            bvol = sum([float(x[1]) for x in bids[:5]])
            avol = sum([float(x[1]) for x in asks[:5]])
            obi = (bvol - avol) / max(bvol + avol, 1e-9)
            if abs(obi) < float(getattr(mc, "min_obi", 0.15)):
                return False
        return True
    except Exception:
        return True

def _majors_gate(ex: ExchangeWrapper, cfg: AppConfig, timeframe: str, candles_limit: int) -> tuple[bool, int]:
    mj = getattr(cfg.strategy, "majors_regime", None)
    if not mj or not getattr(mj, "enabled", False):
        return True, 0
    ok = 0
    for s in list(getattr(mj, "majors", [])):
        try:
            raw = ex.fetch_ohlcv(s, timeframe=timeframe, limit=candles_limit)
            if not raw:
                ok += 1
                continue
            df = pd.DataFrame(raw, columns=["ts","open","high","low","close","volume"])
            df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
            df.set_index("dt", inplace=True)
            ser = df["close"]
            if regime_ok(ser, int(mj.ema_len), float(mj.slope_bps_per_day), use_abs=False):
                ok += 1
        except Exception:
            ok += 1
    return (ok >= 2), ok

def _normalize_soft_block(value):
    try:
        if value is None:
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0

def _state_get_hourstats(state: dict) -> dict:
    return state.setdefault("hour_stats", {})

def _ema_update(prev: Optional[float], value: float, alpha: float) -> float:
    if prev is None:
        return value
    return float(alpha * value + (1 - alpha) * prev)

def _update_hour_stats_on_close(state: dict, cfg: AppConfig, symbol: str, pnl_usdt: float, entry_time_iso: Optional[str]) -> None:
    tod_cfg = getattr(cfg.strategy, "time_of_day_whitelist", None)
    if not tod_cfg or not getattr(tod_cfg, "enabled", False):
        return
    try:
        if entry_time_iso:
            et = pd.Timestamp(entry_time_iso)
        else:
            et = pd.Timestamp.utcnow()
        hour = int(et.hour)
        hs = _state_get_hourstats(state)
        rec = hs.get(str(hour)) or {"n": 0, "sum": 0.0, "ema_pnl_bps": None}
        pnl = float(pnl_usdt or 0.0)
        rec["n"] = int(rec["n"]) + 1
        rec["sum"] = float(rec["sum"] or 0.0) + pnl
        alpha = float(getattr(tod_cfg, "ema_alpha", 0.2))
        rec["ema_usdt"] = _ema_update(rec.get("ema_usdt"), pnl, alpha)
        hs[str(hour)] = rec
        state["hour_stats"] = hs
    except Exception as e:
        log.debug(f"hour-stats update failed: {e}")

def _safe_float(x, default: Optional[float] = None) -> Optional[float]:
    try:
        return float(x)
    except Exception:
        return default

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

def _spread_bps(tkr: dict) -> Optional[float]:
    try:
        bid = float(tkr.get("bid") or tkr.get("bidPrice") or 0.0)
        ask = float(tkr.get("ask") or tkr.get("askPrice") or 0.0)
        if bid > 0 and ask > 0 and ask >= bid:
            return (ask - bid) / ((ask + bid) / 2.0) * 10_000.0
    except Exception:
        pass
    return None

def _is_new_position(pos: dict) -> bool:
    return abs(float(pos.get("net_qty") or 0.0)) < 1e-9

def _minute_aligned(minute: int) -> bool:
    if minute <= 0:
        return True
    now = utcnow()
    return (now.minute % minute) == 0

def _dd_pct(ref: float, noweq: float) -> float:
    if ref is None or ref <= 0 or noweq is None or noweq <= 0:
        return 0.0
    return 100.0 * max(0.0, (ref - noweq) / ref)

# -------- New helpers: pending orders & precision / limits ----------

def _sum_pending_same_side(open_orders: List[dict] | None, side: str) -> float:
    """Sum remaining qty for non-reduce-only open orders on the same side."""
    q = 0.0
    for od in open_orders or []:
        if od.get("reduceOnly") or od.get("reduce_only"):
            continue
        st = (od.get("status") or "").lower()
        if st in ("closed", "canceled"):
            continue
        if (od.get("side") or "").lower() == (side or "").lower():
            rem = od.get("remaining", od.get("amount", 0.0))
            try:
                q += float(rem or 0.0)
            except Exception:
                pass
    return q

def _get_symbol_specs(ex, sym: str, state: dict | None = None) -> dict:
    """
    Best-effort limits/precision from wrapper/ccxt + learned cache from prior rejections.
    Returns: {amount_step, amount_min, min_notional, integer_amount}
    """
    # Learned cache from prior rejections
    cache_min = None
    try:
        if isinstance(state, dict):
            cache_min = float((state.get("min_qty_cache", {}) or {}).get(sym))
    except Exception:
        cache_min = None

    # Prefer wrapper helper if available
    if hasattr(ex, "get_symbol_specs"):
        try:
            specs = ex.get_symbol_specs(sym) or {}
            if cache_min and (not specs.get("amount_min") or specs["amount_min"] < cache_min):
                specs["amount_min"] = cache_min
            return specs
        except Exception:
            pass
    # Fallback to ccxt market metadata
    m = None
    if hasattr(ex, "exchange"):
        try:
            m = ex.exchange.market(sym)
        except Exception:
            m = None
    specs = {"amount_step": 0.0, "amount_min": 0.0, "min_notional": 0.0, "integer_amount": False}
    if m:
        limits = m.get("limits", {}) or {}
        amt = limits.get("amount", {}) or {}
        cost = limits.get("cost", {}) or {}
        prec = m.get("precision", {}) or {}
        info = m.get("info", {}) or {}

        # CCXT normalized (if present)
        if amt.get("step") is not None:
            try: specs["amount_step"] = float(amt["step"])
            except Exception: pass
        if amt.get("min") is not None:
            try: specs["amount_min"]  = float(amt["min"])
            except Exception: pass
        if cost.get("min") is not None:
            try: specs["min_notional"] = float(cost["min"])
            except Exception: pass

        # Bybit native lot filter
        lsf = info.get("lotSizeFilter") or {}
        for k in ("qtyStep", "minOrderQty"):
            v = lsf.get(k)
            if v is not None:
                try:
                    v = float(v)
                    if k == "qtyStep" and (specs["amount_step"] == 0.0 or v > specs["amount_step"]):
                        specs["amount_step"] = v
                    if k == "minOrderQty" and (specs["amount_min"] == 0.0 or v > specs["amount_min"]):
                        specs["amount_min"] = v
                except Exception:
                    pass

        # Some listings are integer contracts (e.g., 1000BTT)
        specs["integer_amount"] = bool(prec.get("amount") == 0 or m.get("contractSize") == 1)

    # Apply learned cache if larger
    if cache_min and (specs["amount_min"] == 0.0 or specs["amount_min"] < cache_min):
        specs["amount_min"] = cache_min

    return specs

def _quantize_amount(qty: float, step: float, integer_amount: bool) -> float:
    if qty <= 0:
        return 0.0
    if integer_amount:
        return float(int(math.floor(qty)))
    if step and step > 0:
        return math.floor(qty / step) * step
    return qty


# -------------------- ADX (Wilder DMI) --------------------

def _compute_adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    high = df["high"].astype("float64")
    low = df["low"].astype("float64")
    close = df["close"].astype("float64")

    up = high.diff()
    down = -low.diff()
    plus_dm = ((up > down) & (up > 0)) * up
    minus_dm = ((down > up) & (down > 0)) * down

    tr1 = (high - low)
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    atr = tr.ewm(alpha=1.0 / n, adjust=False).mean()
    plus_di = 100.0 * (plus_dm.ewm(alpha=1.0 / n, adjust=False).mean() / atr).replace([np.inf, -np.inf], np.nan)
    minus_di = 100.0 * (minus_dm.ewm(alpha=1.0 / n, adjust=False).mean() / atr).replace([np.inf, -np.inf], np.nan)
    dx = (abs(plus_di - minus_di) / (plus_di + minus_di).replace(0.0, np.nan)) * 100.0
    adx = dx.ewm(alpha=1.0 / n, adjust=False).mean()
    return adx

def _compute_dmi(df: pd.DataFrame, n: int = 14):
    high = df["high"].astype("float64")
    low = df["low"].astype("float64")
    close = df["close"].astype("float64")

    up = high.diff()
    down = -low.diff()
    plus_dm = ((up > down) & (up > 0)) * up
    minus_dm = ((down > up) & (down > 0)) * down

    tr1 = (high - low)
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    atr = tr.ewm(alpha=1.0 / n, adjust=False).mean().replace(0.0, np.nan)

    plus_di = 100.0 * (plus_dm.ewm(alpha=1.0 / n, adjust=False).mean() / atr)
    minus_di = 100.0 * (minus_dm.ewm(alpha=1.0 / n, adjust=False).mean() / atr)

    dx = (abs(plus_di - minus_di) / (plus_di + minus_di).replace(0.0, np.nan)) * 100.0
    adx = dx.ewm(alpha=1.0 / n, adjust=False).mean()

    adx_rising = adx.diff() > 0
    return adx, plus_di, minus_di, adx_rising


# -------------------- dynamic banlist / symbol scoring --------------------

def _state_get_symstats(state: dict) -> dict:
    return state.setdefault("sym_stats", {})

def _update_symbol_score_on_close(state: dict, cfg: AppConfig, symbol: str, pnl_usdt: float) -> None:
    sf_cfg = getattr(cfg.strategy, "symbol_filter", None)
    if not sf_cfg or not sf_cfg.enabled or not getattr(sf_cfg, "score", None) or not sf_cfg.score.enabled:
        return

    stats = _state_get_symstats(state)
    s = stats.get(symbol) or {
        "n": 0, "wins": 0, "losses": 0,
        "ema_wr": None, "ema_pf": None, "ema_pnl": None,
        "gw": 0.0, "gl": 0.0,
        "status": "active", "ban_until": None,
        "grace_left": 0,
        "last_update": None,
    }

    s["n"] += 1
    if pnl_usdt >= 0:
        s["wins"] += 1
        s["gw"] += pnl_usdt
    else:
        s["losses"] += 1
        s["gl"] += abs(pnl_usdt)

    inst_wr = 100.0 if pnl_usdt >= 0 else 0.0
    inst_pf = (abs(pnl_usdt) if pnl_usdt > 0 else 0.0) / (abs(pnl_usdt) if pnl_usdt < 0 else 0.0 or 1e-9)

    alpha = float(sf_cfg.score.ema_alpha)
    s["ema_wr"] = _ema_update(s.get("ema_wr"), inst_wr, alpha)
    s["ema_pf"] = _ema_update(s.get("ema_pf"), inst_pf, alpha)
    s["ema_pnl"] = _ema_update(s.get("ema_pnl"), float(pnl_usdt), alpha)
    s["last_update"] = pd.Timestamp.utcnow().isoformat()

    wl = set((sf_cfg.whitelist or []))
    if symbol in wl:
        s["status"] = "active"
        s["ban_until"] = None
        s["grace_left"] = 0
    else:
        nmin = int(sf_cfg.score.min_sample_trades)
        if s["n"] >= nmin:
            if (float(s["ema_wr"] or 0.0) < float(sf_cfg.score.block_below_win_rate_pct) or
                float(s["ema_pf"] or 1.0) < float(sf_cfg.score.pf_block_threshold) or
                float(s["ema_pnl"] or 0.0) < float(sf_cfg.score.pnl_block_threshold_usdt_per_trade)):
                until = (pd.Timestamp.utcnow() + pd.Timedelta(minutes=int(sf_cfg.score.ban_minutes))).isoformat()
                s["status"] = "banned"
                s["ban_until"] = until
                s["grace_left"] = int(sf_cfg.score.grace_trades_after_unban)
            else:
                if (float(s["ema_wr"] or 100.0) < float(sf_cfg.score.min_win_rate_pct) or
                    float(s["ema_pf"] or 1.0) < float(sf_cfg.score.pf_downweight_threshold)):
                    s["status"] = "downweighted"
                else:
                    s["status"] = "active"

    stats[symbol] = s
    state["sym_stats"] = stats

def _apply_symbol_filter_to_targets(state: dict, cfg: AppConfig, targets: pd.Series) -> pd.Series:
    sf_cfg = getattr(cfg.strategy, "symbol_filter", None)
    if not sf_cfg or not sf_cfg.enabled:
        return targets

    stats = _state_get_symstats(state)
    ban_static = set((sf_cfg.banlist or []))
    wl = set((sf_cfg.whitelist or []))
    down_factor = float(getattr(getattr(sf_cfg, "score", None), "downweight_factor", 0.6)) if getattr(getattr(sf_cfg, "score", None), "enabled", False) else 1.0

    out = targets.copy()
    now = pd.Timestamp.utcnow()
    for s in list(out.index):
        if s in ban_static and s not in wl:
            out.loc[s] = 0.0
            continue
        st = (stats.get(s) or {})
        status = st.get("status")
        ban_until = st.get("ban_until")
        if status == "banned" and ban_until:
            try:
                if pd.Timestamp(ban_until) > now and s not in wl:
                    out.loc[s] = 0.0
                    continue
                else:
                    st["status"] = "downweighted" if (st.get("grace_left", 0) or 0) > 0 else "active"
                    st["ban_until"] = None
                    stats[s] = st
            except Exception:
                pass

        if st.get("status") == "downweighted" and s not in wl:
            out.loc[s] = float(out.loc[s]) * down_factor

    state["sym_stats"] = stats
    return out


# -------------------- Fast SL/TP thread --------------------


class FastSLTPThread(threading.Thread):
    """
    Fast stop/TP management thread.

    New: Supports MA-ATR trailing stop when configured under cfg.risk.trailing_sl:
      risk:
        trailing_sl:
          enabled: true
          type: "ma_atr"
          ma_len: 34
          atr_length: 28
          multiplier: 1.5
          cooldown_bars: 0

    Fallback: if trailing_sl absent or type != "ma_atr", continues using the
    chandelier-style ATR trail (HH/LL ± trail_atr_mult × ATR) defined by legacy keys:
        risk.trailing_enabled, risk.trail_atr_mult, risk.atr_len, etc.
    """
    def __init__(self, ex: ExchangeWrapper, cfg: AppConfig, state: dict, dry: bool, stop_event: threading.Event):
        super().__init__(daemon=True)
        self.ex = ex
        self.cfg = cfg
        self.state = state
        self.dry = dry
        self.stop_event = stop_event

        self._last_ohlcv_ts: Dict[str, float] = {}
        self._last_closed_tail: Dict[str, pd.DataFrame] = {}
        self._atr_cache: Dict[str, float] = {}
        self._ema_cache: Dict[str, float] = {}   # NEW: for MA-ATR trailing

        self.state.setdefault("perpos", {})
        self.state.setdefault("cooldowns", {})
        self.state.setdefault("enter_bar_time", {})
        self.state.setdefault("locked_r", {})

        # General risk params
        self.stop_close_only: bool = bool(getattr(self.cfg.risk, "stop_on_close_only", True))
        self.stop_confirm_bars: int = int(getattr(self.cfg.risk, "stop_confirm_bars", 0))
        self.min_hold_minutes: int = int(getattr(self.cfg.risk, "min_hold_minutes", 0))
        self.catastrophic_mult: float = float(getattr(self.cfg.risk, "catastrophic_atr_mult", 3.5))
        self.stop_buffer_bps: float = float(getattr(self.cfg.risk, "stop_buffer_bps", 0.0))

        # Legacy trailing (chandelier/HHLL)-style
        self.trail_mult_base: float = float(getattr(self.cfg.risk, "trail_atr_mult", 0.0) or 0.0)

        # New trailing_sl block (optional)
        tsl = getattr(self.cfg.risk, "trailing_sl", None)
        self.ts_enabled = bool(tsl and getattr(tsl, "enabled", False))
        self.ts_type = (getattr(tsl, "type", "") or "").strip().lower() if tsl else ""
        if self.ts_type == "ma_atr":
            self.ts_ma_len = int(getattr(tsl, "ma_len", 34))
            self.ts_atr_len = int(getattr(tsl, "atr_length", 28))
            self.ts_mult   = float(getattr(tsl, "multiplier", 1.5))
            self.ts_cooldown_bars = int(getattr(tsl, "cooldown_bars", 0))
        else:
            # Fallback to legacy lengths
            self.ts_ma_len = int(getattr(self.cfg.risk, "ema_len_for_trail", 34))
            self.ts_atr_len = int(getattr(self.cfg.risk, "atr_len", 28))
            self.ts_mult = float(self.trail_mult_base or 0.0)
            self.ts_cooldown_bars = 0

        # No-progress and partial ladders
        np_cfg = getattr(self.cfg.risk, "no_progress", None)
        self.no_progress_enabled = bool(np_cfg and np_cfg.enabled)
        self.no_progress_min_minutes = int(np_cfg.min_minutes) if np_cfg else 0
        self.no_progress_min_rr = float(np_cfg.min_rr) if np_cfg else 0.0

        ladd = getattr(self.cfg.risk, "partial_ladders", None)
        self.ladd_enabled = bool(ladd and ladd.enabled)
        self.ladd_levels_base = list(getattr(ladd, "r_levels", []) or [])
        self.ladd_sizes = list(getattr(ladd, "sizes", []) or [])
        self.ladd_reduce_only = bool(getattr(ladd, "reduce_only", True))

        tu = getattr(self.cfg.risk, "trailing_unlocks", None)
        self.unlock_enabled = bool(tu and tu.enabled)
        self.unlock_triggers = list(getattr(tu, "triggers_r", []) or [])
        self.unlock_locks = list(getattr(tu, "lock_r", []) or [])

        rx = getattr(self.cfg.risk, "exit_on_regime_flip", None)
        self.exit_regime_enabled = bool(rx and rx.enabled)
        self.exit_regime_confirm = int(getattr(rx, "confirm_bars", 1) or 1)

    def _adaptive_scales(self, symbol: str, last_close: float) -> Tuple[float, float, float]:
        ad = getattr(self.cfg.risk, "adaptive", None)
        if not ad or not ad.enabled or last_close <= 0:
            return 1.0, 1.0, 1.0
        atr = float(self._atr_cache.get(symbol, 0.0) or 0.0)
        if atr <= 0:
            return 1.0, 1.0, 1.0
        atrp_bps = (atr / float(last_close)) * 10_000.0
        low = float(getattr(ad, "low_thr_bps", 40.0))
        high = float(getattr(ad, "high_thr_bps", 120.0))
        tier = "mid"
        if atrp_bps <= low:
            tier = "low"
        elif atrp_bps >= high:
            tier = "high"
        def _pick(obj, key, default=1.0):
            try:
                return float(getattr(obj, key).get(tier, default))
            except Exception:
                return default
        sl_scale = _pick(ad, "sl_scale", 1.0)
        trail_scale = _pick(ad, "trail_scale", 1.0)
        ladd_scale = _pick(ad, "ladder_r_scale", 1.0)
        return sl_scale, trail_scale, ladd_scale

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

        close_v = _safe_float(last_closed.get("close"))
        high_v = _safe_float(last_closed.get("high"))
        low_v  = _safe_float(last_closed.get("low"))
        atr_v  = _safe_float(atr_val)
        entry_v = _safe_float(entry_price, close_v)

        if (not isinstance(pinfo, dict)) or (int(pinfo.get("sign", 0) or 0) != sign):
            perpos[symbol] = {
                "sign": sign,
                "entry_price": float(entry_v),
                "entry_atr": float(atr_v),
                "trail_hh": float(high_v),
                "trail_ll": float(low_v),
                "partial_done": False,
                "max_rr": 0.0,
                "ladder_done": [False] * len(self.ladd_levels_base),
                "regime_bad_count": 0,
            }
            self.state.setdefault("enter_bar_time", {})[symbol] = pd.Timestamp.utcnow().isoformat()
            self.state.setdefault("locked_r", {})[symbol] = 0.0
        else:
            if sign > 0:
                pinfo["trail_hh"] = float(max(_safe_float(pinfo.get("trail_hh"), high_v), high_v))
            else:
                pinfo["trail_ll"] = float(min(_safe_float(pinfo.get("trail_ll"), low_v), low_v))

    def _compute_stop_px(self, symbol: str, last_closed: pd.Series) -> Tuple[Optional[float], Optional[float], float, float, int]:
        pinfo = self.state.get("perpos", {}).get(symbol) or {}
        sign = int(pinfo.get("sign", 0) or 0)
        entry_price = _safe_float(pinfo.get("entry_price"))
        entry_atr   = _safe_float(pinfo.get("entry_atr"))
        if entry_price is None or entry_atr is None or entry_atr <= 0 or sign == 0:
            return None, None, 0.0, 0.0, 0

        last_close = _safe_float(last_closed.get("close"), entry_price)
        sl_scale, trail_scale, ladd_scale = self._adaptive_scales(symbol, last_close or entry_price)

        atr_mult_sl_eff = float(getattr(self.cfg.risk, "atr_mult_sl", 2.0)) * sl_scale
        init_sl = entry_price - atr_mult_sl_eff * entry_atr if sign > 0 else entry_price + atr_mult_sl_eff * entry_atr

        cur_atr = float(self._atr_cache.get(symbol, entry_atr) or entry_atr)

        # Trailing stop computation (new MA-ATR vs legacy HH/LL)
        stop_px = init_sl
        if self.ts_enabled and self.ts_type == "ma_atr" and self.ts_mult > 0 and cur_atr > 0:
            ema_val = float(self._ema_cache.get(symbol, last_close) or last_close)
            trail = (ema_val - (self.ts_mult * trail_scale) * cur_atr) if sign > 0 else (ema_val + (self.ts_mult * trail_scale) * cur_atr)
            stop_px = max(init_sl, trail) if sign > 0 else min(init_sl, trail)
        else:
            # Legacy chandelier style (HH/LL ± k*ATR)
            trail_k_eff = float(self.trail_mult_base or 0.0) * trail_scale
            if trail_k_eff > 0 and cur_atr > 0:
                hh = _safe_float(pinfo.get("trail_hh"), _safe_float(last_closed.get("high")))
                ll = _safe_float(pinfo.get("trail_ll"), _safe_float(last_closed.get("low")))
                trail_sl = (hh - trail_k_eff * cur_atr) if sign > 0 else (ll + trail_k_eff * cur_atr)
                stop_px = max(init_sl, trail_sl) if sign > 0 else min(init_sl, trail_sl)

        # R-unit and breakeven/locks
        R_unit = float(entry_atr * atr_mult_sl_eff)
        be_after = float(getattr(self.cfg.risk, "breakeven_after_r", 0.0))
        if be_after > 0 and last_close is not None:
            if sign > 0 and last_close >= entry_price + be_after * R_unit:
                stop_px = max(stop_px, entry_price)
            elif sign < 0 and last_close <= entry_price - be_after * R_unit:
                stop_px = min(stop_px, entry_price)

        locked = float(self.state.get("locked_r", {}).get(symbol, 0.0) or 0.0)
        if self.unlock_enabled and self.unlock_triggers and self.unlock_locks:
            for trig, lock in zip(self.unlock_triggers, self.unlock_locks):
                trig_eff = float(trig) * (ladd_scale if ladd_scale else 1.0)
                lock_r = float(lock)
                if locked < lock_r:
                    if sign > 0 and last_close >= entry_price + trig_eff * R_unit:
                        stop_px = max(stop_px, entry_price + lock_r * R_unit)
                        locked = lock_r
                    elif sign < 0 and last_close <= entry_price - trig_eff * R_unit:
                        stop_px = min(stop_px, entry_price - lock_r * R_unit)
                        locked = lock_r
        else:
            for trig, lock in [(0.8, 0.0), (1.5, 0.5), (2.5, 1.2)]:
                trig_eff = trig * (ladd_scale if ladd_scale else 1.0)
                if locked < lock:
                    if sign > 0 and last_close >= entry_price + trig_eff * R_unit:
                        stop_px = max(stop_px, entry_price + lock * R_unit)
                        locked = lock
                    elif sign < 0 and last_close <= entry_price - trig_eff * R_unit:
                        stop_px = min(stop_px, entry_price - lock * R_unit)
                        locked = lock
        if locked > 0:
            self.state.setdefault("locked_r", {})[symbol] = locked

        buf = self.stop_buffer_bps / 10_000.0
        if buf > 0:
            stop_px = stop_px * (1.0 - buf) if sign > 0 else stop_px * (1.0 + buf)

        cat_px = entry_price - self.catastrophic_mult * entry_atr if sign > 0 else entry_price + self.catastrophic_mult * entry_atr
        return float(stop_px), float(cat_px), float(entry_price), float(R_unit), int(sign)

    def _partial_ladders(self, symbol: str, qty: float, rr_now: float, last_close: float):
        if not self.ladd_enabled or qty == 0:
            return
        pinfo = self.state.get("perpos", {}).get(symbol) or {}
        done = pinfo.get("ladder_done") or [False] * len(self.ladd_levels_base)

        _, _, ladd_scale = self._adaptive_scales(symbol, last_close)
        levels_eff = [float(r) * (ladd_scale if ladd_scale else 1.0) for r in self.ladd_levels_base]

        changed = False
        for i, (r, sz) in enumerate(zip(levels_eff, self.ladd_sizes)):
            if done[i]:
                continue
            if rr_now >= float(r):
                side = "sell" if qty > 0 else "buy"
                q = max(0.0, abs(qty) * float(sz))
                if q <= 0:
                    continue
                try:
                    if self.dry:
                        log.info(f"[DRY-LADDER] {symbol} {side} {q}")
                    else:
                        self.ex.create_order_safe(symbol, side, q, None, post_only=False, reduce_only=self.ladd_reduce_only)
                    done[i] = True
                    changed = True
                except Exception as e:
                    log.warning(f"Ladder TP error {symbol}: {e}")
        if changed:
            pinfo["ladder_done"] = done
            self.state.setdefault("perpos", {})[symbol] = pinfo

    def _place_exit(self, symbol: str, qty: float, reason: str, exit_px: Optional[float]):
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
            try:
                pinfo = (self.state.get("perpos", {}) or {}).get(symbol, {})
                ep = float(pinfo.get("entry_price") or 0.0)
                sign = int(pinfo.get("sign") or (1 if qty > 0 else -1))
                px = float(exit_px or ep)
                realized = (px - ep) * (q if sign > 0 else -q)
                _update_symbol_score_on_close(self.state, self.cfg, symbol, float(realized))
                _update_hour_stats_on_close(self.state, self.cfg, symbol, float(realized), (self.state.get("enter_bar_time", {}) or {}).get(symbol))
            except Exception:
                pass

            cdm = int(getattr(self.cfg.risk, "cooldown_minutes_after_stop", 0))
            if cdm > 0:
                until = (pd.Timestamp.utcnow() + pd.Timedelta(minutes=cdm)).isoformat()
                self.state.setdefault("cooldowns", {})[symbol] = until
            self.state.get("perpos", {}).pop(symbol, None)
            self.state.get("enter_bar_time", {}).pop(symbol, None)
            self.state.get("locked_r", {}).pop(symbol, None)

    def run(self):
        fast_s = int(getattr(self.cfg.risk, "fast_check_seconds", 0))
        if fast_s <= 0:
            return
        tf = getattr(self.cfg.risk, "stop_timeframe", "5m")
        # Use MA-ATR-specified ATR length when enabled, else legacy
        atr_len = int(self.ts_atr_len if (self.ts_enabled and self.ts_type == "ma_atr") else getattr(self.cfg.risk, "atr_len", 28))

        while not self.stop_event.is_set():
            try:
                time.sleep(max(1, fast_s))
                positions = self.ex.fetch_positions() or {}
                if not positions:
                    continue

                for sym, pdct in positions.items():
                    qty = float(pdct.get("net_qty") or 0.0)
                    if qty == 0.0:
                        self.state.get("perpos", {}).pop(sym, None)
                        self.state.get("enter_bar_time", {}).pop(sym, None)
                        self.state.get("locked_r", {}).pop(sym, None)
                        continue

                    nowts = float(time.time())
                    if nowts - float(self._last_ohlcv_ts.get(sym, 0.0)) > 55.0:
                        try:
                            raw = self.ex.fetch_ohlcv(sym, tf, limit=max(60, atr_len + 10, self.ts_ma_len + 10))
                            if not raw:
                                continue
                            df = pd.DataFrame(raw, columns=["ts","open","high","low","close","volume"])
                            df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
                            df.set_index("dt", inplace=True)
                            tail = df.tail(max(3, self.stop_confirm_bars + 2)).copy()
                            self._last_closed_tail[sym] = tail

                            # Cache ATR (Wilder RMA via signals.compute_atr)
                            self._atr_cache[sym] = float(_safe_float(compute_atr(df, n=atr_len, method="rma").iloc[-1], np.nan))

                            # Cache EMA for MA-ATR if enabled
                            if self.ts_enabled and self.ts_type == "ma_atr":
                                ema_ser = df["close"].ewm(span=int(self.ts_ma_len), adjust=False).mean()
                                self._ema_cache[sym] = float(_safe_float(ema_ser.iloc[-1], np.nan))

                            self._last_ohlcv_ts[sym] = nowts
                        except Exception as e:
                            log.debug(f"fast fetch_ohlcv {sym} failed: {e}")
                            continue

                    closed_tail = self._last_closed_tail.get(sym)
                    if closed_tail is None or closed_tail.empty:
                        continue
                    last_closed = closed_tail.iloc[-1]

                    entry_px = float(pdct.get("entryPrice") or last_closed["close"])
                    self._init_or_update_perpos(sym, qty, last_closed, self._atr_cache.get(sym, 0.0), entry_px)

                    # FAST exit on regime flip (optional)
                    if self.exit_regime_enabled and getattr(self.cfg.strategy, "regime_filter", None) and self.cfg.strategy.regime_filter.enabled:
                        try:
                            ser = closed_tail["close"].dropna()
                            ok = regime_ok(ser, int(self.cfg.strategy.regime_filter.ema_len),
                                           float(self.cfg.strategy.regime_filter.slope_min_bps_per_day),
                                           use_abs=bool(getattr(self.cfg.strategy.regime_filter, "use_abs", False)))
                        except Exception:
                            ok = True
                        pinfo = self.state["perpos"].get(sym, {})
                        if ok:
                            pinfo["regime_bad_count"] = 0
                        else:
                            pinfo["regime_bad_count"] = int(pinfo.get("regime_bad_count", 0)) + 1
                        self.state["perpos"][sym] = pinfo
                        if pinfo.get("regime_bad_count", 0) >= max(1, self.exit_regime_confirm):
                            self._place_exit(sym, qty, "REGIME-FLIP", float(last_closed["close"]))
                            continue

                    pinfo = self.state["perpos"].get(sym, {})
                    ep = float(pinfo.get("entry_price") or entry_px)
                    atr0 = float(pinfo.get("entry_atr") or self._atr_cache.get(sym, 0.0) or 1.0)
                    last_c = float(last_closed["close"])

                    sl_scale, _, _ = self._adaptive_scales(sym, last_c)
                    R_unit = atr0 * float(getattr(self.cfg.risk, "atr_mult_sl", 2.0)) * sl_scale
                    rr_now = abs(last_c - ep) / (R_unit if R_unit > 1e-12 else 1.0)
                    pinfo["max_rr"] = float(max(float(pinfo.get("max_rr", 0.0)), rr_now))
                    self.state["perpos"][sym] = pinfo

                    self._partial_ladders(sym, qty, rr_now, last_c)

                    max_hours = int(getattr(self.cfg.risk, "max_hours_in_trade", 0) or 0)
                    if max_hours > 0:
                        enter_iso = self.state.get("enter_bar_time", {}).get(sym)
                        if enter_iso:
                            age_h = (pd.Timestamp.utcnow() - pd.Timestamp(enter_iso)).total_seconds() / 3600.0
                            if age_h >= max_hours:
                                self._place_exit(sym, qty, "TIME-EXIT", last_c)
                                continue

                    np_cfg = getattr(self.cfg.risk, "no_progress", None)
                    if np_cfg and np_cfg.enabled and self._minutes_held(sym) >= float(np_cfg.min_minutes or 0):
                        if float(pinfo.get("max_rr", 0.0)) < float(np_cfg.min_rr or 0.0):
                            self._place_exit(sym, qty, "NO-PROGRESS", last_c)
                            continue

                    # Compute stops
                    normal_px, cat_px, ep, R_unit_eff, sign = self._compute_stop_px(sym, last_closed)
                    if sign == 0:
                        continue

                    allow_normal = (self.min_hold_minutes <= 0) or (self._minutes_held(sym) >= self.min_hold_minutes)

                    hit_normal = False
                    hit_cat = False
                    if normal_px is not None and allow_normal:
                        if self.stop_confirm_bars <= 0:
                            if (sign > 0 and last_closed["close"] <= normal_px) or (sign < 0 and last_closed["close"] >= normal_px):
                                hit_normal = True
                        else:
                            tail = closed_tail.tail(self.stop_confirm_bars)
                            if sign > 0:
                                hit_normal = bool((tail["close"] <= normal_px).all())
                            else:
                                hit_normal = bool((tail["close"] >= normal_px).all())

                    if cat_px is not None:
                        if (sign > 0 and last_closed["low"] <= cat_px) or (sign < 0 and last_closed["high"] >= cat_px):
                            hit_cat = True

                    # Additional real-time trailing guard (legacy path only)
                    if (not (self.ts_enabled and self.ts_type == "ma_atr")) and bool(getattr(self.cfg.risk, "trailing_enabled", True)) and self.trail_mult_base > 0 and not hit_cat and not hit_normal:
                        if sign > 0:
                            sl_scale, trail_scale, _ = self._adaptive_scales(sym, last_c)
                            cur_atr = self._atr_cache.get(sym, atr0) or atr0
                            trail_k_eff = self.trail_mult_base * trail_scale
                            trail = float(max(pinfo.get("trail_hh", last_c), last_c)) - trail_k_eff * cur_atr
                            if last_closed["close"] <= trail:
                                hit_normal = True
                        else:
                            sl_scale, trail_scale, _ = self._adaptive_scales(sym, last_c)
                            cur_atr = self._atr_cache.get(sym, atr0) or atr0
                            trail_k_eff = self.trail_mult_base * trail_scale
                            trail = float(min(pinfo.get("trail_ll", last_c), last_c)) + trail_k_eff * cur_atr
                            if last_closed["close"] >= trail:
                                hit_normal = True

                    if hit_cat:
                        self._place_exit(sym, qty, "CAT-STOP", last_c)
                        continue
                    if hit_normal:
                        self._place_exit(sym, qty, "STOP", last_c)
                        continue

            except Exception as e:
                log.debug(f"Fast thread loop error: {e}")



# -------------------- startup reconcile --------------------

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
                "max_rr": float(state.get("perpos", {}).get(sym, {}).get("max_rr", 0.0) or 0.0),
                "ladder_done": state.get("perpos", {}).get(sym, {}).get("ladder_done", []),
                "regime_bad_count": int(state.get("perpos", {}).get(sym, {}).get("regime_bad_count", 0) or 0),
            }
            state.setdefault("enter_bar_time", {})[sym] = state.get("enter_bar_time", {}).get(sym) or pd.Timestamp.utcnow().isoformat()
            state.setdefault("locked_r", {})[sym] = float(state.get("locked_r", {}).get(sym, 0.0) or 0.0)
            live_syms.append(sym)
        except Exception as e:
            log.warning(f"Startup reconcile: OHLCV/ATR failed for {sym}: {e}")

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


# -------------------- order maintenance --------------------

def _reconcile_open_orders(
    ex: ExchangeWrapper,
    cfg: AppConfig,
    targets: pd.Series,
    positions: dict,
    tickers: dict,
    eligible_syms: set,
    state: dict,
    now_ts: float,
):
    st = getattr(cfg.execution, "stale_orders", None)
    if not st or not getattr(st, "enabled", False):
        return

    last_ts = float(state.get("last_stale_cleanup_ts", 0.0) or 0.0)
    if (now_ts - last_ts) < int(getattr(st, "cleanup_interval_sec", 60)):
        return

    max_age = int(getattr(st, "max_age_sec", 180))
    far_bps = float(getattr(st, "reprice_if_far_bps", 15.0))
    maker_ttl = int(getattr(cfg.execution, "maker_ttl_secs", 0) or 0)

    cancel_if_not_targeted = bool(getattr(st, "cancel_if_not_targeted", True))
    keep_reduce_only = bool(getattr(st, "keep_reduce_only", True))

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
        desired_side = "buy" if tgt_w > 0 else ("sell" if tgt_w < 0 else None)
        still_eligible = (s in eligible_syms)

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

        to_cancel = []
        for od in odlist:
            if keep_reduce_only and (od.get("reduceOnly") or od.get("reduce_only")):
                continue

            od_side = (od.get("side") or "").lower()
            if desired_side and od_side and od_side != desired_side:
                to_cancel.append(od.get("id"))
                continue

            age_bad = False
            created = od.get("timestamp") or od.get("time") or od.get("created")
            if created:
                try:
                    created_s = float(created) / (1000.0 if float(created) > 10_000_000_000 else 1.0)
                    age_bad = (now_ts - created_s) > max_age
                except Exception:
                    age_bad = False

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

        if to_cancel:
            canceled = 0
            for oid in to_cancel:
                if not oid:
                    continue
                try:
                    ex.cancel_order(oid, s)
                    canceled += 1
                except Exception:
                    try:
                        ex.cancel_all_orders(s)
                        canceled = len(to_cancel)
                        break
                    except Exception:
                        pass
            if canceled:
                log.info(f"[CLEANUP] {s}: canceled {canceled} stale/opposite/away orders for reprice.")

    state["last_stale_cleanup_ts"] = now_ts


# -------------------- main loop --------------------

def run_live(cfg: AppConfig, dry: bool = False):
    log.info("Starting live loop (mode=%s)", "DRY" if dry else "LIVE")
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
    state.setdefault("sym_stats", {})

    day_start_equity = float(state.get("day_start_equity", 0.0))
    day_high_equity = float(state.get("day_high_equity", 0.0))
    disable_until_ts = float(state.get("disable_until_ts", 0.0))
    soft_block_until_ts = float((state or {}).get("soft_block_until_ts") or 0.0)
    current_stepdown_tier = int(state.get("risk_stepdown_tier", 0))

    ex = ExchangeWrapper(cfg.exchange)

    if getattr(cfg.execution, "cancel_open_orders_on_start", False):
        try:
            ex.cancel_all_orders(None)
            log.info("Startup safety: cancel_all_orders executed (per config flag).")
        except Exception as e:
            log.warning(f"Startup safety: cancel_all_orders failed (continuing): {e}")

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

            # Day rollover (UTC) and equity high tracking
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

            if eq > 0 and eq > day_high_equity:
                day_high_equity = eq
                state["day_high_equity"] = day_high_equity
                write_json(state_path, state)

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
                    soft_block_until_ts = float((state or {}).get("soft_block_until_ts") or 0.0)
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

            # Microstructure pre-gate (entries only)
            keep = list(bars.keys())
            if getattr(cfg.execution, "microstructure", None) and getattr(cfg.execution.microstructure, "enabled", False):
                tkr_map = ex.fetch_tickers(list(bars.keys())) or {}
                keep = []
                for s in list(bars.keys()):
                    tkr = tkr_map.get(s, {}) or {}
                    ob = ex.fetch_order_book(s, limit=10)
                    if _micro_ok(cfg, tkr, ob):
                        keep.append(s)
                bars = {k: bars[k] for k in keep if k in bars}
                removed = [s for s in tkr_map.keys() if s not in keep] if isinstance(tkr_map, dict) else []
                log.info(f"[GATE] Microstructure pre-gate: kept={len(keep)} removed={len(removed)} enabled={getattr(cfg.execution.microstructure, 'enabled', False)}")

            # ADX filter
            eligible_syms = list(bars.keys())
            adx_cfg = getattr(cfg.strategy, "adx_filter", None)
            if adx_cfg and adx_cfg.enabled:
                blocked = 0
                keep2 = []
                for s, df in bars.items():
                    try:
                        adx = _compute_adx(df[["high","low","close"]], int(adx_cfg.len)).iloc[-1]
                        if float(adx) >= float(adx_cfg.min_adx):
                            keep2.append(s)
                        else:
                            blocked += 1
                    except Exception:
                        keep2.append(s)
                eligible_syms = keep2
                if blocked > 0:
                    log.info(f"ADX gate: {blocked}/{len(bars)} symbols blocked this cycle.")

            # Time-of-day whitelist
            tod_cfg = getattr(cfg.strategy, "time_of_day_whitelist", None)
            if tod_cfg and tod_cfg.enabled:
                hour_now = int(pd.Timestamp.utcnow().hour)
                allowed_hours = set((tod_cfg.fixed_hours or []))
                if not allowed_hours:
                    hs = state.get("hour_stats", {})
                    allowed = []
                    for h, rec in hs.items():
                        n = int(rec.get("n") or 0)
                        ema_usdt = float(rec.get("ema_usdt") or 0.0)
                        if n >= int(tod_cfg.min_trades_per_hour or 0):
                            if bool(tod_cfg.use_ema):
                                if ema_usdt >= float(getattr(tod_cfg, "threshold_bps", 0.0)):
                                    allowed.append(int(h))
                            else:
                                if float(rec.get("sum") or 0.0) >= 0.0:
                                    allowed.append(int(h))
                    if len(allowed) >= int(tod_cfg.min_hours_allowed or 0):
                        allowed_hours = set(allowed)
                if allowed_hours and hour_now not in allowed_hours:
                    log.info(f"Time-of-day gate: hour {hour_now} not in allowed {sorted(list(allowed_hours))}; pausing new entries this cycle.")
                    eligible_syms = []
                if not eligible_syms:
                    log.info("Time gates blocked entries; staying flat.")
                    time.sleep(max(1, int(getattr(cfg.execution, "poll_seconds", 5))))
                    continue

            # Regime filter
            closes = pd.concat({s: bars[s]["close"] for s in bars}, axis=1).dropna(how="all")
            if cfg.strategy.regime_filter.enabled:
                ema_len = int(cfg.strategy.regime_filter.ema_len)
                thr = float(cfg.strategy.regime_filter.slope_min_bps_per_day)
                use_abs = bool(getattr(cfg.strategy.regime_filter, "use_abs", False))
                blocked = 0
                keep3 = []
                for s in list(eligible_syms):
                    ser = closes[s].dropna()
                    try:
                        ok = regime_ok(ser, ema_len, thr, use_abs=use_abs)
                    except Exception:
                        ok = True
                    if ok:
                        keep3.append(s)
                    else:
                        blocked += 1
                eligible_syms = keep3
                if blocked > 0:
                    log.info(f"Regime gate: {blocked}/{len(closes.columns)} symbols blocked this cycle.")
                if len(eligible_syms) == 0:
                    log.info("Regime gate blocked all symbols; staying flat.")
                    time.sleep(max(1, int(getattr(cfg.execution, "poll_seconds", 5))))
                    continue

            # Majors regime overall gate/downweight
            mj = getattr(cfg.strategy, "majors_regime", None)
            majors_ok, okc = _majors_gate(ex, cfg, cfg.exchange.timeframe, cfg.exchange.candles_limit)
            majors_downweight = 1.0
            log.info(f"[GATE] Majors regime: enabled={getattr(cfg.strategy, 'majors_regime', None) and getattr(cfg.strategy.majors_regime, 'enabled', False)} ok={majors_ok} action={getattr(cfg.strategy.majors_regime, 'action', 'block')} ok_count={okc}")
            if mj and getattr(mj, "enabled", False):
                if not majors_ok:
                    if getattr(mj, "action", "block") == "block":
                        log.info("Majors regime gate blocked entries this cycle.")
                        eligible_syms = []
                    else:
                        majors_downweight = float(getattr(mj, "downweight_factor", 0.6))

            closes_used = closes[eligible_syms]

            # 2) Risk step-down with drawdown
            step_cfg = getattr(cfg, "risk_stepdown", None)
            gl_mult = 1.0
            max_new_mult = 1.0
            if step_cfg and step_cfg.enabled:
                ref = day_high_equity if bool(getattr(cfg.risk, "use_trailing_killswitch", True)) else day_start_equity
                dd = _dd_pct(ref, eq)
                tiers = list(getattr(step_cfg, "dd_levels_pct", []) or [])
                gl_m = list(getattr(step_cfg, "gross_leverage_multipliers", []) or [])
                new_m = list(getattr(step_cfg, "max_new_positions_multipliers", []) or [])
                tier = 0
                for i, lvl in enumerate(tiers, start=1):
                    if dd >= float(lvl):
                        tier = i
                if tier > current_stepdown_tier:
                    current_stepdown_tier = tier
                elif tier < current_stepdown_tier:
                    rec = float(getattr(step_cfg, "recover_hysteresis_pct", 0.0))
                    need = max(0.0, dd - rec)
                    if dd <= need:
                        current_stepdown_tier = tier
                state["risk_stepdown_tier"] = current_stepdown_tier
                if current_stepdown_tier > 0:
                    idx = current_stepdown_tier - 1
                    gl_mult = float(gl_m[idx]) if idx < len(gl_m) else gl_mult
                    max_new_mult = float(new_m[idx]) if idx < len(new_m) else max_new_mult

            # 3) Targets (router or legacy)
            funding_map: Dict[str, float] = {}
            if getattr(cfg.strategy.funding_tilt, "enabled", False):
                try:
                    funding_map = ex.fetch_funding_rates(list(bars.keys())) or {}
                except Exception:
                    pass

            router_mode_raw = getattr(getattr(cfg, "strategy", object()), "mode", None)
            router_mode = str(router_mode_raw or "auto").strip().lower()
            log.info(f"[ROUTER] config strategy.mode={router_mode!r} (raw={router_mode_raw!r})")
            use_router = router_mode in ("auto", "xsmom", "tsmom")

            try:
                if use_router:
                    targets = build_targets_auto(closes_used, cfg)  # regime_router handles object/dict cfg
                    chosen_mode = decide_mode(cfg, closes_used)
                    log.info(f"[ROUTER] signal_mode={chosen_mode}, gross={float(targets.abs().sum()):.4f}")
                else:
                    raise RuntimeError("Router disabled")
            except Exception as e:
                log.info(f"[ROUTER] disabled or failed ({e}); using legacy build_targets pipeline.")
                if getattr(closes_used, 'empty', True) or getattr(closes_used, 'shape', (0,0))[1] == 0:
                    targets = pd.Series(0.0, index=closes.columns)
                else:
                    targets = build_targets(
                        closes_used,
                        getattr(cfg.strategy, "lookbacks", [1, 6, 24]),
                        getattr(cfg.strategy, "lookback_weights", [1.0, 1.0, 1.0]),
                        getattr(cfg.strategy, "vol_lookback", 72),
                        k_min=getattr(cfg.strategy, "k_min", 2),
                        k_max=getattr(cfg.strategy, "k_max", 6),
                        market_neutral=getattr(cfg.strategy, "market_neutral", True),
                        gross_leverage=getattr(cfg.strategy, "gross_leverage", 1.10),
                        max_weight_per_asset=getattr(cfg.strategy, "max_weight_per_asset", 0.14),
                        dynamic_k_fn=dynamic_k if bool(getattr(cfg.strategy, "use_dynamic_k", False)) else None,
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
                        signal_power=float(getattr(cfg.strategy, "signal_power", 1.35)),
                    ).reindex(closes.columns).fillna(0.0)
            else:
                # If router path succeeded, apply stepdown multiplier after targets
                try:
                    if gl_mult != 1.0:
                        targets *= float(gl_mult)
                except Exception:
                    pass

            # Reindex to full universe (blocked symbols → 0)
            targets = targets.reindex(closes.columns).fillna(0.0)
            try:
                longs = int((targets > 0).sum()); shorts = int((targets < 0).sum()); gross = float(targets.abs().sum())
                log.info(f"[SUMMARY] sizing pipeline: longs={longs} shorts={shorts} gross={gross:.4f}")
            except Exception:
                pass

            # --- MTF confirmation gate ---
            if bool(getattr(cfg.strategy, "require_mtf_alignment", True)):
                tf2 = getattr(cfg.strategy, "confirmation_timeframe", "4h")
                lb = int(getattr(cfg.strategy, "confirmation_lookback", 6))
                keep4 = []
                for s in list(targets.index):
                    try:
                        raw = ex.fetch_ohlcv(s, timeframe=tf2, limit=max(60, lb+2))
                        if not raw:
                            keep4.append(s); continue
                        dfc = pd.DataFrame(raw, columns=["ts","open","high","low","close","volume"])
                        dfc["dt"] = pd.to_datetime(dfc["ts"], unit="ms", utc=True); dfc.set_index("dt", inplace=True)
                        pc = closes_used[s].dropna() if s in closes_used.columns else pd.Series(dtype=float)
                        hc = dfc["close"].dropna()
                        if len(pc) > lb and len(hc) > lb:
                            p_ret = float(pc.iloc[-1] / pc.iloc[-lb] - 1.0)
                            c_ret = float(hc.iloc[-1] / hc.iloc[-lb] - 1.0)
                            if (targets.loc[s] > 0 and c_ret >= 0) or (targets.loc[s] < 0 and c_ret <= 0):
                                keep4.append(s)
                        else:
                            keep4.append(s)
                    except Exception:
                        keep4.append(s)
                targets = targets.reindex(keep4).fillna(0.0)
                log.info(f"[GATE] MTF confirm: kept={len(keep4)}")

            # --- Kelly per-symbol scaling (tiny) ---
            if getattr(cfg.strategy, "kelly", None) and getattr(cfg.strategy.kelly, "enabled", False):
                sym_stats = state.get("sym_stats")
                if isinstance(sym_stats, dict) and len(sym_stats) > 0:
                    targets = apply_kelly_scaling(targets, sym_stats, cfg.strategy.kelly)
                    nz = targets.replace(0.0, float('nan')).abs()
                    log.info(f"[SIZING] KELLY scaling applied: nonzero={int(nz.count())} gross={float(nz.sum()):.4f}")

            # --- Majors downweight (if action=downweight and majors weak) ---
            try:
                if majors_downweight < 0.999:
                    targets *= float(majors_downweight)
            except Exception:
                pass

            # --- Time-of-day boost ---
            tod = getattr(cfg.strategy, "time_of_day_whitelist", None)
            if tod and getattr(tod, "enabled", False) and bool(getattr(tod, "boost_good_hours", False)):
                cur_h = pd.Timestamp.utcnow().hour
                good = getattr(tod, "fixed_good_hours", None)
                if good and cur_h in good:
                    bf = float(getattr(tod, "boost_factor", 1.0))
                    if bf != 1.0:
                        targets *= bf
                    log.info(f"[SIZING] ToD boost applied: hour={cur_h} factor={bf}")

            # --- Cooldowns to zero weight while active ---
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
                                    state["cooldowns"].pop(s, None)
                            except Exception:
                                pass
            except Exception:
                pass

            # --- Dynamic symbol filter (banlist/downsizing) ---
            targets = _apply_symbol_filter_to_targets(state, cfg, targets)

            # >>> CARRY/BASIS SLEEVE: build after momentum gates, before portfolio scaler
            try:
                carry_cfg = parse_carry_cfg(_cfg_to_dict(cfg))
                if carry_cfg.enabled:
                    w_mom = targets.copy()

                    # Defaults for micro gates if wrapper lacks helpers
                    try:
                        spread_bps_map = ex.get_spread_bps_map(list(closes.columns)) if hasattr(ex, 'get_spread_bps_map') else {s: 5.0 for s in closes.columns}
                    except Exception:
                        spread_bps_map = {s: 5.0 for s in closes.columns}
                    try:
                        depth_usd_map = ex.get_depth_usd_map(list(closes.columns)) if hasattr(ex, 'get_depth_usd_map') else {s: 5000.0 for s in closes.columns}
                    except Exception:
                        depth_usd_map = {s: 5000.0 for s in closes.columns}
                    try:
                        percentile_30d_map = ex.get_funding_percentile_30d(list(closes.columns)) if hasattr(ex, 'get_funding_percentile_30d') else {s: 0.5 for s in closes.columns}
                    except Exception:
                        percentile_30d_map = {s: 0.5 for s in closes.columns}

                    # Funding-carry sleeve (perps)
                    w_carry = pd.Series(0.0, index=w_mom.index, name='carry')
                    if carry_cfg.funding.enabled:
                        w_fc, meta_fc = build_funding_carry_weights(
                            ex=ex,
                            universe=list(closes.columns),
                            equity=float(eq or 0.0),
                            cfg=carry_cfg,
                            spread_bps_map=spread_bps_map,
                            depth_usd_map=depth_usd_map,
                            percentile_30d_map=percentile_30d_map,
                        )
                        w_carry = w_carry.add(w_fc.reindex(w_carry.index).fillna(0.0), fill_value=0.0)
                        kept_fc = [k for k,v in meta_fc.items() if v.get('chosen')]
                        log.info(f"[CARRY] funding: chosen={len(kept_fc)} {kept_fc[:6]}...")

                    # Basis cash-and-carry (requires spot+dated futures quotes)
                    if carry_cfg.basis.enabled and hasattr(ex, 'get_dated_futures_quotes'):
                        try:
                            futs_quotes = ex.get_dated_futures_quotes(list(closes.columns))  # {sym: {'F','S','dte_days'}}
                        except Exception:
                            futs_quotes = {}
                        if futs_quotes:
                            w_bc, meta_bc = build_basis_carry_weights(
                                ex=ex,
                                universe_spot=list(closes.columns),
                                equity=float(eq or 0.0),
                                cfg=carry_cfg,
                                futs_quotes=futs_quotes,
                            )
                            w_carry = w_carry.add(w_bc.reindex(w_carry.index).fillna(0.0), fill_value=0.0)
                            kept_bc = [k for k,v in meta_bc.items() if v.get('chosen')]
                            log.info(f"[CARRY] basis: chosen={len(kept_bc)} {kept_bc[:6]}...")

                    # Blend sleeves by budget fraction into combined targets
                    targets = combine_sleeves(
                        w_momentum=w_mom,
                        w_carry=w_carry,
                        carry_budget_frac=float(getattr(carry_cfg, 'budget_frac', 0.35)),
                        total_gross_leverage=float(getattr(cfg.strategy, 'gross_leverage', 1.2)),
                        per_asset_cap=float(getattr(cfg.strategy, 'max_weight_per_asset', 0.14)),
                    )
                    try:
                        g = float(targets.abs().sum())
                        log.info(f"[CARRY] combined gross after blend: {g:.4f}")
                    except Exception:
                        pass
            except Exception as e:
                log.warning(f"[CARRY] sleeve integration failed (non-fatal): {e}")

            # === Portfolio-level scaler: VolTarget × DD Stepdown × Fractional-Kelly ===
            def _portfolio_returns(closes_df: pd.DataFrame, weights: pd.Series, lookback: int = 20) -> pd.Series:
                rets = closes_df.pct_change().dropna()
                w = weights.reindex(closes_df.columns).fillna(0.0)
                pr = (rets @ w).tail(lookback)
                return pr

            vt_target_bps = float(getattr(getattr(cfg, "sizing", object()), "portfolio_target_daily_vol_bps", 60.0))
            vt_lb = int(getattr(getattr(cfg, "sizing", object()), "portfolio_vol_lookback", 20))
            vt_lo = float(getattr(getattr(cfg, "sizing", object()), "portfolio_vol_min_mult", 0.6))
            vt_hi = float(getattr(getattr(cfg, "sizing", object()), "portfolio_vol_max_mult", 1.3))

            try:
                pr = _portfolio_returns(closes_used, targets, lookback=vt_lb)
                rv = float(pr.std()) if len(pr) > 2 else 0.0
                tgt = vt_target_bps / 1e4
                s_vol = (tgt / rv) if rv > 1e-9 else 1.0
                s_vol = float(max(vt_lo, min(vt_hi, s_vol)))
            except Exception:
                s_vol = 1.0

            # Drawdown stepdown on equity vs trailing high
            def _dd_abs(ref_high: float, noweq: float) -> float:
                if ref_high <= 0 or noweq <= 0: return 0.0
                return max(0.0, (ref_high - noweq) / ref_high)

            dd2, dd4, dd6 = 0.02, 0.04, 0.06
            m2, m4, m6 = 0.90, 0.75, 0.60
            ref_h = float(state.get("day_high_equity") or eq or 0.0)
            eq_now2 = float(eq or ref_h)
            dd_abs = _dd_abs(ref_h, eq_now2)
            s_dd = 1.0
            if dd_abs >= dd6: s_dd = m6
            elif dd_abs >= dd4: s_dd = m4
            elif dd_abs >= dd2: s_dd = m2

            # Fractional-Kelly overlay using portfolio SR proxy (very small)
            try:
                mu = float(pr.mean()) if len(pr) else 0.0
                sd = float(pr.std()) if len(pr) else 0.0
                sr = (mu / sd) if sd > 1e-9 else 0.0
                kelly_tilt = 1.0 + 0.20 * max(-1.0, min(1.0, sr))  # ±20% at |SR|=1
                s_kelly = float(max(0.8, min(1.2, kelly_tilt)))
            except Exception:
                s_kelly = 1.0

            gross_mult = float(s_vol * s_dd * s_kelly)
            targets *= gross_mult
            log.info(f"[SIZING] portfolio scaler: vol_mult={s_vol:.2f} dd_mult={s_dd:.2f} kelly_mult={s_kelly:.2f} → gross_mult={gross_mult:.2f}")

            # --- HARDENING: re-apply per-asset cap AFTER all multipliers ---
            try:
                max_w = float(getattr(getattr(cfg, "sizing", object()), "max_weight_per_asset", 0.14))
                if max_w > 0:
                    targets = targets.clip(lower=-max_w, upper=max_w)
            except Exception:
                pass

            
            # --- RISK-BASED NOTIONAL CAP (ATR stop sizing) ---
            # Limit each symbol's weight so loss at initial SL ≤ risk_per_trade × equity.
            try:
                atr_len_rb = int(getattr(cfg.risk, "atr_len", 28))
                atr_mult_sl_rb = float(getattr(cfg.risk, "atr_mult_sl", 2.0))
                risk_per_trade_rb = float(getattr(cfg.risk, "risk_per_trade", 0.0))
                if risk_per_trade_rb > 0 and atr_mult_sl_rb > 0 and eq and eq > 0:
                    for _s in list(targets.index):
                        try:
                            df_b = bars.get(_s)
                            if df_b is None or df_b.empty:
                                continue
                            close_last = float(df_b["close"].iloc[-1])
                            if close_last <= 0:
                                continue
                            atr_val = float(compute_atr(df_b, n=atr_len_rb, method="rma").iloc[-1])
                            stop_pct = (atr_val * atr_mult_sl_rb) / max(close_last, 1e-12)
                            if stop_pct <= 0:
                                continue
                            cap_w_risk = risk_per_trade_rb / stop_pct
                            w_old = float(targets.loc[_s])
                            targets.loc[_s] = float((cap_w_risk if w_old >= 0 else -cap_w_risk)) if abs(w_old) > cap_w_risk else w_old
                        except Exception:
                            continue
            except Exception as _e_rb:
                log.debug(f"risk-based cap skipped: {_e_rb}")

            # --- HARDENING: enforce per-symbol notional cap in weights \(if set\) ---
            try:
                cap_usdt = float(getattr(getattr(cfg, "liquidity", object()), "notional_cap_usdt", 0.0) or 0.0)
                if cap_usdt > 0 and eq > 0:
                    cap_w = cap_usdt / eq
                    targets = targets.apply(lambda w: np.sign(w) * min(abs(w), cap_w))
            except Exception:
                pass

            # 4) Liquidity caps (ADV %, per-symbol notional)
            tickers = ex.fetch_tickers(list(targets.index))
            eq = ex.get_equity_usdt()
            targets = apply_liquidity_caps(
                targets,
                equity_usdt=eq,
                tickers=tickers or {},
                adv_cap_pct=cfg.liquidity.adv_cap_pct,
                notional_cap_usdt=cfg.liquidity.notional_cap_usdt,
            )

            # 4.5) Stale/open order cleanup BEFORE creating new ones
            positions = ex.fetch_positions() or {}
            _reconcile_open_orders(
                ex=ex,
                cfg=cfg,
                targets=targets,
                positions=positions,
                tickers=tickers or {},
                eligible_syms=set(eligible_syms),
                state=state,
                now_ts=time.time(),
            )

            # Build open orders map to avoid double-placing while pending
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

            # 5) Orders
            min_notional = float(getattr(cfg.execution, "min_notional_per_order_usdt", 5.0))
            min_delta_bps = float(getattr(cfg.execution, "min_rebalance_delta_bps", 1.0))
            per_sym_cool = int(getattr(getattr(cfg.execution, "throttle", object()), "min_seconds_between_entries_per_symbol", 12))

            open_symbols = [s for s, p in positions.items() if not _is_new_position(p)]
            base_slots = int(getattr(cfg.strategy.entry_throttle, "max_open_positions", 999))
            remaining_slots = max(0, int(round(base_slots * max_new_mult)) - len(open_symbols))

            base_new_per_cycle = int(getattr(cfg.strategy.entry_throttle, "max_new_positions_per_cycle", 999))
            new_entries_cap = min(int(round(base_new_per_cycle * max_new_mult)), remaining_slots)

            order_syms = list(targets.index)
            try:
                order_syms = sorted(order_syms, key=lambda s: abs(float(targets.loc[s])), reverse=True)
            except Exception:
                pass
            if allow_new_entries and new_entries_cap < len(order_syms):
                existing = [s for s in order_syms if s in open_symbols]
                fresh = [s for s in order_syms if s not in open_symbols][:max(0, new_entries_cap)]
                order_syms = existing + fresh

            # Preview top intended notionals (sanity)
            try:
                preview = []
                for s in order_syms:
                    notional = abs(float(targets.loc[s])) * float(eq)
                    if notional > 0:
                        preview.append((s, float(targets.loc[s]), notional))
                preview.sort(key=lambda x: x[2], reverse=True)
                if preview:
                    msg = ", ".join(f"{s}:{n:.2f}USDT(w={w:+.3f})" for s, w, n in preview[:5])
                    log.info(f"[PREVIEW] top intended notionals: {msg}")
            except Exception:
                pass

            created = 0
            last_trade_ts = state.setdefault("last_trade_ts", {})

            for s in order_syms:
                tgt_w = float(targets.loc[s])
                tkr = (tickers or {}).get(s, {}) or {}
                mid = _mid_price(tkr)
                if mid is None or mid <= 0:
                    continue

                # Per-symbol cooldown to avoid spam
                lt = float(last_trade_ts.get(s, 0.0) or 0.0)
                if per_sym_cool > 0 and (time.time() - lt) < per_sym_cool:
                    continue

                notional = abs(tgt_w) * eq
                if notional < min_notional:
                    continue

                cur_qty = float(positions.get(s, {}).get("net_qty") or 0.0)
                desired_contracts = tgt_w * eq / mid

                # Decide side vs current qty
                raw_delta = desired_contracts - cur_qty
                if abs(raw_delta) <= 0:
                    continue
                side = "buy" if raw_delta > 0 else "sell"

                # === NO-PYRAMID GUARD ===
                pyr = getattr(getattr(cfg.execution, "pyramiding", object()), "enabled", False)
                if not pyr:
                    # If same direction and we would be INCREASING absolute size → skip (no add)
                    if (cur_qty > 0 and desired_contracts > cur_qty) or (cur_qty < 0 and desired_contracts < cur_qty):
                        continue
                else:
                    rr_gate = float(getattr(getattr(cfg.execution, "pyramiding", object()), "allow_when_rr_ge", 0.0) or 0.0)
                    if rr_gate > 0 and ((cur_qty > 0 and desired_contracts > cur_qty) or (cur_qty < 0 and desired_contracts < cur_qty)):
                        locked_r = float(state.get("locked_r", {}).get(s, 0.0) or 0.0)
                        if locked_r < rr_gate:
                            continue
                # === END NO-PYRAMID ===

                # Subtract pending same-side qty to avoid duplicate placement every cycle
                pend = _sum_pending_same_side(open_by_sym.get(s), side)
                abs_delta = max(0.0, abs(raw_delta) - pend)
                if abs_delta <= 0:
                    continue
                delta = math.copysign(abs_delta, raw_delta)

                # Absolute delta-notional floor (prevents tiny dribbler orders)
                delta_notional = abs(delta) * mid
                if delta_notional < min_notional:
                    continue


                # Require a minimal rebalance change in bps of current notional (if any)
                if min_delta_bps > 0 and eq > 0:
                    cur_notional = abs(cur_qty) * mid
                    if cur_notional > 0:
                        step_bps = (abs(delta) * mid) / cur_notional * 10_000.0
                        if step_bps < min_delta_bps:
                            continue

                # Quantize to exchange constraints
                specs = _get_symbol_specs(ex, s, state)
                step = float(specs.get("amount_step") or 0.0)
                min_qty = float(specs.get("amount_min") or 0.0)
                min_cost = float(specs.get("min_notional") or 0.0)
                integer_amt = bool(specs.get("integer_amount", False))

                q_to_send = _quantize_amount(abs(delta), step, integer_amt)

                # Respect min qty and min notional (Bybit precision errors)
                if min_qty > 0 and q_to_send < min_qty:
                    continue
                if min_cost > 0 and (q_to_send * mid) < min_cost:
                    continue
                if q_to_send <= 0:
                    continue

                # Price (limit w/ dynamic offset) or market
                px = None
                if getattr(cfg.execution, "order_type", "limit") == "limit":
                    base_off = float(getattr(cfg.execution, "price_offset_bps", 2.0))
                    dyn = getattr(cfg.execution, "dynamic_offset", None)
                    off_bps = base_off
                    if dyn and getattr(dyn, "enabled", False):
                        sp = _spread_bps(tkr) or 0.0
                        off_bps = min(float(dyn.max_offset_bps), float(dyn.base_bps) + float(dyn.per_spread_coeff) * sp)
                    px = mid * (1.0 - off_bps / 10_000.0) if side == "buy" else mid * (1.0 + off_bps / 10_000.0)

                # Spread guard
                sg = getattr(cfg.execution, "spread_guard", None)
                if sg and getattr(sg, "enabled", False):
                    sp = _spread_bps(tkr) or 0.0
                    if sp > float(getattr(sg, "max_spread_bps", 15.0)) and bool(getattr(sg, "skip_if_wider", True)):
                        log.info(f"[SPREAD-GUARD] Skip {s}: spread {sp:.2f}bps > max {sg.max_spread_bps}bps")
                        continue

                try:
                    if dry:
                        log.info(f"[DRY] {s}: {side} {q_to_send} @ {px or 'mkt'} (tgt_w={tgt_w:+.4f}, pend={pend:.4f})")
                    else:
                        post_only = bool(getattr(cfg.execution, "post_only", True))
                        ex.create_order_safe(s, side, q_to_send, px, post_only=post_only, reduce_only=False)
                        created += 1
                        last_trade_ts[s] = time.time()
                except Exception as e:
                    msg = str(e)
                    # Learn min qty from error strings like:
                    # "amount ... must be greater than minimum amount precision of 100"
                    mobj = re.search(r"(minimum .*?(?:amount|qty).*?of\s+)([0-9]+(?:\.[0-9]+)?)", msg, re.I)
                    if mobj:
                        try:
                            learned = float(mobj.group(2))
                            state.setdefault("min_qty_cache", {})
                            old = float(state["min_qty_cache"].get(s, 0.0) or 0.0)
                            if learned > old:
                                state["min_qty_cache"][s] = learned
                                write_json(getattr(cfg.paths, "state_path", "state/state.json"), state)
                                log.info(f"[LEARN] Updated {s} min qty to {learned} from exchange error.")
                        except Exception:
                            pass
                    log.warning(f"order {s} failed: {e}")

            if created:
                log.info(f"Placed {created} orders this cycle.")

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