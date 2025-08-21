# v1.2.0 – 2025-08-21
from __future__ import annotations
from typing import List, Optional
from pydantic import BaseModel, Field
import yaml

# ===== Sub-configs =====

class ExchangeCfg(BaseModel):
    id: str = "bybit"
    account_type: str = "swap"      # "swap" or "spot"
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
    slope_min_bps_per_day: float = 0.0  # UNBLOCK: allow flat slope
    use_abs: bool = False               # if True, gate on |slope|

class FundingTiltCfg(BaseModel):
    enabled: bool = True
    weight: float = 0.15

class DiversifyCfg(BaseModel):
    enabled: bool = True
    corr_lookback: int = 96
    max_pair_corr: float = 0.75

class VolTargetCfg(BaseModel):
    enabled: bool = True
    target_daily_vol_bps: float = 80.0
    min_scale: float = 0.6
    max_scale: float = 1.3

class StrategyCfg(BaseModel):
    lookbacks: List[int] = Field(default_factory=lambda: [1, 6, 24])
    lookback_weights: List[float] = Field(default_factory=lambda: [1.0, 1.0, 1.0])
    vol_lookback: int = 72

    k_min: int = 4       # UNBLOCK: smaller K bounds for small equity
    k_max: int = 10

    market_neutral: bool = True
    gross_leverage: float = 1.25
    max_weight_per_asset: float = 0.16

    regime_filter: RegimeFilterCfg = RegimeFilterCfg()
    funding_tilt: FundingTiltCfg = FundingTiltCfg()

    # Entry quality gating
    entry_zscore_min: float = 0.20

    diversify: DiversifyCfg = DiversifyCfg()
    vol_target: VolTargetCfg = VolTargetCfg()

class LiquidityCfg(BaseModel):
    adv_cap_pct: float = 0.002
    notional_cap_usdt: float = 80.0  # small acct cap

class ExecutionCfg(BaseModel):
    order_type: str = "market"      # "market" (initially to verify flow), later you can switch to "limit"
    post_only: bool = False
    price_offset_bps: int = 0       # for limit mode; 0 when market
    slippage_bps_guard: int = 25
    set_leverage: int = 3

    rebalance_minute: int = 0       # UNBLOCK: don’t delay after funding, trade right away
    poll_seconds: int = 15
    align_after_funding_minutes: int = 0
    funding_hours_utc: List[int] = Field(default_factory=lambda: [0, 8, 16])

    # small-account churn guards
    min_notional_per_order_usdt: float = 10.0
    min_rebalance_delta_bps: float = 40.0   # 0.40% equity delta

class RiskCfg(BaseModel):
    # Stop/TP sizing (use Wilder ATR; see signals.compute_atr(method="rma"))
    atr_len: int = 28
    atr_mult_sl: float = 2.8
    atr_mult_tp: float = 4.0
    use_tp: bool = True

    # Fast SL/TP loop
    fast_check_seconds: int = 2
    stop_timeframe: str = "5m"
    trailing_enabled: bool = True
    trail_atr_mult: float = 2.6
    breakeven_after_r: float = 2.0

    stop_on_close_only: bool = True
    stop_buffer_bps: float = 10.0
    cooldown_minutes_after_stop: int = 45

    partial_tp_enabled: bool = False

    # Governance
    max_daily_loss_pct: float = 3.5
    trade_disable_minutes: int = 180
    min_close_pnl_pct: float = 2.0

    # Time-based exit
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
