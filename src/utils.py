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
    """
    os.makedirs(logs_dir, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level.upper())

    fmt = logging.Formatter(
        fmt="%(asctime)sZ | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    # Console
    sh = logging.StreamHandler()
    sh.setLevel(level.upper())
    sh.setFormatter(fmt)

    # Daily rotating file (UTC)
    file_path = os.path.join(logs_dir, "xsmom.log")
    fh = TimedRotatingFileHandler(
        file_path,
        when="midnight",
        interval=1,
        backupCount=file_backups,
        utc=True,
    )
    fh.suffix = "%Y%m%d"
    fh.setLevel(level.upper())
    fh.setFormatter(fmt)

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
        # default=str turns non-JSON-native objects (e.g., pd.Timestamp) into strings
        json.dump(data, f, indent=2, default=str)


def load_env_file_if_present():
    """
    Minimal .env loader for manual runs.
    Loads key=value pairs from <repo_root>/.env if present, but DOES NOT override
    any environment variables that are already set in the process.
    """
    try:
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
        pass
