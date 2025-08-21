# v1.2.2 – 2025-08-21
from __future__ import annotations
import logging
import os
from typing import Dict, List, Optional, Tuple

import ccxt
from tenacity import retry, stop_after_attempt, wait_exponential

from .config import ExchangeCfg

log = logging.getLogger("exchange")

class ExchangeWrapper:
    def __init__(self, cfg: ExchangeCfg):
        self.cfg = cfg
        api_key = os.getenv("BYBIT_API_KEY") or os.getenv("API_KEY")
        secret = os.getenv("BYBIT_API_SECRET") or os.getenv("API_SECRET")

        if not api_key or not secret:
            log.warning(
                "API keys missing. Private endpoints (balances, orders) will fail; equity will appear as 0. "
                "Set them in .env or export them before running."
            )

        klass = getattr(ccxt, cfg.id)
        self.x = klass(
            {
                "apiKey": api_key,
                "secret": secret,
                "enableRateLimit": True,
                "timeout": 20000,
                "options": {
                    "defaultType": "swap" if cfg.account_type == "swap" else "spot",
                },
            }
        )

        # Bybit Unified Trading Account (UTA)
        self.unified_margin = bool(getattr(cfg, "unified_margin", False))
        if self.x.id == "bybit" and self.unified_margin:
            opts = getattr(self.x, "options", {}) or {}
            opts.update(
                {
                    "defaultType": "swap",
                    "accountType": "UNIFIED",
                    "fetchBalance": {"accountType": "UNIFIED", "coin": self.cfg.quote},
                }
            )
            self.x.options = opts

        # testnet toggle
        if cfg.testnet and hasattr(self.x, "set_sandbox_mode"):
            self.x.set_sandbox_mode(True)

        log.info(
            f"CCXT init: id={self.x.id}, testnet={getattr(self.x, 'sandbox', False)}, "
            f"defaultType={getattr(self.x, 'options', {}).get('defaultType')}, "
            f"accountType={getattr(self.x, 'options', {}).get('accountType')}, "
            f"unified_margin={self.unified_margin}"
        )

    def close(self):
        try:
            self.x.close()
        except Exception:
            pass

    # -------- Markets / Universe --------

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

        # Liquidity filter via 24h quote volume
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

    # -------- Market Data --------

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int):
        return self.x.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)

    def fetch_tickers(self, symbols: List[str]) -> Dict[str, dict]:
        try:
            return self.x.fetch_tickers(symbols)
        except Exception as e:
            log.debug(f"fetch_tickers error: {e}")
            return {}

    def fetch_price(self, symbol: str) -> Optional[float]:
        try:
            t = self.x.fetch_ticker(symbol)
            px = t.get("last") or t.get("close")
            return float(px) if px is not None else None
        except Exception as e:
            log.debug(f"fetch_price error {symbol}: {e}")
            return None

    def fetch_funding_rates(self, symbols: List[str]) -> Dict[str, float]:
        out: Dict[str, float] = {}
        try:
            if hasattr(self.x, "fetch_funding_rates"):
                data = self.x.fetch_funding_rates(symbols)
            elif hasattr(self.x, "fetch_funding_rate"):
                data = [self.x.fetch_funding_rate(s) for s in symbols]
            else:
                return out
            for d in data or []:
                try:
                    sym = d.get("symbol")
                    rate = float(d.get("fundingRate") or 0.0)
                    if sym:
                        out[sym] = rate
                except Exception:
                    pass
        except Exception as e:
            log.debug(f"fetch_funding_rates failed: {e}")
        return out

    # -------- Account / Positions --------

    def get_equity_usdt(self) -> float:
        """Returns total account equity denominated in USDT (best-effort)."""
        try:
            bal = self.x.fetch_balance(params={"accountType": "UNIFIED"} if self.unified_margin else {})
            total = bal.get("total", {})
            usdt_equity = float(total.get("USDT", 0.0))
            if usdt_equity == 0.0:
                # fallback for some ccxt versions
                usdt_equity = float(bal.get("info", {}).get("result", {}).get("list", [{}])[0].get("totalEquity", 0.0))
            return max(0.0, usdt_equity)
        except Exception as e:
            log.warning(f"fetch_balance failed: {e}")
            return 0.0

    def fetch_positions(self) -> Dict[str, dict]:
        """
        Consolidate per-symbol long/short into a single net position:
          result[symbol] = {
            'long_qty', 'short_qty', 'net_qty',
            'entryPrice_long', 'entryPrice_short', 'entryPrice' (dominant side)
          }
        Works around exchanges (e.g., Bybit UTA) that return two rows per symbol.
        """
        consolidated: Dict[str, dict] = {}
        try:
            raw = self.x.fetch_positions() or []
            for p in raw:
                s = p.get("symbol")
                if not s:
                    continue
                side = (p.get("side") or "").lower()  # 'long' / 'short' / maybe ''
                qty = float(p.get("contracts") or p.get("contractSize") or p.get("positionAmt") or 0.0)
                if qty == 0:
                    # some adapters put signed qty in 'contracts' with side="long", keep positive
                    # still record entry price if needed
                    pass
                ep = None
                try:
                    ep = float(p.get("entryPrice") or 0.0) or None
                except Exception:
                    ep = None

                c = consolidated.setdefault(
                    s,
                    {
                        "long_qty": 0.0,
                        "short_qty": 0.0,
                        "net_qty": 0.0,
                        "entryPrice_long": None,
                        "entryPrice_short": None,
                        "entryPrice": None,
                    },
                )

                if side == "long":
                    c["long_qty"] += abs(qty)
                    if ep:
                        c["entryPrice_long"] = ep
                elif side == "short":
                    c["short_qty"] += abs(qty)
                    if ep:
                        c["entryPrice_short"] = ep
                else:
                    # Some brokers provide signed qty without side
                    if qty > 0:
                        c["long_qty"] += abs(qty)
                        if ep:
                            c["entryPrice_long"] = ep
                    elif qty < 0:
                        c["short_qty"] += abs(qty)
                        if ep:
                            c["entryPrice_short"] = ep

            # finalize
            for s, c in consolidated.items():
                c["net_qty"] = float(c["long_qty"]) - float(c["short_qty"])
                c["entryPrice"] = c["entryPrice_long"] if c["net_qty"] > 0 else (c["entryPrice_short"] if c["net_qty"] < 0 else None)

            return consolidated
        except Exception as e:
            log.debug(f"fetch_positions error: {e}")
            return consolidated

    # -------- Trading --------

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
        """Wraps CCXT order APIs sanely across modes."""
        side = side.lower()
        if abs(size) <= 0:
            return None

        params = {"reduceOnly": reduce_only}
        if self.cfg.account_type == "swap":
            # ensure futures/swap createOrder
            if price is None:
                # market
                return self.x.create_order(symbol, "market", side, abs(size), None, params)
            else:
                # limit
                params["postOnly"] = post_only
                return self.x.create_order(symbol, "limit", side, abs(size), float(price), params)
        else:
            # spot fallback
            if price is None:
                return self.x.create_market_order(symbol, side, abs(size), params=params)
            else:
                params["postOnly"] = post_only
                return self.x.create_limit_order(symbol, side, abs(size), float(price), params)
