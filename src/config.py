# config.py — v2.0 (2025-09-02)
from __future__ import annotations

from typing import List, Optional, Tuple, Dict, Any
from pydantic import BaseModel, Field, ValidationError
import yaml, os

# -----------------------------
# Exchange
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

# -----------------------------
# Strategy filters & extras
# -----------------------------
class HurstCfg(BaseModel):
    enabled: bool = False
    lags: List[int] = Field(default_factory=lambda: [2,4,8,16,32])
    min_h: float = 0.5

class RegimeFilterCfg(BaseModel):
    enabled: bool = False
    ema_len: int = 200
    slope_min_bps_per_day: float = 0.0
    use_abs: bool = False
    hurst: HurstCfg = HurstCfg()

class FundingTiltCfg(BaseModel):
    enabled: bool = False
    weight: float = 0.0

class FundingTrimCfg(BaseModel):
    enabled: bool = False
    threshold_bps: float = 0.0
    slope_per_bps: float = 0.0
    max_reduction: float = 0.0

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
    require_rising: bool = False
    use_di_alignment: bool = True
    min_di_separation: float = 0.0
    di_hysteresis_bps: float = 0.0

class SymbolScoreCfg(BaseModel):
    enabled: bool = True
    min_sample_trades: int = 8
    ema_alpha: float = 0.2
    min_win_rate_pct: float = 38.0
    pf_downweight_threshold: float = 1.0
    downweight_factor: float = 0.6
    block_below_win_rate_pct: float = 38.0
    pf_block_threshold: float = 0.90
    pnl_block_threshold_usdt_per_trade: float = -0.1
    ban_minutes: int = 1440
    grace_trades_after_unban: int = 3
    decay_days: int = 14
    pf_warn_threshold: float = 1.2

class SymbolFilterCfg(BaseModel):
    enabled: bool = True
    whitelist: List[str] | None = None
    banlist: List[str] | None = None
    ban_minutes_after_loss: int = 0
    score: SymbolScoreCfg = SymbolScoreCfg()

class TimeOfDayWhitelistCfg(BaseModel):
    enabled: bool = False
    use_ema: bool = True
    ema_alpha: float = 0.2
    min_trades_per_hour: int = 5
    min_hours_allowed: int = 6
    threshold_bps: float = 0.0  # interpreted as USDT/trade in live.py
    fixed_hours: List[int] | None = None
    downweight_factor: float = 0.6
    # NEW optional boosters
    boost_good_hours: bool = False
    boost_factor: float = 1.0
    fixed_good_hours: List[int] | None = None

class LiquidityCapsCfg(BaseModel):
    enabled: bool = False
    max_weight_low_liq: float = 0.02
    symbols_low_liq: List[str] = Field(default_factory=list)

# NEW: Majors regime guard
class MajorsRegimeCfg(BaseModel):
    enabled: bool = False
    majors: List[str] = Field(default_factory=lambda: ["BTC/USDT:USDT","ETH/USDT:USDT"])
    ema_len: int = 200
    slope_bps_per_day: float = 1.0
    action: str = "block"   # or "downweight"
    downweight_factor: float = 0.6

# NEW: Kelly-style conviction scaling
class KellyCfg(BaseModel):
    enabled: bool = False
    base_frac: float = 0.5
    half_kelly: bool = True
    min_scale: float = 0.5
    max_scale: float = 1.6

