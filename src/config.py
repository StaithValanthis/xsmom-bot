# config.py — v1.6
# - Pydantic schema for all sections
# - Backward-compatible load_config(yaml_path) for main.py
from __future__ import annotations

from typing import List, Optional, Tuple, Dict, Any
from pydantic import BaseModel, Field, ValidationError
import yaml
import os


# -----------------------------
# Section models
# -----------------------------

class ExchangeCfg(BaseModel):
    id: str
    account_type: str
    quote: str
    only_perps: bool = True
    unified_margin: bool = True
    testnet: bool = False

    max_symbols: int = 20
    min_usd_volume_24h: float = 0.0
    min_price: float = 0.0
    timeframe: str = "1h"
    candles_limit: int = 1500


class RegimeFilterCfg(BaseModel):
    enabled: bool = False
    ema_len: int = 200
    slope_min_bps_per_day: float = 0.0
    use_abs: bool = False


class FundingTiltCfg(BaseModel):
    enabled: bool = False
    weight: float = 0.0


class DiversifyCfg(BaseModel):
    enabled: bool = False
    corr_lookback: int = 48
    max_pair_corr: float = 0.9


class VolTargetCfg(BaseModel):
    enabled: bool = False
    target_daily_vol_bps: float = 0.0
    min_scale: float = 0.5
    max_scale: float = 2.0


class EntryThrottleCfg(BaseModel):
    max_new_positions_per_cycle: int = 999
    max_open_positions: int = 999
    per_symbol_trade_cooldown_min: int = 0
    min_entry_zscore: float = 0.0


class SoftKillCfg(BaseModel):
    enabled: bool = False
    soft_daily_loss_pct: float = 0.0
    resume_after_minutes: int = 0


class SoftWinLockCfg(BaseModel):
    enabled: bool = False
    arm_after_r: float = 0.0
    lock_r: float = 0.0
    cooldown_minutes: int = 0

class AdxFilterCfg(BaseModel):
    enabled: bool = False
    len: int = 14
    min_adx: float = 20.0
class SymbolScoreCfg(BaseModel):
    enabled: bool = True
    ema_alpha: float = 0.2
    min_sample_trades: int = 8
    block_below_win_rate_pct: float = 48.0
    pf_block_threshold: float = 1.0
    pf_warn_threshold: float = 1.2
class SymbolFilterCfg(BaseModel):
    enabled: bool = True
    whitelist: list[str] | None = None
    banlist: list[str] | None = None
    ban_minutes_after_loss: int = 0
    score: SymbolScoreCfg = SymbolScoreCfg()
class TimeOfDayWhitelistCfg(BaseModel):
    enabled: bool = False
    # Only trade during hours with positive EMA PnL (or above threshold) computed from paper/live results
    use_ema: bool = True
    ema_alpha: float = 0.2
    min_trades_per_hour: int = 5
    min_hours_allowed: int = 6
    # If use_ema=True, require ema_pnl_bps >= threshold_bps; else mean pnl >= 0
    threshold_bps: float = 0.0
    # Optional: hard allowlist of UTC hours (0-23) that overrides stats if provided
    fixed_hours: list[int] | None = None
    downweight_factor: float = 0.6


class StrategyCfg(BaseModel):
    lookbacks: List[int]
    lookback_weights: List[float]
    vol_lookback: int

    k_min: int
    k_max: int

    market_neutral: bool
    gross_leverage: float
    max_weight_per_asset: float

    regime_filter: RegimeFilterCfg = RegimeFilterCfg()
    funding_tilt: FundingTiltCfg = FundingTiltCfg()

    entry_zscore_min: float = 0.0

    diversify: DiversifyCfg = DiversifyCfg()
    vol_target: VolTargetCfg = VolTargetCfg()
    entry_throttle: EntryThrottleCfg = EntryThrottleCfg()
    soft_kill: SoftKillCfg = SoftKillCfg()
    soft_win_lock: SoftWinLockCfg = SoftWinLockCfg()
    adx_filter: AdxFilterCfg = AdxFilterCfg()
    symbol_filter: SymbolFilterCfg = SymbolFilterCfg()
    time_of_day_whitelist: TimeOfDayWhitelistCfg = TimeOfDayWhitelistCfg()







class LiquidityCfg(BaseModel):
    adv_cap_pct: float
    notional_cap_usdt: float


