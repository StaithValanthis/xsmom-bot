# High-Level Architecture

## System Overview

**xsmom-bot** is a fully automated, multi-pair crypto futures trading system built on Bybit USDT-perpetual futures.

### Core Components

```
┌─────────────────────────────────────────────────────────────┐
│                    XSMOM-BOT SYSTEM                         │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  ┌──────────────┐                                           │
│  │   Exchange   │  CCXT wrapper for Bybit                   │
│  │   (Bybit)    │  • Fetch OHLCV, tickers, order books      │
│  └──────────────┘  • Place/cancel orders                    │
│       │             • Get equity, positions                  │
│       │                                                      │
│       ▼                                                      │
│  ┌──────────────┐                                           │
│  │   Data       │  OHLCV bars, tickers, funding rates       │
│  │  Ingestion   │  • Filter by volume/price                 │
│  └──────────────┘  • Warmup period                          │
│       │                                                      │
│       ▼                                                      │
│  ┌──────────────┐                                           │
│  │   Signals    │  Signal generation                        │
│  │  Generator   │  • Multi-lookback momentum                │
│  │              │  • Cross-sectional z-scores               │
│  │              │  • Signal power amplification             │
│  └──────────────┘                                           │
│       │                                                      │
│       ▼                                                      │
│  ┌──────────────┐                                           │
│  │   Filters    │  Signal filtering                         │
│  │              │  • Regime filter (EMA slope)              │
│  │              │  • ADX filter                             │
│  │              │  • Symbol scoring                         │
│  │              │  • Time-of-day whitelist                  │
│  └──────────────┘                                           │
│       │                                                      │
│       ▼                                                      │
│  ┌──────────────┐                                           │
│  │   Sizing     │  Position sizing engine                   │
│  │   Engine     │  • Top-K selection (long/short)          │
│  │              │  • Inverse-volatility sizing              │
│  │              │  • Per-asset and portfolio caps           │
│  │              │  • Volatility targeting                   │
│  │              │  • Kelly scaling (optional)               │
│  └──────────────┘                                           │
│       │                                                      │
│       ▼                                                      │
│  ┌──────────────┐                                           │
│  │   Risk       │  Risk management                          │
│  │  Management  │  • Daily loss limits                      │
│  │              │  • Drawdown tracking                       │
│  │              │  • Kill-switch logic                      │
│  │              │  • Position-level stops                   │
│  └──────────────┘                                           │
│       │                                                      │
│       ▼                                                      │
│  ┌──────────────┐                                           │
│  │   Execution  │  Order management                         │
│  │              │  • Order reconciliation                   │
│  │              │  • Limit order placement                  │
│  │              │  • Stale order cleanup                    │
│  │              │  • Spread guard                           │
│  └──────────────┘                                           │
│       │                                                      │
│       ▼                                                      │
│  ┌──────────────┐                                           │
│  │   State      │  State persistence                        │
│  │              │  • Position tracking                      │
│  │              │  • Cooldowns and bans                     │
│  │              │  • Daily equity tracking                  │
│  │              │  • Symbol statistics                      │
│  └──────────────┘                                           │
│                                                              │
│  ┌──────────────────────────────────────────────────────┐  │
│  │              Background Threads                      │  │
│  │  • FastSLTPThread: Stop-loss/take-profit monitoring │  │
│  │    (runs every 2s, checks 5m bars)                  │  │
│  └──────────────────────────────────────────────────────┘  │
│                                                              │
│  ┌──────────────────────────────────────────────────────┐  │
│  │            Optimizer (Separate Process)              │  │
│  │  Walk-Forward + Bayesian + Monte Carlo              │  │
│  │  • Runs weekly (systemd timer)                      │  │
│  │  • Optimizes 18 core parameters                     │  │
│  │  • Deploys new configs with versioning              │  │
│  └──────────────────────────────────────────────────────┘  │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

---

## Data Flow

### Live Trading Loop

```
1. Exchange API
   ↓
2. Fetch OHLCV bars (for universe symbols)
   ↓