class StrategyCfg(BaseModel):
    signal_power: float = 1.35
    lookbacks: List[int] = Field(default_factory=lambda: [12,24,48,96])
    lookback_weights: List[float] = Field(default_factory=lambda: [0.4,0.3,0.2,0.1])
    vol_lookback: int = 96

    k_min: int = 2
    k_max: int = 4

    market_neutral: bool = True
    gross_leverage: float = 1.5
    max_weight_per_asset: float = 0.2

    # Filters
    adx_filter: AdxFilterCfg = AdxFilterCfg()
    symbol_filter: SymbolFilterCfg = SymbolFilterCfg()
    regime_filter: RegimeFilterCfg = RegimeFilterCfg()
    time_of_day_whitelist: TimeOfDayWhitelistCfg = TimeOfDayWhitelistCfg()

    # Sizing extras
    funding_tilt: FundingTiltCfg = FundingTiltCfg()
    funding_trim: FundingTrimCfg = FundingTrimCfg()
    diversify: DiversifyCfg = DiversifyCfg()
    vol_target: VolTargetCfg = VolTargetCfg()
    entry_throttle: EntryThrottleCfg = EntryThrottleCfg()

    # Soft controls
    soft_kill: SoftKillCfg = SoftKillCfg()
    soft_win_lock: SoftWinLockCfg = SoftWinLockCfg()

    # Optional caps for known illiquid tickers
    liquidity_caps: LiquidityCapsCfg = LiquidityCapsCfg()

    # Entry threshold
    entry_zscore_min: float = 0.0

    # NEW: multi-timeframe confirmation
    confirmation_timeframe: str = "4h"
    confirmation_lookback: int = 6
    require_mtf_alignment: bool = True

    # NEW: majors regime & kelly sections
    majors_regime: MajorsRegimeCfg = MajorsRegimeCfg()
    kelly: KellyCfg = KellyCfg()

# -----------------------------
# Liquidity
# -----------------------------
class LiquidityCfg(BaseModel):
    adv_cap_pct: float = 0.0
    notional_cap_usdt: float = 0.0

# -----------------------------
# Execution
# -----------------------------
class SpreadGuardCfg(BaseModel):
    enabled: bool = False
    max_spread_bps: float = 15.0
    skip_if_wider: bool = True

class DynamicOffsetCfg(BaseModel):
    enabled: bool = False
    base_bps: float = 3.0
    per_spread_coeff: float = 0.5
    max_offset_bps: float = 20.0

class MicrostructureCfg(BaseModel):
    enabled: bool = False
    min_obi: float = 0.15
    max_spread_bps: float = 8.0

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

    stale_orders: StaleOrdersCfg = StaleOrdersCfg()
    spread_guard: SpreadGuardCfg = SpreadGuardCfg()
    dynamic_offset: DynamicOffsetCfg = DynamicOffsetCfg()
    microstructure: MicrostructureCfg = MicrostructureCfg()

    # If True, when computed order size is below the exchange minimum amount/notional,
    # bump the size up to the minimum instead of skipping the trade.
    bump_to_exchange_min: bool = True

# -----------------------------
# Risk
# -----------------------------
class TrailingUnlocksCfg(BaseModel):
    enabled: bool = False
    triggers_r: List[float] = Field(default_factory=list)
    lock_r: List[float] = Field(default_factory=list)

class ExitOnRegimeFlipCfg(BaseModel):
    enabled: bool = False
    confirm_bars: int = 1

class AdaptiveRiskCfg(BaseModel):
    enabled: bool = False
    low_thr_bps: float = 40.0
    high_thr_bps: float = 120.0
    sl_scale: Dict[str, float] = Field(default_factory=lambda: {"low":1.4, "mid":1.0, "high":0.8})
    trail_scale: Dict[str, float] = Field(default_factory=lambda: {"low":1.2, "mid":1.0, "high":0.8})
    ladder_r_scale: Dict[str, float] = Field(default_factory=lambda: {"low":1.1, "mid":1.0, "high":0.9})

class PartialLaddersCfg(BaseModel):
    enabled: bool = False
    r_levels: List[float] = Field(default_factory=list)
    sizes: List[float] = Field(default_factory=list)
    reduce_only: bool = True

class ProfitLockCfg(BaseModel):
    enabled: bool = False
    triggers_r: List[float] = Field(default_factory=list)
    lock_to_r: List[float] = Field(default_factory=list)

class NoProgressCfg(BaseModel):
    enabled: bool = False
    min_minutes: int = 20
    min_rr: float = 0.3
    min_close_pnl_pct: float = 0.0
    tiers: Dict[str, List[float]] | None = None

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
    stop_confirm_bars: int = 0
    min_hold_minutes: int = 0
    catastrophic_atr_mult: float = 3.5

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

    # Extended
    trailing_unlocks: TrailingUnlocksCfg = TrailingUnlocksCfg()
    exit_on_regime_flip: ExitOnRegimeFlipCfg = ExitOnRegimeFlipCfg()
    adaptive: AdaptiveRiskCfg = AdaptiveRiskCfg()
    partial_ladders: PartialLaddersCfg = PartialLaddersCfg()
    no_progress: NoProgressCfg = NoProgressCfg()
    profit_lock: ProfitLockCfg = ProfitLockCfg()

    # Legacy compat
    profit_lock_steps: Optional[List[Tuple[float, float]]] = None
    breakeven_extra_bps: float = 0.0
    trail_after_partial_mult: float = 0.0
    age_tighten: Optional[Dict[str, float]] = None

