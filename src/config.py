from typing import List, Optional
from pydantic import BaseModel, Field

class ExchangeCfg(BaseModel):
    id: str = "bybit"
    account_type: str = "swap"
    quote: str = "USDT"
    only_perps: bool = True
    unified_margin: bool = True
    testnet: bool = False
    max_symbols: int = 120
    min_usd_volume_24h: float = 1_000_000
    min_price: float = 0.005
    timeframe: str = "1h"
    candles_limit: int = 1200

class RegimeFilterCfg(BaseModel):
    enabled: bool = False
    ema_len: int = 200
    slope_min_bps_per_day: float = 5.0

class FundingTiltCfg(BaseModel):
    enabled: bool = False
    weight: float = 0.2

class StrategyCfg(BaseModel):
    lookbacks: List[int] = Field(default_factory=lambda: [1, 6, 24])
    lookback_weights: List[float] = Field(default_factory=lambda: [1.0, 1.0, 1.0])
    vol_lookback: int = 48
    k_min: int = 6
    k_max: int = 20
    market_neutral: bool = True
    gross_leverage: float = 2.0
    max_weight_per_asset: float = 0.10
    regime_filter: RegimeFilterCfg = RegimeFilterCfg()
    funding_tilt: FundingTiltCfg = FundingTiltCfg()

class LiquidityCfg(BaseModel):
    adv_cap_pct: float = 0.004
    notional_cap_usdt: float = 50_000.0

class ExecutionCfg(BaseModel):
    order_type: str = "market"    # market or limit
    post_only: bool = True
    price_offset_bps: int = 5
    slippage_bps_guard: int = 25
    set_leverage: int = 3
    rebalance_minute: int = 15    # calmer default vs 5
    poll_seconds: int = 15
    align_after_funding_minutes: int = 0
    funding_hours_utc: List[int] = Field(default_factory=lambda: [0, 8, 16])

class RiskCfg(BaseModel):
    # Stop/TP sizing (calmer)
    atr_len: int = 21
    atr_mult_sl: float = 2.2
    atr_mult_tp: float = 3.8
    use_tp: bool = True

    # Fast loop controls
    fast_check_seconds: int = 2
    stop_timeframe: str = "3m"
    trailing_enabled: bool = True
    trail_atr_mult: float = 2.2
    breakeven_after_r: float = 1.5

    # Wick resistance (NEW)
    stop_on_close_only: bool = True
    stop_buffer_bps: float = 8.0
    cooldown_minutes_after_stop: int = 30

    # Partials
    partial_tp_enabled: bool = True
    partial_tp_r: float = 2.0
    partial_tp_size: float = 0.5

    # Kill switch & churn gate
    max_daily_loss_pct: float = 5.0
    trade_disable_minutes: int = 120
    min_close_pnl_pct: float = 1.0

class CostsCfg(BaseModel):
    taker_fee_bps: float = 7.0
    maker_fee_bps: float = 1.5
    slippage_bps: float = 5.0
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

def load_config(path: str) -> "AppConfig":
    import yaml
    with open(path, "r") as f:
        raw = yaml.safe_load(f)
    return AppConfig(**(raw or {}))
