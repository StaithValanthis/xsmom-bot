import json
import logging
import os
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Optional

def utcnow() -> datetime:
    return datetime.now(timezone.utc)

def setup_logging(level: str, logs_dir: str, file_max_mb: int, file_backups: int):
    os.makedirs(logs_dir, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level.upper())

    fmt = logging.Formatter(
        fmt="%(asctime)sZ | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    # stdout
    sh = logging.StreamHandler()
    sh.setLevel(level.upper())
    sh.setFormatter(fmt)
    root.addHandler(sh)

    # rotating file
    fh = RotatingFileHandler(
        os.path.join(logs_dir, "xsmom.log"),
        maxBytes=file_max_mb * 1024 * 1024,
        backupCount=file_backups,
    )
    fh.setLevel(level.upper())
    fh.setFormatter(fmt)
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