3. Signal Generation
   ├─ Multi-lookback momentum (12h, 24h, 48h, 96h)
   ├─ Cross-sectional z-scores
   └─ Signal power amplification
   ↓
4. Signal Filtering
   ├─ Regime filter (EMA slope)
   ├─ ADX filter
   ├─ Symbol scoring
   └─ Time-of-day whitelist
   ↓
5. Position Sizing
   ├─ Top-K selection (long/short)
   ├─ Inverse-volatility sizing
   ├─ Per-asset caps
   ├─ Portfolio vol targeting
   └─ Kelly scaling (optional)
   ↓
6. Risk Checks
   ├─ Daily loss limit
   ├─ Drawdown limit
   └─ Kill-switch logic
   ↓
7. Order Reconciliation
   ├─ Cancel stale orders
   ├─ Place new limit orders
   └─ Update positions
   ↓
8. State Persistence
   └─ Write state JSON (positions, stats, cooldowns)
```

### Stop-Loss / Take-Profit Loop (Background Thread)

```
FastSLTPThread (runs every 2s):
1. Fetch current positions
   ↓
2. Fetch latest 5m bars
   ↓
3. Check stop-loss triggers
   ├─ ATR-based stops
   ├─ Trailing stops
   └─ Breakeven moves
   ↓
4. Check take-profit triggers
   ├─ Partial TP ladders
   └─ Full exits
   ↓
5. Place exit orders if triggered
   ↓
6. Update state (cooldowns, PnL)
```

### Backtesting Flow

```
1. Fetch historical OHLCV (from exchange or local)
   ↓
2. Warmup period (max(lookbacks) + vol_lookback + 5 bars)
   ↓
3. For each bar:
   a. Compute targets (same as live trading loop)
   b. Portfolio return = (targets × returns).sum()
   c. Costs = fees + slippage + funding
   d. Equity += pnl + costs
   ↓
4. Compute performance metrics:
   ├─ Total return, annualized return
   ├─ Sharpe ratio, Calmar ratio
   ├─ Max drawdown
   └─ Turnover statistics
```

### Optimizer Flow

```
1. Fetch historical data
   ↓
2. Generate WFO segments (train/OOS windows)
   ↓
3. For each segment:
   a. Run Bayesian Optimization on training data
   b. Select top K parameter sets
   c. Evaluate on OOS window
   d. Run Monte Carlo stress tests
   ↓
4. Aggregate metrics across segments
   ↓
5. Compare to baseline (current live config)
   ├─ Check improvement thresholds
   └─ Check safety constraints
   ↓
6. If approved: Deploy new config (with backup)
   Else: Keep existing config
