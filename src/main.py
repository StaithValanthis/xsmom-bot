# main.py — hardened for systemd: pre-insert project paths into sys.path
from __future__ import annotations
import os, sys, argparse, logging

# --- Ensure project and src directories are on sys.path BEFORE imports ---
_THIS = os.path.abspath(__file__)
_SRC_DIR = os.path.dirname(_THIS)                 # e.g., /opt/xsmom-bot/src
_PROJ_DIR = os.path.dirname(_SRC_DIR)             # e.g., /opt/xsmom-bot

for p in (_SRC_DIR, _PROJ_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

# Now attempt imports in this order:
MODE = "unknown"
try:
    # 1) relative package import (works with: python -m src.main)
    from .config import load_config       # type: ignore
    from .live import run_live            # type: ignore
    from .backtester import run_backtest  # type: ignore
    from .exchange import ExchangeWrapper # type: ignore
    MODE = "pkg-rel"
except Exception:
    try:
        # 2) plain imports from src on sys.path (works with: python /opt/xsmom-bot/src/main.py)
        from config import load_config    # type: ignore
        from live import run_live         # type: ignore
        from backtester import run_backtest  # type: ignore
        from exchange import ExchangeWrapper # type: ignore
        MODE = "src-plain"
    except Exception:
        # 3) absolute package import from project root (works with: python -m src.main from /opt/xsmom-bot)
        from src.config import load_config    # type: ignore
        from src.live import run_live         # type: ignore
        from src.backtester import run_backtest  # type: ignore
        from src.exchange import ExchangeWrapper # type: ignore
        MODE = "pkg-abs"

log = logging.getLogger("main")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s", datefmt="%Y-%m-%dT%H:%M:%SZ")

def parse_args():
    p = argparse.ArgumentParser()
    # LEGACY support: allow positional 'live' or 'backtest' after main module
    p.add_argument('mode_pos', nargs='?', choices=['live', 'backtest'], help='(legacy) positional mode')
    p.add_argument("--config", default=os.environ.get("XSMOM_CONFIG", "/opt/xsmom-bot/config/config.yaml"), help="Path to YAML config")
    p.add_argument("--mode", choices=["live", "backtest"], default=None)
    p.add_argument("--dry", action="store_true", help="Paper-trade mode")
    args = p.parse_args()
    # resolve mode: explicit flag wins, else legacy positional, else default 'live'
    args.mode = args.mode or args.mode_pos or 'live'
    return args

def _run_backtest_flow(cfg):
    # Build universe from current exchange filters, then run backtest once and log stats
    ex = ExchangeWrapper(cfg.exchange)
    try:
        universe = ex.fetch_markets_filtered()
        if not universe:
            log.error("No symbols after filters; cannot run backtest.")
            return 2
    finally:
        try:
            ex.close()
        except Exception:
            pass
    stats = run_backtest(cfg, universe, prefetch_bars=None, return_curve=False)
    if not stats:
        log.error("Backtest returned no stats; check logs.")
        return 3
    log.info("Backtest stats: %s", {k: (float(v) if isinstance(v, (int, float)) else str(type(v))) for k, v in stats.items()})
    print("=== BACKTEST RESULTS ===")
    print(f"Universe size: {len(universe)} | Timeframe: {cfg.exchange.timeframe}")
    print(f"Total Return:  {stats.get('total_return', 0.0):.2%}")
    print(f"Annualized:    {stats.get('annualized', 0.0):.2%}")
    print(f"Max Drawdown:  {stats.get('max_drawdown', 0.0):.2%}")
    print(f"Sharpe:        {stats.get('sharpe', 0.0):.2f}")
    print(f"Calmar:        {stats.get('calmar', 0.0):.2f}")
    return 0

def main():
    args = parse_args()
    cfg = load_config(args.config)
    log.info(f"Starting {args.mode} loop (dry={args.dry}) using config={args.config} [import_mode={MODE}]")
    if args.mode == "live":
        run_live(cfg, dry=args.dry)
    else:
        code = _run_backtest_flow(cfg)
        raise SystemExit(code)

if __name__ == "__main__":
    main()
