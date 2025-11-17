# src/anti_churn.py
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, Tuple, Optional, List
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

@dataclass
class AntiChurnCfg:
    enabled: bool = True
    cooldown_minutes: int = 20
    after_stop_cooldown_minutes: int = 60
    max_trades_per_lookback: int = 2
    lookback_minutes: int = 60
    streak_pause_after_losses: int = 3
    streak_pause_minutes: int = 180
    reentry_zscore_max: float = 0.25
    reentry_atr_breakout_mult: float = 0.5
    reentry_use_any: bool = True
    hysteresis_z: float = 0.10
    min_bar_separation: int = 1

    @classmethod
    def from_cfg(cls, root: dict) -> "AntiChurnCfg":
        ac = (root or {}).get("risk", {}).get("anti_churn", {}) or {}
        rr = ac.get("reentry_requires_reset", {}) or {}
        return cls(
            enabled=bool(ac.get("enabled", True)),
            cooldown_minutes=int(ac.get("cooldown_minutes", 20)),
            after_stop_cooldown_minutes=int(ac.get("after_stop_cooldown_minutes", 60)),
            max_trades_per_lookback=int(ac.get("max_trades_per_lookback", 2)),
            lookback_minutes=int(ac.get("lookback_minutes", 60)),
            streak_pause_after_losses=int(ac.get("streak_pause_after_losses", 3)),
            streak_pause_minutes=int(ac.get("streak_pause_minutes", 180)),
            reentry_zscore_max=float(rr.get("zscore_max", 0.25)),
            reentry_atr_breakout_mult=float(rr.get("atr_breakout_mult", 0.5)),
            reentry_use_any=bool(rr.get("use_any", True)),
            hysteresis_z=float(ac.get("hysteresis_z", 0.10)),
            min_bar_separation=int(ac.get("min_bar_separation", 1)),
        )

@dataclass
class LastTrade:
    t: datetime                    # UTC
    side: str                      # "long"/"short"
    entry_price: float
    exit_price: Optional[float] = None
    pnl: Optional[float] = None
    exit_reason: Optional[str] = None
    bar_index: Optional[int] = None

@dataclass
class SymbolState:
    trades: List[LastTrade] = field(default_factory=list)
    cons_losses: int = 0
    paused_until: Optional[datetime] = None
    last_z_for_side: Dict[str, float] = field(default_factory=dict)  # hysteresis state