```

---

## Module Responsibilities

### Core Modules (`src/`)

- **`main.py`** - Entry point: CLI parsing, dispatches to `live` or `backtest`
- **`config.py`** - Pydantic config schema: type-safe configuration management
- **`exchange.py`** - CCXT wrapper: unified interface for Bybit (fetch OHLCV, orders, equity)
- **`live.py`** - Live trading loop: orchestrates strategy execution, order management, risk checks
- **`backtester.py`** - Backtesting engine: simulates strategy with realistic costs
- **`signals.py`** - Signal generation: momentum, regime filters, ADX, meta-labeler
- **`sizing.py`** - Position sizing: inverse-volatility, Kelly scaling, caps, vol targeting
- **`risk.py`** - Risk management: kill-switch, drawdown tracking, daily loss limits
- **`regime_router.py`** - Regime switching: dynamically chooses XSMOM vs TSMOM
- **`carry.py`** - Carry trading: funding/basis trades with delta-neutral hedging
- **`anti_churn.py`** - Trade throttling: prevents overtrading via cooldowns
- **`utils.py`** - Utilities: JSON I/O, logging setup, health checks

### Optimizer Modules (`src/optimizer/`)

- **`full_cycle.py`** - Full-cycle orchestrator: WFO + BO + MC + deployment
- **`walk_forward.py`** - Walk-forward optimization: purged segments with embargo
- **`bo_runner.py`** - Bayesian optimization: Optuna TPE sampler
- **`monte_carlo.py`** - Monte Carlo stress testing: bootstrap and cost perturbation
- **`backtest_runner.py`** - Backtest runner: clean entrypoint with parameter overrides
- **`config_manager.py`** - Config manager: versioning, deployment, rollback
- **`rollback_cli.py`** - Rollback CLI: restore previous config versions

### Infrastructure Modules

- **`notifications/discord_notifier.py`** - Discord webhook client: embeds, rate limiting
- **`notifications/optimizer_notifications.py`** - Optimizer notifications: formats and sends results
- **`reports/daily_report.py`** - Daily performance reports: PnL aggregation, Discord notifications

For complete module map, see [`module_map.md`](module_map.md) (auto-generated).

---

## Data Storage

### State File

**Location**: `config/paths.state_path` (default: `/opt/xsmom-bot/state.json`)

**Contents:**
```json
{
  "perpos": { ... },              // Per-position state (entry price, stop level, etc.)
  "cooldowns": { ... },           // Symbol cooldowns (ban lists, trade throttling)
  "day_start_equity": 10000.0,    // Equity at start of UTC day
  "day_high_equity": 10200.0,     // Highest equity during day
  "day_date": "2025-11-17",       // Current UTC date
  "sym_stats": { ... },           // Per-symbol trade statistics
  "hour_stats": { ... },          // Per-hour trade statistics (for ToD filter)
  ...
}
```

**Purpose:**
- Crash recovery (reload positions on restart)
- Trade throttling (cooldowns, bans)
- Daily equity tracking (kill-switch thresholds)
- Symbol statistics (performance-based filtering)

**Format:** JSON (atomic writes via `utils.write_json_atomic()`)

### Config Files

**Location**: `config/config.yaml` (live), `config/optimized/config_YYYYMMDD_HHMMSS.yaml` (versioned)

**Contents:**
- Exchange settings (universe, timeframe)
- Strategy parameters (lookbacks, K selection, signal power)
- Risk controls (daily loss limits, stop-loss multipliers)
- Execution settings (rebalance timing, order placement)
- Optimizer settings (WFO/BO/MC configuration)

**Format:** YAML (Pydantic validation via `config.load_config()`)

### Logs

**Location**: `config/paths.logs_dir` (default: `/opt/xsmom-bot/logs`)

**Contents:**
- Daily rotating log files (console + file)
- Optimizer results (`logs/optimizer_full_cycle_YYYYMMDD_HHMMSS.json`)
- Daily report logs (if cron/systemd configured)

---

## Communication Patterns

### Exchange API

- **Protocol**: CCXT (unified exchange interface)
- **Exchange**: Bybit USDT-perp futures
- **Retry Logic**: `tenacity` (exponential backoff)
- **Rate Limiting**: CCXT built-in rate limiting

### Discord Notifications

- **Protocol**: HTTP webhooks
- **Format**: Rich embeds (title, description, fields, colors)
- **Rate Limiting**: Automatic retry on 429 with backoff
- **Non-Blocking**: Failures logged but don't crash bot

### State Persistence

- **Format**: JSON files
- **Write Method**: Atomic writes (temporary file + rename)
- **Read Method**: Safe reads with defaults (graceful degradation)
- **Frequency**: Every cycle (after position updates)

---

## Concurrency Model

### Main Loop

- **Thread**: Single main thread
- **Frequency**: Every hour (at minute 1, configurable)
- **Blocking**: Synchronous API calls (with retry logic)

### Background Threads

- **FastSLTPThread**: Stop-loss/take-profit monitoring
  - Runs every 2 seconds
  - Checks 5-minute bars
  - Places exit orders if triggered
  - Thread-safe state updates (via locks)

### Optimizer

- **Process**: Separate process (via systemd or CLI)
- **Frequency**: Weekly (configurable via systemd timer)
- **Blocking**: Synchronous backtests (can take hours)

---

## Error Handling & Resilience

### Exchange API Failures

- **Retry Logic**: `tenacity` with exponential backoff (up to 3 attempts)
- **Fallback**: Skip cycle if API fails (log warning, continue)
- **Rate Limiting**: CCXT built-in rate limiting

### Config File Errors

- **Validation**: Pydantic schema validation (fails fast on startup)
- **Defaults**: Safe defaults in `config._merge_defaults()`
- **Rollback**: Previous config backed up before deployment

### State File Corruption

- **Atomic Writes**: `utils.write_json_atomic()` (temporary file + rename)
- **Safe Reads**: `utils.read_json()` (defaults on error)
- **Recovery**: Position reconciliation on startup (reload from exchange)

### Network Failures

- **Retry Logic**: Built into `exchange.py` (tenacity)
- **Timeout**: 20-second timeout on API calls
- **Fallback**: Skip cycle if exchange unreachable (log warning)

---

## Performance Characteristics

### Latency

- **Main Loop**: ~1-5 seconds per cycle (fetch data, compute signals, place orders)
- **FastSLTPThread**: ~100ms per check (fetch positions, check stops)
- **Exchange API**: ~200-500ms per call (with retry logic)

### Throughput

- **Symbols**: Up to 36 symbols (configurable via `exchange.max_symbols`)
- **Positions**: Up to 12 open positions (configurable via `strategy.entry_throttle.max_open_positions`)
- **Orders**: ~5-10 orders per cycle (limit orders, post-only)

### Resource Usage

- **Memory**: ~100-200 MB (pandas DataFrames, state dict)
- **CPU**: Low (simple calculations, mostly I/O bound)
- **Disk**: ~1-10 MB (state file, logs, configs)

---

## Security & Safety

### API Key Management

- **Storage**: Environment variables (`.env` file, not committed)
- **Access**: Read-only for live trading (no withdrawals)
- **Testnet**: Separate testnet API keys for testing

### Risk Limits

- **Daily Loss Limit**: Stop trading if daily loss > threshold (default: 5%)
- **Drawdown Limit**: Stop trading if max drawdown > threshold (optional)
- **Position Caps**: Per-asset and portfolio-level caps

### Crash Recovery

- **Position Reconciliation**: Reload positions from exchange on startup
- **State Persistence**: Atomic writes prevent corruption
- **Order Cleanup**: Cancel stale orders on startup (configurable)

---

## Monitoring & Observability

### Logging

- **Format**: Structured logs (timestamp, level, module, message)
- **Output**: Console + daily rotating files
- **Level**: Configurable via `config.logging.level` (default: INFO)

### Health Checks

- **Heartbeat**: `utils.write_heartbeat()` writes timestamp to file
- **Monitoring**: External system can check heartbeat freshness
- **Location**: `config/paths.state_path` + `.heartbeat` suffix

### Discord Notifications

- **Optimizer Results**: Sent after each optimization run
- **Daily Reports**: Sent daily (if cron/systemd configured)
- **Format**: Rich embeds with color coding (green/orange/red)

See [`../operations/monitoring_and_alerts.md`](../operations/monitoring_and_alerts.md) for detailed setup.

---

## Deployment Model

### Development

```bash
# Run backtest
python -m src.main backtest --config config/config.yaml

# Run live (testnet)
python -m src.main live --config config/config.yaml
```

### Production

- **Service**: `systemd/xsmom-bot.service` (main trading bot)
- **Optimizer Timer**: `systemd/xsmom-optimizer-full-cycle.timer` (weekly)
- **Daily Report Timer**: `systemd/xsmom-daily-report.timer` (daily at 00:05 UTC)

See [`../operations/deployment_ubuntu_systemd.md`](../operations/deployment_ubuntu_systemd.md) for setup.

---

## Next Steps

- **Strategy Logic**: [`strategy_logic.md`](strategy_logic.md) - How the strategy works conceptually
- **Risk Management**: [`risk_management.md`](risk_management.md) - Risk limits, sizing, stops
- **Config System**: [`config_system.md`](config_system.md) - How config.yaml maps to code
- **Knowledge Base**: [`../kb/framework_overview.md`](../kb/framework_overview.md) - Complete framework map

---

**Motto: MAKE MONEY** — with a clear, well-understood, and well-documented architecture. 📈

