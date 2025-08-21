# v1.1.0 – 2025-08-21
import argparse
import logging

from .config import load_config
from .exchange import ExchangeWrapper
from .backtester import run_backtest
from .live import run_live
from .utils import setup_logging, load_env_file_if_present
from .optimizer import optimize  # NEW

def main():
    # Ensure .env is loaded for manual runs (systemd and run_local.sh already export it)
    load_env_file_if_present()

    p = argparse.ArgumentParser(prog="xsmom-bot")
    p.add_argument("cmd", choices=["backtest", "live", "scan", "optimize"])
    p.add_argument("--config", required=True, help="Path to YAML config")
    p.add_argument("--dry", action="store_true", help="Live dry-run mode")
    args = p.parse_args()

    cfg = load_config(args.config)
    setup_logging(cfg.logging.level, cfg.paths.logs_dir, cfg.logging.file_max_mb, cfg.logging.file_backups)

    if args.cmd == "scan":
        ex = ExchangeWrapper(cfg.exchange)
        try:
            syms = ex.fetch_markets_filtered()
            logging.getLogger("scan").info(
                f"Symbols ({len(syms)}): {', '.join(syms[:50])}{'...' if len(syms) > 50 else ''}"
            )
        finally:
            ex.close()

    elif args.cmd == "backtest":
        ex = ExchangeWrapper(cfg.exchange)
        syms = ex.fetch_markets_filtered()
        ex.close()
        run_backtest(cfg, syms)

    elif args.cmd == "optimize":
        best_params, best_stats = optimize(cfg)
        if best_params:
            logging.getLogger("opt").info(
                f"Recommended (Calmar={best_stats.get('calmar',0):.2f}, Sharpe={best_stats.get('sharpe',0):.2f}, "
                f"Ann={best_stats.get('annualized',0):.2%}, DD={best_stats.get('max_drawdown',0):.2%}):\n{best_params}"
            )

    elif args.cmd == "live":
        run_live(cfg, dry=args.dry)

if __name__ == "__main__":
    main()