class ReEntryGuard:
    def __init__(self, cfg: AntiChurnCfg, state_dir: str = "/opt/xsmom-bot/state"):
        self.cfg = cfg
        self.state_path = Path(state_dir) / "anti_churn.json"
        self.state: Dict[str, SymbolState] = {}
        self._load()

    def _load(self):
        try:
            if self.state_path.exists() and self.state_path.is_file():
                raw = json.loads(self.state_path.read_text())
                for sym, s in raw.items():
                    st = SymbolState(
                        trades=[LastTrade(
                            t=datetime.fromisoformat(tr["t"]),
                            side=tr["side"],
                            entry_price=tr["entry_price"],
                            exit_price=tr.get("exit_price"),
                            pnl=tr.get("pnl"),
                            exit_reason=tr.get("exit_reason"),
                            bar_index=tr.get("bar_index"),
                        ) for tr in s.get("trades", [])],
                        cons_losses=int(s.get("cons_losses", 0)),
                        paused_until=datetime.fromisoformat(s["paused_until"]) if s.get("paused_until") else None,
                        last_z_for_side=s.get("last_z_for_side", {}),
                    )
                    self.state[sym] = st
        except Exception:
            self.state = {}

    def _save(self):
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            out = {}
            for sym, st in self.state.items():
                out[sym] = {
                    "trades": [{
                        "t": lt.t.replace(tzinfo=timezone.utc).isoformat(),
                        "side": lt.side,
                        "entry_price": lt.entry_price,
                        "exit_price": lt.exit_price,
                        "pnl": lt.pnl,
                        "exit_reason": lt.exit_reason,
                        "bar_index": lt.bar_index,
                    } for lt in st.trades[-200:]],  # cap history
                    "cons_losses": st.cons_losses,
                    "paused_until": st.paused_until.replace(tzinfo=timezone.utc).isoformat() if st.paused_until else None,
                    "last_z_for_side": st.last_z_for_side,
                }
            self.state_path.write_text(json.dumps(out))
        except Exception:
            pass

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def record_fill(self, symbol: str, side: str, entry_price: float, bar_index: Optional[int] = None):
        st = self.state.setdefault(symbol, SymbolState())
        st.trades.append(LastTrade(t=self._now(), side=side, entry_price=entry_price, bar_index=bar_index))
        self._save()

    def record_exit(self, symbol: str, pnl: float, exit_reason: str, exit_price: float | None = None):
        st = self.state.setdefault(symbol, SymbolState())
        if st.trades:
            st.trades[-1].exit_price = exit_price
            st.trades[-1].pnl = pnl
            st.trades[-1].exit_reason = exit_reason
        # update loss streak + pause if needed
        st.cons_losses = (st.cons_losses + 1) if pnl < 0 else 0
        if st.cons_losses >= self.cfg.streak_pause_after_losses:
            st.paused_until = self._now() + timedelta(minutes=self.cfg.streak_pause_minutes)
            st.cons_losses = 0  # reset after pausing
        self._save()

    def update_hysteresis(self, symbol: str, side: str, z: float | None):
        if z is None:
            return
        st = self.state.setdefault(symbol, SymbolState())
        st.last_z_for_side[side] = float(z)
        self._save()

    def allow_new_entry(self,
                        symbol: str,
                        side: str,
                        z_now: Optional[float],
                        atr: Optional[float],
                        price_now: Optional[float],
                        bar_index: Optional[int]) -> Tuple[bool, str]:
        if not self.cfg.enabled:
            return True, "ok:disabled"

        now = self._now()
        st = self.state.setdefault(symbol, SymbolState())

        # Pause window due to streak of losses
        if st.paused_until and now < st.paused_until:
            return False, f"paused_until={st.paused_until.isoformat()}"

        # Bar separation
        if self.cfg.min_bar_separation and st.trades and bar_index is not None:
            last = st.trades[-1]
            if last.bar_index is not None and bar_index - int(last.bar_index) < self.cfg.min_bar_separation:
                return False, "min_bar_separation"

        # Cooldowns
        for lt in reversed(st.trades):
            if lt.side != side:
                continue
            mins = self.cfg.cooldown_minutes
            if lt.exit_reason in ("stop", "stop_loss", "sl"):
                mins = max(mins, self.cfg.after_stop_cooldown_minutes)
            if now - lt.t < timedelta(minutes=mins):
                return False, f"cooldown {mins}m"

        # Rate-cap (max trades over lookback)
        lookback_cut = now - timedelta(minutes=self.cfg.lookback_minutes)
        recent = [lt for lt in st.trades if lt.t >= lookback_cut]
        if len(recent) >= self.cfg.max_trades_per_lookback:
            return False, f"rate_cap {len(recent)}/{self.cfg.max_trades_per_lookback}@{self.cfg.lookback_minutes}m"

        # Re-entry reset: require z reset OR price breakout from last extreme
        if st.trades:
            last = st.trades[-1]
            need_any = self.cfg.reentry_use_any
            ok_z = (z_now is not None and abs(float(z_now)) <= self.cfg.reentry_zscore_max)
            ok_px = False
            if atr and price_now and last.entry_price:
                thresh = self.cfg.reentry_atr_breakout_mult * float(atr)
                if side == "long":
                    ok_px = price_now >= last.entry_price + thresh
                else:
                    ok_px = price_now <= last.entry_price - thresh
            conds = (ok_z, ok_px)
            if need_any and not any(conds):
                return False, "reentry_reset_needed"
            if not need_any and not all(conds):
                return False, "reentry_reset_needed_all"

        # Hysteresis: if we recently exited long at z ~ +band, don’t re-arm until z falls back by hysteresis_z
        if z_now is not None:
            last_z = st.last_z_for_side.get(side)
            if last_z is not None:
                if side == "long" and float(z_now) > last_z - self.cfg.hysteresis_z:
                    return False, "hysteresis"
                if side == "short" and float(z_now) < last_z + self.cfg.hysteresis_z:
                    return False, "hysteresis"

        return True, "ok"
