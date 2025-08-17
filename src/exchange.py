import os
import logging
from typing import Dict, List, Optional, Tuple

import ccxt
from tenacity import retry, stop_after_attempt, wait_exponential

from .config import ExchangeCfg

log = logging.getLogger("exchange")

class ExchangeWrapper:
    def __init__(self, cfg: ExchangeCfg):
        self.cfg = cfg
        api_key = os.getenv("BYBIT_API_KEY", "")
        secret = os.getenv("BYBIT_API_SECRET", "")
        klass = getattr(ccxt, cfg.id)

        self.x = klass({
            "apiKey": api_key,
            "secret": secret,
            "enableRateLimit": True,
            "options": {
                "defaultType": "swap" if cfg.account_type == "swap" else "spot",
            },
        })
        if cfg.testnet and hasattr(self.x, "set_sandbox_mode"):
            self.x.set_sandbox_mode(True)

    def close(self):
        try:
            self.x.close()
        except Exception:
            pass

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    def load_markets(self):
        return self.x.load_markets()

    def fetch_markets_filtered(self) -> List[str]:
        markets = self.load_markets()
        symbols = []
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
            return symbols

        # Liquidity filter via tickers (24h quote volume)
        try:
            ticks = self.x.fetch_tickers(symbols)
        except Exception:
            return []
        keep = []
        for s in symbols:
            t = ticks.get(s, {})
            last = t.get("last") or t.get("close") or 0.0
            qv = t.get("quoteVolume", 0.0) or 0.0
            if (last or 0) >= self.cfg.min_price and qv >= self.cfg.min_usd_volume_24h:
                keep.append(s)
        keep = sorted(keep)
        if self.cfg.max_symbols and len(keep) > self.cfg.max_symbols:
            keep = keep[: self.cfg.max_symbols]
        log.info(f"Universe after filters: {len(keep)} symbols")
        return keep

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int):
        return self.x.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)

    def fetch_tickers(self, symbols: List[str]):
        try:
            return self.x.fetch_tickers(symbols)
        except Exception:
            return {}

    def fetch_positions_map(self) -> Dict[str, dict]:
        try:
            pos = self.x.fetch_positions()
        except Exception:
            pos = []
        out = {}
        for p in pos:
            sym = p.get("symbol")
            if not sym:
                continue
            out[sym] = p
        return out

    def fetch_balance_usdt(self) -> float:
        try:
            bal = self.x.fetch_balance()
            usdt = bal.get(self.cfg.quote, {})
            return float(usdt.get("total") or usdt.get("free") or 0.0)
        except Exception:
            return 0.0

    def fetch_price(self, symbol: str) -> Optional[float]:
        try:
            t = self.x.fetch_ticker(symbol)
            return t.get("last") or t.get("close")
        except Exception:
            return None

    def get_precision(self, symbol: str) -> Tuple[float, float]:
        m = self.x.market(symbol)
        amt = m.get("precision", {}).get("amount", None)
        price = m.get("precision", {}).get("price", None)
        return amt or 0.0001, price or 0.0001

    def quantize(self, symbol: str, amount: float, price: Optional[float]):
        q_amount = float(self.x.amount_to_precision(symbol, amount))
        q_price = None if price is None else float(self.x.price_to_precision(symbol, price))
        return q_amount, q_price

    def create_order_safe(self, symbol: str, side: str, amount: float, price: Optional[float], post_only: bool, reduce_only: bool):
        params = {}
        if reduce_only:
            params["reduceOnly"] = True
        if post_only:
            params["timeInForce"] = "PostOnly"
        if price is None:
            return self.x.create_market_order(symbol, side, abs(amount), params)
        else:
            return self.x.create_limit_order(symbol, side, abs(amount), price, params)

    def try_set_leverage(self, symbol: str, leverage: int):
        try:
            if hasattr(self.x, "setLeverage"):
                self.x.setLeverage(leverage, symbol, params={})
                log.info(f"Set leverage {leverage}x for {symbol}")
        except Exception as e:
            log.debug(f"setLeverage not supported or failed for {symbol}: {e}")
