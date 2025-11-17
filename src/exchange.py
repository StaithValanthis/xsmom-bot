# v1.6.4 – 2025-09-04 (CCXT-only; added fetch_order_book wrapper)
from __future__ import annotations
import logging
import os
from typing import Any, Dict, Iterable, List, Optional

import ccxt
from tenacity import retry, stop_after_attempt, wait_exponential
from .config import ExchangeCfg
from .risk_controller import APICircuitBreaker

log = logging.getLogger("exchange")


class ExchangeWrapper:
    """
    CCXT-only unified wrapper for Bybit USDT-perp.
    Public methods stable for live.py/backtester.
    """

    def __init__(self, cfg: ExchangeCfg, risk_cfg=None):
        """
        Initialize ExchangeWrapper.
        
        Args:
            cfg: ExchangeCfg
            risk_cfg: Optional RiskCfg for circuit breaker config
        """
        self.cfg = cfg
        
        # Circuit breaker for API failures (MAKE MONEY hardening)
        # Get config from risk_cfg if provided, else default
        if risk_cfg:
            cb_config = getattr(risk_cfg, "api_circuit_breaker", {}) or {}
            if isinstance(cb_config, dict):
                self.circuit_breaker = APICircuitBreaker(
                    max_errors=int(cb_config.get("max_errors", 5)),
                    window_seconds=int(cb_config.get("window_seconds", 300)),
                    cooldown_seconds=int(cb_config.get("cooldown_seconds", 600)),
                ) if cb_config.get("enabled", True) else None
            else:
                self.circuit_breaker = None
        else:
            self.circuit_breaker = None

        # ---- API keys from env (unchanged) ----
        api_key = os.getenv("BYBIT_API_KEY") or os.getenv("API_KEY")
        secret = os.getenv("BYBIT_API_SECRET") or os.getenv("API_SECRET")
        if not api_key or not secret:
            log.warning("API keys missing. Private endpoints may fail; equity may appear as 0.")

        klass = getattr(ccxt, cfg.id)
        self.x = klass({
            "apiKey": api_key,
            "secret": secret,
            "enableRateLimit": True,
            "timeout": 20000,
            "options": {
                "defaultType": "swap" if cfg.account_type == "swap" else "spot",
            },
        })
        if cfg.testnet and hasattr(self.x, "set_sandbox_mode"):
            self.x.set_sandbox_mode(True)

        # Bybit UTA hints
        self.unified_margin = bool(getattr(cfg, "unified_margin", False))
        if self.x.id == "bybit" and self.unified_margin:
            opts = getattr(self.x, "options", {}) or {}
            opts.update({
                "defaultType": "swap",
                "accountType": "UNIFIED",
                "fetchBalance": {"accountType": "UNIFIED", "coin": self.cfg.quote},
            })
            self.x.options = opts

        try:
            self.x.load_markets()
        except Exception as e:
            log.warning(f"load_markets failed: {e}")

        log.info(
            "CCXT init: id=%s, testnet=%s, defaultType=%s, accountType=%s, unified_margin=%s",
            self.x.id,
            bool(getattr(self.x, 'sandbox', False)),
            getattr(self.x, 'options', {}).get('defaultType'),
            getattr(self.x, 'options', {}).get('accountType'),
            self.unified_margin,
        )

    # ------------------------ Markets / Universe ------------------------

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    def load_markets(self):
        return self.x.load_markets()

    def fetch_markets_filtered(self) -> List[str]:
        markets = self.load_markets()
        symbols: List[str] = []

        for sym, m in markets.items():
            if m.get("active") is not True:
                continue
            if self.cfg.only_perps:
                if (m.get("swap") is True) and m.get("quote") == self.cfg.quote:
                    if m.get("type") == "swap" or m.get("contract", False):
                        if m.get("settle") in (self.cfg.quote, None) and m.get("linear", True):
                            symbols.append(sym)
            else:
                if m.get("spot") is True and m.get("quote") == self.cfg.quote:
                    symbols.append(sym)

        if not symbols:
            log.warning("No symbols after basic market filters.")
            return symbols

        try:
            ticks = self.x.fetch_tickers(symbols)
        except Exception as e:
            log.warning(f"fetch_tickers failed: {e}")
            return []

        keep = []
        for s in symbols:
            t = ticks.get(s, {}) or {}
            last = t.get("last") or t.get("close") or 0.0
            qv = t.get("quoteVolume", 0.0) or 0.0
            if (last or 0) >= self.cfg.min_price and qv >= self.cfg.min_usd_volume_24h:
                keep.append(s)

        keep = sorted(keep)
        if self.cfg.max_symbols and len(keep) > self.cfg.max_symbols:
            keep = keep[: self.cfg.max_symbols]
        log.info(f"Universe after filters: {len(keep)} symbols")
        return keep

    # ------------------------ Market Data ------------------------

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int, since: Optional[int] = None):
        """
        Fetch OHLCV data with automatic pagination for limits > 1000.
        
        Bybit limits single requests to 1000 bars. This method automatically
        paginates to fetch more data when limit > 1000.
        
        Args:
            symbol: Symbol to fetch (e.g., 'BTC/USDT:USDT')
            timeframe: Bar timeframe (e.g., '1h', '5m')
            limit: Number of bars to fetch (will paginate if > 1000)
            since: Optional start timestamp in milliseconds (if None, fetches most recent)
        
        Returns:
            List of [timestamp, open, high, low, close, volume] arrays
        """
        # Bybit API limit per request
        MAX_BARS_PER_REQUEST = 1000
        
        if limit <= MAX_BARS_PER_REQUEST:
            # Single request is sufficient
            return self.x.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit, since=since)
        
        # Need pagination: fetch in chunks going backwards in time
        log.debug(f"Fetching {limit} bars for {symbol} (paginating, max {MAX_BARS_PER_REQUEST} per request)")
        
        all_bars = []
        remaining = limit
        
        # Calculate timeframe duration in milliseconds
        timeframe_ms = self._timeframe_to_ms(timeframe)
        if timeframe_ms is None:
            log.warning(f"Unknown timeframe {timeframe}, falling back to single request")
            return self.x.fetch_ohlcv(symbol, timeframe=timeframe, limit=min(limit, MAX_BARS_PER_REQUEST), since=since)
        
        # CCXT returns oldest-first. To get most recent N bars:
        # Strategy: Fetch chunks going backwards in time, then reverse to get chronological order
        # 1. First chunk: since=None gets most recent 1000 bars
        # 2. Subsequent chunks: Use oldest timestamp from previous chunk to fetch older data
        # 3. Prepend older chunks, then take the last N bars (most recent)
        
        chunks = []  # Store chunks (will be in reverse chronological order)
        current_since = since  # None means most recent
        
        while remaining > 0:
            # Fetch up to MAX_BARS_PER_REQUEST bars
            chunk_limit = min(remaining, MAX_BARS_PER_REQUEST)
            
            try:
                chunk = self.x.fetch_ohlcv(
                    symbol,
                    timeframe=timeframe,
                    limit=chunk_limit,
                    since=current_since
                )
                
                if not chunk:
                    break
                
                chunks.append(chunk)
                remaining -= len(chunk)
                
                if len(chunk) < chunk_limit:
                    # Got fewer bars than requested, no more data available
                    break
                
                # Move backwards in time for next chunk
                # Use the oldest timestamp from this chunk (first element, since CCXT returns oldest-first)
                if chunk:
                    oldest_timestamp = chunk[0][0]  # First element's timestamp (oldest in chunk)
                    # Calculate start time for next (older) chunk
                    # Go back by (chunk_limit * timeframe_ms) to avoid overlap
                    current_since = oldest_timestamp - (chunk_limit * timeframe_ms)
                    # Ensure we don't go negative
                    if current_since < 0:
                        break
                
            except Exception as e:
                log.warning(f"Pagination chunk failed for {symbol}: {e}")
                break
        
        # Combine chunks: chunks are in reverse order (newest first), so reverse and concatenate
        # Then take the last N bars to get most recent
        if chunks:
            # Reverse chunks list (oldest chunk first), then flatten
            all_bars = []
            for chunk in reversed(chunks):
                all_bars.extend(chunk)
            
            # Trim to exact limit (keep most recent N bars)
            # Since all_bars is now oldest-first, take the last N elements
            if len(all_bars) > limit:
                all_bars = all_bars[-limit:]
        
        log.debug(f"Fetched {len(all_bars)} bars for {symbol} (requested {limit})")
        return all_bars
    
    def _timeframe_to_ms(self, timeframe: str) -> Optional[int]:
        """Convert timeframe string to milliseconds."""
        # Common timeframes
        timeframe_map = {
            '1m': 60 * 1000,
            '3m': 3 * 60 * 1000,
            '5m': 5 * 60 * 1000,
            '15m': 15 * 60 * 1000,
            '30m': 30 * 60 * 1000,
            '1h': 60 * 60 * 1000,
            '2h': 2 * 60 * 60 * 1000,
            '4h': 4 * 60 * 60 * 1000,
            '6h': 6 * 60 * 60 * 1000,
            '12h': 12 * 60 * 60 * 1000,
            '1d': 24 * 60 * 60 * 1000,
            '1w': 7 * 24 * 60 * 60 * 1000,
        }
        return timeframe_map.get(timeframe)

    def fetch_tickers(self, symbols: Iterable[str]) -> Dict[str, dict]:
        syms = list(symbols)
        try:
            return self.x.fetch_tickers(syms)
        except Exception as e:
            log.debug(f"fetch_tickers error: {e}")
            return {}

    def fetch_order_book(self, symbol: str, limit: int = 10) -> dict:
        try:
            return self.x.fetch_order_book(symbol, limit=limit)
        except Exception as e:
            log.debug(f"fetch_order_book error: {e}")
            return {}

    def fetch_funding_rates(self, symbols: List[str]) -> Dict[str, float]:
        out: Dict[str, float] = {}
        try:
            if hasattr(self.x, "fetch_funding_rates"):
                data = self.x.fetch_funding_rates(symbols)
                for d in data or []:
                    sym = d.get("symbol")
                    rate = float(d.get("fundingRate") or 0.0)
                    if sym:
                        out[sym] = rate
            elif hasattr(self.x, "fetch_funding_rate"):
                for s in symbols:
                    d = self.x.fetch_funding_rate(s)
                    out[s] = float(d.get("fundingRate") or 0.0)
        except Exception as e:
            log.debug(f"fetch_funding_rates failed: {e}")
        return out

    # ------------------------ Account / Positions ------------------------

    def get_equity_usdt(self) -> float:
        try:
            bal = self.x.fetch_balance(params={"accountType": "UNIFIED"} if self.unified_margin else {})
            total = bal.get("total", {})
            usdt_equity = float(total.get("USDT", 0.0))
            if usdt_equity == 0.0:
                # Try Bybit unified balance path structure
                usdt_equity = float(bal.get("info", {}).get("result", {}).get("list", [{}])[0].get("totalEquity", 0.0))
            if self.circuit_breaker:
                self.circuit_breaker.record_success()
            return max(0.0, usdt_equity)
        except Exception as e:
            log.warning(f"fetch_balance failed: {e}")
            if self.circuit_breaker:
                self.circuit_breaker.record_error()
            return 0.0
    
    def get_margin_ratio(self) -> Optional[Dict[str, float]]:
        """
        Get margin ratio information.
        
        Returns:
            Dict with 'equity', 'used_margin', 'available_margin', 'margin_ratio' (usage %)
            or None if unavailable
        """
        try:
            bal = self.x.fetch_balance(params={"accountType": "UNIFIED"} if self.unified_margin else {})
            
            # Try standard CCXT structure
            total = bal.get("total", {})
            used = bal.get("used", {})
            free = bal.get("free", {})
            
            equity = float(total.get("USDT", 0.0))
            used_margin = float(used.get("USDT", 0.0))
            available = float(free.get("USDT", 0.0))
            
            # Try Bybit-specific structure
            if equity == 0.0:
                info = bal.get("info", {}).get("result", {}).get("list", [{}])
                if info:
                    equity = float(info[0].get("totalEquity", 0.0))
                    used_margin = float(info[0].get("totalUsedBalance", 0.0))
                    available = float(info[0].get("totalAvailableBalance", 0.0))
            
            if equity <= 0:
                return None
            
            margin_ratio_pct = (used_margin / equity) * 100.0 if equity > 0 else 0.0
            
            if self.circuit_breaker:
                self.circuit_breaker.record_success()
            
            return {
                "equity": equity,
                "used_margin": used_margin,
                "available_margin": available,
                "margin_ratio_pct": margin_ratio_pct,
            }
        except Exception as e:
            log.warning(f"get_margin_ratio failed: {e}")
            if self.circuit_breaker:
                self.circuit_breaker.record_error()
            return None

    def fetch_positions(self) -> Dict[str, dict]:
        consolidated: Dict[str, dict] = {}
        try:
            raw = self.x.fetch_positions() or []
            if self.circuit_breaker:
                self.circuit_breaker.record_success()
            for p in raw:
                s = p.get("symbol")
                if not s:
                    continue
                side = (p.get("side") or "").lower()
                qty = float(p.get("contracts") or p.get("contractSize") or p.get("positionAmt") or 0.0)
                ep = None
                try:
                    ep = float(p.get("entryPrice") or 0.0) or None
                except Exception:
                    ep = None
                c = consolidated.setdefault(
                    s,
                    {"long_qty": 0.0, "short_qty": 0.0, "net_qty": 0.0, "entryPrice": None},
                )
                if side == "long":
                    c["long_qty"] += abs(qty)
                elif side == "short":
                    c["short_qty"] += abs(qty)
                else:
                    if qty > 0:
                        c["long_qty"] += abs(qty)
                    elif qty < 0:
                        c["short_qty"] += abs(qty)
                if ep:
                    c["entryPrice"] = ep
            for s, c in consolidated.items():
                c["net_qty"] = float(c["long_qty"]) - float(c["short_qty"])
                if c["entryPrice"] is None:
                    c["entryPrice"] = 0.0
            return consolidated
        except Exception as e:
            log.debug(f"fetch_positions error: {e}")
            if self.circuit_breaker:
                self.circuit_breaker.record_error()
            return consolidated

    # ------------------------ Trading ------------------------

    def set_leverage(self, symbol: str, lev: int):
        try:
            if hasattr(self.x, "set_leverage"):
                self.x.set_leverage(lev, symbol, params={"buyLeverage": lev, "sellLeverage": lev})
        except Exception as e:
            log.debug(f"set_leverage({symbol},{lev}) failed: {e}")

    def _limit_price_from_side(self, side: str, mid: float, bps: int) -> float:
        off = (bps / 10_000.0) * mid
        return (mid - off) if side.lower() == "buy" else (mid + off)

    def create_order_safe(
        self,
        symbol: str,
        side: str,
        size: float,
        price: Optional[float],
        *,
        post_only: bool = False,
        reduce_only: bool = False,
    ):
        if abs(size) <= 0:
            return None

        params: Dict[str, Any] = {"reduceOnly": reduce_only}
        try:
            if self.cfg.account_type == "swap":
                if price is None:
                    return self.x.create_order(symbol, "market", side.lower(), abs(size), None, params)
                else:
                    params["postOnly"] = post_only
                    return self.x.create_order(symbol, "limit", side.lower(), abs(size), float(price), params)
            else:
                if price is None:
                    return self.x.create_market_order(symbol, side.lower(), abs(size), params=params)
                else:
                    params["postOnly"] = post_only
                    return self.x.create_limit_order(symbol, side.lower(), abs(size), float(price), params)
        except Exception as e:
            log.debug(f"create_order failed: {e}")
            raise

    # ---- Open Orders helpers (used by startup cancel and ops) ----

    def fetch_open_orders(self, symbol: Optional[str] = None) -> List[dict]:
        try:
            return self.x.fetch_open_orders(symbol)
        except Exception as e:
            log.debug(f"fetch_open_orders failed: {e}")
            return []

    def cancel_order_safe(self, order_id: str, symbol: str) -> None:
        try:
            self.x.cancel_order(order_id, symbol)
        except Exception as e:
            log.debug(f"cancel_order_safe({order_id}, {symbol}) failed: {e}")
            raise

    # --- Compatibility alias for live.py (which calls ex.cancel_order) ---
    def cancel_order(self, order_id: str, symbol: str) -> None:
        """Alias for backward compatibility with live.py and maintenance scripts."""
        return self.cancel_order_safe(order_id, symbol)

    def cancel_all_orders(self, symbol: Optional[str] = None) -> Any:
        try:
            return self.x.cancel_all_orders(symbol)
        except Exception as e:
            log.debug(f"cancel_all_orders failed: {e}")
            raise

    # ------------------------ Cleanup ------------------------

    def close(self):
        try:
            self.x.close()
        except Exception:
            pass