# -----------------------------
# Paths, logging, costs
# -----------------------------
class PathsCfg(BaseModel):
    state_path: str
    logs_dir: str
    metrics_path: Optional[str] = None

class LoggingCfg(BaseModel):
    level: str = "INFO"
    file_max_mb: int = 20
    file_backups: int = 5

class CostsCfg(BaseModel):
    maker_fee_bps: float = 1.0
    taker_fee_bps: float = 5.0
    slippage_bps: float = 2.0
    borrow_bps: float = 0.0
    maker_fill_ratio: float = 0.5

class AppConfig(BaseModel):
    exchange: ExchangeCfg
    strategy: StrategyCfg
    liquidity: LiquidityCfg
    execution: ExecutionCfg
    risk: RiskCfg
    paths: PathsCfg
    logging: LoggingCfg
    costs: CostsCfg = CostsCfg()

# -----------------------------
# Loader
# -----------------------------
def _merge_defaults(raw: Dict[str, Any]) -> Dict[str, Any]:
    raw = dict(raw or {})

    raw.setdefault("strategy", {})
    raw["strategy"].setdefault("regime_filter", {})
    raw["strategy"].setdefault("funding_tilt", {})
    raw["strategy"].setdefault("funding_trim", {})
    raw["strategy"].setdefault("diversify", {})
    raw["strategy"].setdefault("vol_target", {})
    raw["strategy"].setdefault("entry_throttle", {})
    raw["strategy"].setdefault("soft_kill", {})
    raw["strategy"].setdefault("soft_win_lock", {})
    raw["strategy"].setdefault("adx_filter", {})
    raw["strategy"].setdefault("symbol_filter", {})
    raw["strategy"]["symbol_filter"].setdefault("score", {})
    raw["strategy"]["symbol_filter"].setdefault("banlist", [])
    raw["strategy"].setdefault("time_of_day_whitelist", {})
    raw["strategy"].setdefault("liquidity_caps", {})
    # new
    raw["strategy"].setdefault("majors_regime", {})
    raw["strategy"].setdefault("kelly", {})
    raw["strategy"].setdefault("confirmation_timeframe", "4h")
    raw["strategy"].setdefault("confirmation_lookback", 6)
    raw["strategy"].setdefault("require_mtf_alignment", True)

    raw.setdefault("execution", {})
    raw["execution"].setdefault("stale_orders", {})
    raw["execution"].setdefault("spread_guard", {})
    raw["execution"].setdefault("dynamic_offset", {})
    raw["execution"].setdefault("microstructure", {})
    raw["execution"].setdefault("bump_to_exchange_min", True)

    raw.setdefault("risk", {})
    raw["risk"].setdefault("trailing_unlocks", {})
    raw["risk"].setdefault("exit_on_regime_flip", {})
    raw["risk"].setdefault("adaptive", {})
    raw["risk"].setdefault("partial_ladders", {})
    raw["risk"].setdefault("no_progress", {})
    raw["risk"].setdefault("profit_lock", {})

    raw.setdefault("paths", {})
    raw["paths"].setdefault("metrics_path", None)

    raw.setdefault("costs", {})

    return raw

def load_config(yaml_path: str) -> AppConfig:
    path = os.path.abspath(yaml_path)
    if not os.path.exists(path):
        raise FileNotFoundError(f"Config YAML not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    data = _merge_defaults(data)
    # === PATCH: costs back-compat ===
    costs = (data.get("costs") or {})
    if "maker_fee_bps" not in costs and "maker_bps" in costs:
        costs["maker_fee_bps"] = costs.get("maker_bps")
    if "taker_fee_bps" not in costs and "taker_bps" in costs:
        costs["taker_fee_bps"] = costs.get("taker_bps")
    data["costs"] = costs
    

    try:
        cfg = AppConfig(**data)
    except ValidationError as e:
        raise RuntimeError(f"Invalid config.yaml: {e}")

    return cfg
