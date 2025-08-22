# v1.4.1 – 2025-08-22
from __future__ import annotations
from typing import List
from pydantic import BaseModel, Field
import yaml

class ExchangeCfg(BaseModel):
    id: str = "bybit"
    account_type: str = "swap"
    quote: str = "USDT"
    only_perps: bool = True
    unified_margin: bool = True
    testnet: bool = False

    max_symbols: int = 36
    min_usd_volume_24h: float = 30_000_000.0
    min_price: float = 0.03
    timeframe: str = "1h"
    candles_limit: int = 1500

class RegimeFilterCfg(BaseModel):
    enabled: bool = True
    ema_len: int = 200
    slope_min_bps_per_day: float = 2.0
    use_abs: bool = True

class FundingTiltCfg(BaseModel):
    enabled: bool = True
    weight: float = 0.15

class DiversifyCfg(BaseModel):
    enabled: bool = True
    corr_lookback: int = 96
    max_pair_corr: float = 0.60

class VolTargetCfg(BaseModel):
    enabled: bool = True
    target_daily_vol_bps: float = 60.0
    min_scale: float = 0.6
    max_scale: float = 1.2

class EntryThrottleCfg(BaseModel):
    max_new_positions_per_cycle: int = 2
    max_open_positions: int = 8
    per_symbol_trade_cooldown_min: int = 60
    min_entry_zscore: float = 0.6

class SoftKillCfg(BaseModel):
    enabled: bool = True
    soft_daily_loss_pct: float = 1.5
    resume_after_minutes: int = 240

class StrategyCfg(BaseModel):
    lookbacks: List[int] = Field(default_factory=lambda: [1, 6, 24])
    lookback_weights: List[float] = Field(default_factory=lambda: [1.0, 1.0, 1.0])
    vol_lookback: int = 72

    k_min: int = 2
    k_max: int = 6

    market_neutral: bool = True
    gross_leverage: float = 1.10
    max_weight_per_asset: float = 0.14

    regime_filter: RegimeFilterCfg = RegimeFilterCfg()
    funding_tilt: FundingTiltCfg = FundingTiltCfg()

    entry_zscore_min: float = 0.4
    diversify: DiversifyCfg = DiversifyCfg()
    vol_target: VolTargetCfg = VolTargetCfg()

    entry_throttle: EntryThrottleCfg = EntryThrottleCfg()
    soft_kill: SoftKillCfg = SoftKillCfg()

class LiquidityCfg(BaseModel):
    adv_cap_pct: float = 0.002
    notional_cap_usdt: float = 70.0

class ExecutionCfg(BaseModel):
    order_type: str = "limit"
    post_only: bool = True
    price_offset_bps: int = 3
    slippage_bps_guard: int = 25
    set_leverage: int = 3

    rebalance_minute: int = 5
    poll_seconds: int = 15
    align_after_funding_minutes: int = 0
    funding_hours_utc: List[int] = Field(default_factory=lambda: [0, 8, 16])

    min_notional_per_order_usdt: float = 15.0
    min_rebalance_delta_bps: float = 60.0

    # NEW: documented, but the bot now reconciles regardless (safety first)
    reload_positions_on_start: bool = True

class RiskCfg(BaseModel):
    atr_len: int = 28
    atr_mult_sl: float = 3.2
    atr_mult_tp: float = 4.2
    use_tp: bool = True

    fast_check_seconds: int = 2
    stop_timeframe: str = "5m"
    trailing_enabled: bool = True
    trail_atr_mult: float = 3.0
    breakeven_after_r: float = 2.0

    stop_on_close_only: bool = True
    stop_buffer_bps: float = 10.0
    cooldown_minutes_after_stop: int = 90

    partial_tp_enabled: bool = True
    partial_tp_r: float = 2.5
    partial_tp_size: float = 0.5

    # HARD kill-switch (can trail from intraday high)
    max_daily_loss_pct: float = 3.0
    trade_disable_minutes: int = 360
    use_trailing_killswitch: bool = True  # measure DD from day_high_equity

    min_close_pnl_pct: float = 2.0
    max_hours_in_trade: int = 48

class CostsCfg(BaseModel):
    taker_fee_bps: float = 6.0
    maker_fee_bps: float = 0.0
    slippage_bps: float = 6.0
    funding_bps_per_day: float = 1.0

class PathsCfg(BaseModel):
    state_path: str = "state/state.json"
    logs_dir: str = "logs"

class LoggingCfg(BaseModel):
    level: str = "INFO"
    file_max_mb: int = 20
    file_backups: int = 5

class AppConfig(BaseModel):
    exchange: ExchangeCfg = ExchangeCfg()
    strategy: StrategyCfg = StrategyCfg()
    liquidity: LiquidityCfg = LiquidityCfg()
    execution: ExecutionCfg = ExecutionCfg()
    risk: RiskCfg = RiskCfg()
    costs: CostsCfg = CostsCfg()
    paths: PathsCfg = PathsCfg()
    logging: LoggingCfg = LoggingCfg()

def load_config(path: str) -> AppConfig:
    with open(path, "r") as f:
        raw = yaml.safe_load(f)
    return AppConfig(**raw)
