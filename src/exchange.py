# v1.6.2 – 2025-08-22 (CCXT-only; cleaned; startup-safe helpers kept)
from __future__ import annotations
import logging
import os
from typing import Any, Dict, Iterable, List, Optional

import ccxt
from tenacity import retry, stop_after_attempt, wait_exponential
from .config import ExchangeCfg

log = logging.getLogger("exchange")


class ExchangeWrapper:
    """
    CCXT-only unified wrapper for Bybit USDT-perp.
    Public methods stable for live.py/backtester.
    """

    def __init__(self, cfg: ExchangeCfg):
        self.cfg = cfg

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
    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int):
        return self.x.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)

    def fetch_tickers(self, symbols: Iterable[str]) -> Dict[str, dict]:
        syms = list(symbols)
        try:
            return self.x.fetch_tickers(syms)
        except Exception as e:
            log.debug(f"fetch_tickers error: {e}")
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
            return max(0.0, usdt_equity)
        except Exception as e:
            log.warning(f"fetch_balance failed: {e}")
            return 0.0

    def fetch_positions(self) -> Dict[str, dict]:
        consolidated: Dict[str, dict] = {}
        try:
            raw = self.x.fetch_positions() or []
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
