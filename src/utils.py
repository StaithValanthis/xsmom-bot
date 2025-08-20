import json
import logging
import os
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
from typing import Optional


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def setup_logging(level: str, logs_dir: str, file_max_mb: int, file_backups: int):
    """
    Configure logging to:
      - Console (stdout)
      - Daily rotating log file in `logs/` that rolls at UTC midnight

    Files created:
      logs/xsmom.log              (current file)
      logs/xsmom.log.YYYYMMDD     (previous days, rotated automatically)

    Notes:
      * We keep `file_backups` days of history.
      * We still accept `file_max_mb` param for backward-compat, but
        rotation is now time-based (daily) instead of size-based.
    """
    os.makedirs(logs_dir, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level.upper())

    fmt = logging.Formatter(
        fmt="%(asctime)sZ | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    # --- Console handler (stdout) ---
    sh = logging.StreamHandler()
    sh.setLevel(level.upper())
    sh.setFormatter(fmt)

    # --- Daily rotating file handler (UTC midnight rollover) ---
    # Base filename stays 'xsmom.log' while rotated files are suffixed with date:
    #   xsmom.log.20250820, xsmom.log.20250821, ...
    file_path = os.path.join(logs_dir, "xsmom.log")
    fh = TimedRotatingFileHandler(
        file_path,
        when="midnight",
        interval=1,
        backupCount=file_backups,
        utc=True,
    )
    # Add YYYYMMDD suffix to the rotated filenames
    # Result: xsmom.log.20250820
    fh.suffix = "%Y%m%d"
    fh.setLevel(level.upper())
    fh.setFormatter(fmt)

    # Replace existing handlers (avoid duplicates when re-running in same process)
    root.handlers = []
    root.addHandler(sh)
    root.addHandler(fh)


def read_json(path: str, default):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path: str, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def load_env_file_if_present():
    """
    Minimal .env loader for manual runs.
    Loads key=value pairs from <repo_root>/.env if present, but DOES NOT override
    any environment variables that are already set in the process.

    This makes `python -m src.main ...` behave like run_local.sh/systemd, which
    already export BYBIT_API_KEY/SECRET.
    """
    try:
        # repo root is parent directory of this file's directory
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        env_path = os.path.join(root, ".env")
        if not os.path.isfile(env_path):
            return
        with open(env_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if not k:
                    continue
                if k not in os.environ:
                    os.environ[k] = v
    except Exception:
        # Never fail app boot because of .env parsing
        pass
