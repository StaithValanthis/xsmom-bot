"""
Utility functions for the XSMOM trading bot.

This module provides:
- Time utilities (UTC timestamps)
- Safe JSON file I/O (atomic writes for state persistence)
- Health check utilities (heartbeat for monitoring)
- Logging configuration
- Environment variable loading

All file I/O operations are designed to be crash-safe and fail gracefully.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

# ============================================================================
# TIME UTILITIES (Pure functions, no side effects)
# ============================================================================


def utcnow() -> datetime:
    """
    Get current UTC datetime.

    Returns:
        Current datetime with UTC timezone.

    Example:
        >>> ts = utcnow()
        >>> ts.tzinfo == timezone.utc
        True
    """
    return datetime.now(timezone.utc)


# ============================================================================
# FILE I/O (Side-effectful, but crash-safe)
# ============================================================================


def read_json(path: str, default: Any = None) -> Any:
    """
    Safely read JSON from file, returning default on any error.

    This function is designed to be fail-safe: it will never crash the bot,
    even if the file is corrupted or missing. Logs warnings on errors.

    Args:
        path: File path to read from.
        default: Value to return if file doesn't exist or can't be parsed.

    Returns:
        Parsed JSON data, or default if error occurs.

    Example:
        >>> state = read_json("state.json", default={})
        >>> isinstance(state, dict)
        True
    """
    if not path:
        log.warning("read_json: empty path provided, returning default")
        return default

    try:
        path_obj = Path(path)
        if not path_obj.exists():
            log.debug(f"read_json: file does not exist: {path}, returning default")
            return default

        if not path_obj.is_file():
            log.warning(f"read_json: path is not a file: {path}, returning default")
            return default

        with open(path_obj, "r", encoding="utf-8") as f:
            data = json.load(f)
        log.debug(f"read_json: successfully read {path}")
        return data

    except json.JSONDecodeError as e:
        log.warning(f"read_json: JSON decode error in {path}: {e}, returning default")
        return default

    except PermissionError as e:
        log.warning(f"read_json: permission denied for {path}: {e}, returning default")
        return default

    except OSError as e:
        log.warning(f"read_json: OS error reading {path}: {e}, returning default")
        return default

    except Exception as e:
        log.warning(f"read_json: unexpected error reading {path}: {e}, returning default", exc_info=True)
        return default


def write_json_atomic(path: str, data: Any) -> None:
    """
    Atomically write JSON to file using temp file + rename.

    This prevents corruption if the bot crashes mid-write:
    1. Write to temp file in same directory
    2. Atomically rename temp file to target (rename is atomic on Linux)
    3. If rename fails, temp file remains (can be cleaned up later)

    Args:
        path: File path to write to.
        data: Data to serialize as JSON.

    Raises:
        OSError: If directory creation or file write fails (non-recoverable).
        json.JSONEncodeError: If data cannot be serialized (should not happen with dicts).

    Example:
        >>> write_json_atomic("state.json", {"key": "value"})
        >>> read_json("state.json")["key"]
        'value'
    """
    if not path:
        raise ValueError("write_json_atomic: path cannot be empty")

    path_obj = Path(path)
    parent_dir = path_obj.parent

    # Create parent directory if it doesn't exist
    if parent_dir and not parent_dir.exists():
        try:
            parent_dir.mkdir(parents=True, exist_ok=True)
            log.debug(f"write_json_atomic: created directory {parent_dir}")
        except OSError as e:
            log.error(f"write_json_atomic: failed to create directory {parent_dir}: {e}")
            raise

    # Write to temp file in same directory (ensures same filesystem for atomic rename)
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=parent_dir if parent_dir else None,
            prefix=f"{path_obj.name}.tmp.",
            delete=False,
        ) as tf:
            temp_path = tf.name
            json.dump(data, tf, indent=2, default=str, ensure_ascii=False)
            tf.flush()
            os.fsync(tf.fileno())  # Force write to disk

        # Atomic rename (will fail if target exists and is different filesystem)
        try:
            os.rename(temp_path, path)
            log.debug(f"write_json_atomic: successfully wrote {path}")
        except OSError as e:
            # Cleanup temp file on rename failure
            try:
                os.unlink(temp_path)
            except Exception:
                pass
            log.error(f"write_json_atomic: failed to rename temp file to {path}: {e}")
            raise

    except json.JSONEncodeError as e:
        log.error(f"write_json_atomic: JSON encode error for {path}: {e}")
        raise

    except OSError as e:
        log.error(f"write_json_atomic: OS error writing {path}: {e}")
        raise

    except Exception as e:
        log.error(f"write_json_atomic: unexpected error writing {path}: {e}", exc_info=True)
        raise


def write_json(path: str, data: Any) -> None:
    """
    Write JSON to file (atomic wrapper).

    This is a convenience wrapper around write_json_atomic().
    Maintains backward compatibility with existing code.

    Args:
        path: File path to write to.
        data: Data to serialize as JSON.

    Raises:
        OSError: If write fails.
        json.JSONEncodeError: If serialization fails.

    Example:
        >>> write_json("state.json", {"equity": 10000.0})
    """
    write_json_atomic(path, data)


# ============================================================================
# HEALTH CHECK UTILITIES (NEW - for monitoring)
# ============================================================================


def write_heartbeat(heartbeat_path: str) -> None:
    """
    Write heartbeat file for health monitoring.

    Creates or updates a heartbeat file with current UTC timestamp.
    Used by monitoring systems to detect if the bot is alive.

    Args:
        heartbeat_path: Path to heartbeat file (e.g., "state/heartbeat.json").

    Side effects:
        Writes/updates heartbeat file atomically.

    Example:
        >>> write_heartbeat("state/heartbeat.json")
        >>> hb = read_heartbeat("state/heartbeat.json")
        >>> "ts" in hb
        True
    """
    try:
        heartbeat_data = {
            "ts": utcnow().isoformat(),
            "unix_ts": utcnow().timestamp(),
        }
        write_json_atomic(heartbeat_path, heartbeat_data)
        log.debug(f"write_heartbeat: wrote heartbeat to {heartbeat_path}")

    except Exception as e:
        # Don't crash bot if heartbeat write fails (monitoring is non-critical)
        log.warning(f"write_heartbeat: failed to write {heartbeat_path}: {e}", exc_info=False)


def read_heartbeat(heartbeat_path: str) -> Optional[dict[str, Any]]:
    """
    Read heartbeat file and compute age.

    Args:
        heartbeat_path: Path to heartbeat file.

    Returns:
        Dict with keys:
            - "ts": ISO format timestamp
            - "unix_ts": Unix timestamp
            - "age_sec": Age in seconds (computed)
            - "healthy": True if age < 120 seconds
        Returns None if file doesn't exist or can't be read.

    Example:
        >>> hb = read_heartbeat("state/heartbeat.json")
        >>> if hb:
        ...     print(f"Age: {hb['age_sec']:.1f}s, Healthy: {hb['healthy']}")
    """
    hb_data = read_json(heartbeat_path, None)
    if hb_data is None:
        return None

    try:
        ts_str = hb_data.get("ts")
        if not ts_str:
            log.warning(f"read_heartbeat: missing 'ts' field in {heartbeat_path}")
            return None

        last_ts = datetime.fromisoformat(ts_str)
        if last_ts.tzinfo is None:
            # Assume UTC if no timezone
            last_ts = last_ts.replace(tzinfo=timezone.utc)

        age_sec = (utcnow() - last_ts).total_seconds()
        healthy = age_sec < 120.0  # Consider unhealthy if > 2 minutes old

        return {
            "ts": ts_str,
            "unix_ts": hb_data.get("unix_ts", last_ts.timestamp()),
            "age_sec": age_sec,
            "healthy": healthy,
        }

    except (ValueError, KeyError, TypeError) as e:
        log.warning(f"read_heartbeat: error parsing {heartbeat_path}: {e}")
        return None


# ============================================================================
# LOGGING CONFIGURATION (Side-effectful, unchanged)
# ============================================================================


def setup_logging(level: str, logs_dir: str, file_max_mb: int, file_backups: int) -> None:
    """
    Configure logging to console and daily rotating file.

    Sets up:
    - Console handler (stdout)
    - Daily rotating log file in `logs/` that rolls at UTC midnight

    Files created:
    - logs/xsmom.log              (current file)
    - logs/xsmom.log.YYYYMMDD     (previous days, rotated automatically)

    Args:
        level: Log level (e.g., "INFO", "DEBUG").
        logs_dir: Directory for log files.
        file_max_mb: Maximum file size in MB (currently unused, file rotates daily).
        file_backups: Number of backup files to keep.

    Side effects:
        Configures global logging handlers (replaces existing).

    Example:
        >>> setup_logging("INFO", "logs/", 20, 5)
    """
    os.makedirs(logs_dir, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level.upper())

    fmt = logging.Formatter(
        fmt="%(asctime)sZ | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    # Console handler
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
    log.info(f"setup_logging: configured logging (level={level}, dir={logs_dir})")


# ============================================================================
# ENVIRONMENT LOADING (Side-effectful, improved error handling)
# ============================================================================


def load_env_file_if_present() -> None:
    """
    Load environment variables from .env file if present.

    Loads key=value pairs from <repo_root>/.env if present.
    Does NOT override environment variables already set in the process.

    Format:
        # Comments allowed
        KEY=value
        ANOTHER_KEY=another_value

    Side effects:
        Sets os.environ for keys not already set.

    Example:
        >>> load_env_file_if_present()
        >>> os.getenv("BYBIT_API_KEY")
        'your_key_here'  # If set in .env and not already in os.environ
    """
    try:
        # Find repo root (parent of src/)
        src_dir = Path(__file__).parent
        repo_root = src_dir.parent
        env_path = repo_root / ".env"

        if not env_path.is_file():
            log.debug("load_env_file_if_present: .env file not found, skipping")
            return

        loaded_count = 0
        with open(env_path, "r", encoding="utf-8") as f:
            for line_num, line in enumerate(f, start=1):
                line = line.strip()
                # Skip empty lines and comments
                if not line or line.startswith("#"):
                    continue

                # Parse key=value
                if "=" not in line:
                    log.warning(f"load_env_file_if_present: skipping malformed line {line_num} in {env_path}")
                    continue

                parts = line.split("=", 1)
                if len(parts) != 2:
                    log.warning(f"load_env_file_if_present: skipping malformed line {line_num} in {env_path}")
                    continue

                key, value = parts[0].strip(), parts[1].strip()
                if not key:
                    log.warning(f"load_env_file_if_present: skipping line {line_num} with empty key in {env_path}")
                    continue

                # Only set if not already in environment
                if key not in os.environ:
                    os.environ[key] = value
                    loaded_count += 1
                else:
                    log.debug(f"load_env_file_if_present: skipping {key} (already set)")

        if loaded_count > 0:
            log.info(f"load_env_file_if_present: loaded {loaded_count} variables from {env_path}")
        else:
            log.debug(f"load_env_file_if_present: no new variables loaded from {env_path}")

    except PermissionError as e:
        log.warning(f"load_env_file_if_present: permission denied reading .env: {e}")

    except OSError as e:
        log.warning(f"load_env_file_if_present: OS error reading .env: {e}")

    except Exception as e:
        log.warning(f"load_env_file_if_present: unexpected error: {e}", exc_info=True)
