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
    MODE = "pkg-rel"
except Exception:
    try:
        # 2) plain imports from src on sys.path (works with: python /opt/xsmom-bot/src/main.py)
        from config import load_config    # type: ignore
        from live import run_live         # type: ignore
        MODE = "src-plain"
    except Exception:
        # 3) absolute package import from project root (works with: python -m src.main from /opt/xsmom-bot)
        from src.config import load_config    # type: ignore
        from src.live import run_live         # type: ignore
        MODE = "pkg-abs"

log = logging.getLogger("main")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s", datefmt="%Y-%m-%dT%H:%M:%SZ")

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--config", default=os.environ.get("XSMOM_CONFIG", "/opt/xsmom-bot/config/config.yaml"), help="Path to YAML config")
    p.add_argument("--mode", choices=["live", "backtest"], default="live")
    p.add_argument("--dry", action="store_true", help="Paper-trade mode")
    return p.parse_args()

def main():
    args = parse_args()
    cfg = load_config(args.config)
    log.info(f"Starting {args.mode} loop (dry={args.dry}) using config={args.config} [import_mode={MODE}]")
    if args.mode == "live":
        run_live(cfg, dry=args.dry)
    else:
        raise SystemExit("Backtest mode not wired in main.py")

if __name__ == "__main__":
    main()
