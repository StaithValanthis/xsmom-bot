"""
Historical OHLCV cache using SQLite.

Provides persistent storage for OHLCV data to reduce API calls.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from datetime import datetime, timezone
import pandas as pd

log = logging.getLogger("data.cache")


class OHLCVCache:
    """
    SQLite-based cache for OHLCV historical data.
    
    Stores bars with (symbol, timeframe, timestamp) as primary key.
    Supports gap-filling and TTL awareness.
    """
    
    def __init__(self, db_path: str | Path):
        """
        Initialize OHLCV cache.
        
        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._create_tables()
        log.info(f"OHLCVCache initialized at {self.db_path}")
    
    def _create_tables(self):
        """Create cache table if it doesn't exist."""
        cursor = self.conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ohlcv (
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                ts INTEGER NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume REAL NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (symbol, timeframe, ts)
            )
        """)
        
        # Index for efficient range queries
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_symbol_tf_ts 
            ON ohlcv(symbol, timeframe, ts)
        """)
        
        self.conn.commit()
    
    def get_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None,
    ) -> List[List]:
        """
        Retrieve OHLCV data from cache.
        
        Args:
            symbol: Symbol (e.g., "BTC/USDT:USDT")
            timeframe: Timeframe (e.g., "1h")
            start_ts: Start timestamp (milliseconds, optional)
            end_ts: End timestamp (milliseconds, optional)
        
        Returns:
            List of bars in CCXT format: [[ts, open, high, low, close, volume], ...]
        """
        cursor = self.conn.cursor()
        
        query = "SELECT ts, open, high, low, close, volume FROM ohlcv WHERE symbol = ? AND timeframe = ?"
        params = [symbol, timeframe]
        
        if start_ts is not None:
            query += " AND ts >= ?"
            params.append(start_ts)
        
        if end_ts is not None:
            query += " AND ts <= ?"
            params.append(end_ts)
        
        query += " ORDER BY ts ASC"
        
        cursor.execute(query, params)
        rows = cursor.fetchall()
        
        # Convert to CCXT format: [[ts, open, high, low, close, volume], ...]
        bars = [[row["ts"], row["open"], row["high"], row["low"], row["close"], row["volume"]] for row in rows]
        
        return bars
    
    def store_ohlcv(self, symbol: str, timeframe: str, bars: List[List]):
        """
        Store OHLCV data into cache.
        
        Args:
            symbol: Symbol (e.g., "BTC/USDT:USDT")
            timeframe: Timeframe (e.g., "1h")
            bars: List of bars in CCXT format: [[ts, open, high, low, close, volume], ...]
        """
        if not bars:
            return
        
        cursor = self.conn.cursor()
        
        # Use INSERT OR REPLACE to handle duplicates
        cursor.executemany("""
            INSERT OR REPLACE INTO ohlcv 
            (symbol, timeframe, ts, open, high, low, close, volume, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            (symbol, timeframe, bar[0], bar[1], bar[2], bar[3], bar[4], bar[5], datetime.now(timezone.utc).isoformat())
            for bar in bars
        ])
        
        self.conn.commit()
        log.debug(f"Stored {len(bars)} bars for {symbol} {timeframe}")
    
    def get_cached_range(
        self,
        symbol: str,
        timeframe: str,
        start_ts: int,
        end_ts: int,
    ) -> Tuple[Optional[int], Optional[int]]:
        """
        Determine what portion of the requested range is cached.
        
        Returns:
            Tuple of (cached_start_ts, cached_end_ts) or (None, None) if no cache
        """
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT MIN(ts) as min_ts, MAX(ts) as max_ts
            FROM ohlcv
            WHERE symbol = ? AND timeframe = ? AND ts >= ? AND ts <= ?
        """, (symbol, timeframe, start_ts, end_ts))
        
        row = cursor.fetchone()
        if row and row["min_ts"] is not None and row["max_ts"] is not None:
            return (int(row["min_ts"]), int(row["max_ts"]))
        return (None, None)
    
    def close(self):
        """Close database connection."""
        self.conn.close()
        log.info(f"OHLCVCache connection closed for {self.db_path}")


def get_cache_instance(db_path: str | Path) -> Optional[OHLCVCache]:
    """
    Factory function to get cache instance if enabled.
    
    Args:
        db_path: Path to database file
    
    Returns:
        OHLCVCache instance or None if path is empty/disabled
    """
    if not db_path:
        return None
    
    try:
        return OHLCVCache(db_path)
    except Exception as e:
        log.warning(f"Failed to initialize OHLCV cache: {e}")
        return None

