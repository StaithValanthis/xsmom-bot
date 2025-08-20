import argparse
import logging

from .config import load_config
from .exchange import ExchangeWrapper
from .backtester import run_backtest
from .live import run_live
from .utils import setup_logging, load_env_file_if_present


def main():
    # Ensure .env is loaded for manual runs (systemd and run_local.sh already export it)
    load_env_file_if_present()

    p = argparse.ArgumentParser(prog="xsmom-bot")
    p.add_argument("cmd", choices=["backtest", "live", "scan"])
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

    elif args.cmd == "live":
        run_live(cfg, dry=args.dry)


if __name__ == "__main__":
    main()