class StaleOrdersCfg(BaseModel):
    enabled: bool = False
    cleanup_interval_sec: int = 60
    max_age_sec: int = 180
    reprice_if_far_bps: float = 15.0
    cancel_if_not_targeted: bool = True
    keep_reduce_only: bool = True


class ExecutionCfg(BaseModel):
    reload_positions_on_start: bool = True
    order_type: str = "limit"
    post_only: bool = True
    price_offset_bps: float = 0.0
    slippage_bps_guard: float = 25.0
    set_leverage: int = 1

    rebalance_minute: int = 1
    poll_seconds: int = 10
    align_after_funding_minutes: int = 0
    funding_hours_utc: List[int] = Field(default_factory=list)

    min_notional_per_order_usdt: float = 5.0
    min_rebalance_delta_bps: float = 1.0

    cancel_open_orders_on_start: bool = False

    # NEW
    stale_orders: StaleOrdersCfg = StaleOrdersCfg()


class RiskCfg(BaseModel):
    atr_len: int = 28
    atr_mult_sl: float = 2.0
    atr_mult_tp: float = 0.0
    use_tp: bool = False

    fast_check_seconds: int = 2
    stop_timeframe: str = "5m"

    trailing_enabled: bool = False
    trail_atr_mult: float = 0.0
    breakeven_after_r: float = 0.0

    stop_on_close_only: bool = False
    stop_confirm_bars: int = 0              # NEW
    min_hold_minutes: int = 0               # NEW
    catastrophic_atr_mult: float = 3.5      # NEW

    stop_buffer_bps: float = 0.0
    cooldown_minutes_after_stop: int = 0

    partial_tp_enabled: bool = False
    partial_tp_r: float = 0.0
    partial_tp_size: float = 0.0

    max_daily_loss_pct: float = 5.0
    trade_disable_minutes: int = 0
    use_trailing_killswitch: bool = False
    min_close_pnl_pct: float = 0.0

    max_hours_in_trade: int = 0

    # Profit protection extras (used by live.py v1.6+)
    profit_lock_steps: Optional[List[Tuple[float, float]]] = None  # e.g. [[0.8,0.0],[1.5,0.5],[2.5,1.2]]
    breakeven_extra_bps: float = 0.0
    trail_after_partial_mult: float = 0.0
    age_tighten: Optional[Dict[str, float]] = None  # e.g. {"12h":0.7,"24h":0.5}


class PathsCfg(BaseModel):
    state_path: str
    logs_dir: str


class LoggingCfg(BaseModel):
    level: str = "INFO"
    file_max_mb: int = 20
    file_backups: int = 5


class AppConfig(BaseModel):
    exchange: ExchangeCfg
    strategy: StrategyCfg
    liquidity: LiquidityCfg
    execution: ExecutionCfg
    risk: RiskCfg
    paths: PathsCfg
    logging: LoggingCfg


# -----------------------------
# Loader
# -----------------------------

def _merge_defaults(raw: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensures optional nested sections exist so pydantic gets sane defaults.
    """
    raw = dict(raw or {})

    raw.setdefault("strategy", {})
    raw["strategy"].setdefault("regime_filter", {})
    raw["strategy"].setdefault("funding_tilt", {})
    raw["strategy"].setdefault("diversify", {})
    raw["strategy"].setdefault("vol_target", {})
    raw["strategy"].setdefault("entry_throttle", {})
    raw["strategy"].setdefault("soft_kill", {})
    raw["strategy"].setdefault("soft_win_lock", {})
    raw["strategy"].setdefault("adx_filter", {})
    raw["strategy"].setdefault("symbol_filter", {})
    raw["strategy"]["symbol_filter"].setdefault("banlist", [])
    raw["strategy"].setdefault("time_of_day_whitelist", {})

    raw.setdefault("execution", {})
    raw["execution"].setdefault("stale_orders", {})

    raw.setdefault("risk", {})

    return raw


def load_config(yaml_path: str) -> AppConfig:
    """
    Backward-compatible entrypoint used by main.py
    """
    path = os.path.abspath(yaml_path)
    if not os.path.exists(path):
        raise FileNotFoundError(f"Config YAML not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    data = _merge_defaults(data)

    try:
        cfg = AppConfig(**data)
    except ValidationError as e:
        # Provide a concise error that still helps
        raise RuntimeError(f"Invalid config.yaml: {e}")

    return cfg

