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

        # Base client
        self.x = klass(
            {
                "apiKey": api_key,
                "secret": secret,
                "enableRateLimit": True,
                "timeout": 20000,
                "options": {
                    # trading perps by default when account_type == "swap"
                    "defaultType": "swap" if cfg.account_type == "swap" else "spot",
                },
            }
        )

        # UNIFIED Trading Account (UTA) hint for Bybit (very important for balances)
        # If you are on Bybit UTA, ccxt needs accountType='UNIFIED' for v5 balance/positions.
        self.unified_margin = bool(getattr(cfg, "unified_margin", False))
        if self.x.id == "bybit" and self.unified_margin:
            opts = getattr(self.x, "options", {}) or {}
            opts.update(
                {
                    "defaultType": "swap",  # we trade linear perps
                    "accountType": "UNIFIED",
                    # ensure fetchBalance passes proper params by default
                    "fetchBalance": {"accountType": "UNIFIED"},
                }
            )
            self.x.options = opts  # assign back

        # Testnet
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
                # USDT linear perps
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

        # Liquidity filter via tickers (24h quote volume)
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

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int):
        return self.x.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)

    def fetch_tickers(self, symbols: List[str]):
        try:
            return self.x.fetch_tickers(symbols)
        except Exception as e:
            log.debug(f"fetch_tickers error: {e}")
            return {}

    def fetch_positions_map(self) -> Dict[str, dict]:
        """
        Map of symbol -> position dict.
        Under UTA, pass accountType='UNIFIED' to ensure correct wallet.
        """
        try:
            params = {"accountType": "UNIFIED"} if (self.x.id == "bybit" and self.unified_margin) else {}
            pos = self.x.fetch_positions(params=params)
        except Exception as e:
            log.debug(f"fetch_positions failed: {e}")
            pos = []
        out: Dict[str, dict] = {}
        for p in pos:
            sym = p.get("symbol")
            if sym:
                out[sym] = p
        return out

    def _parse_unified_equity(self, bal: dict) -> Optional[float]:
        """
        For Bybit UTA, ccxt may not populate bal['USDT'].
        Try bal['total'] then raw info payload (result->list[0]).
        """
        # 1) Standard 'total' mapping
        try:
            total = bal.get("total") or {}
            # Prefer USDT if present, else USD-equivalent if exposed
            if "USDT" in total and total["USDT"] is not None:
                return float(total["USDT"])
            if "USD" in total and total["USD"] is not None:
                return float(total["USD"])
        except Exception:
            pass

        # 2) Raw info fallbacks (common UTA shape)
        try:
            info = bal.get("info") or {}
            # bybit v5 often: {'result': {'list': [{'totalEquity': '...', 'totalAvailableBalance': '...'}]}}
            res = info.get("result") or {}
            lst = res.get("list") or []
            if lst:
                row = lst[0]
                for key in ("totalAvailableBalance", "availableBalance", "totalEquity"):
                    if key in row and row[key] is not None:
                        return float(row[key])
        except Exception:
            pass

        return None

    def fetch_balance_usdt(self) -> float:
        """
        Fetch equity in USDT terms. For Bybit UTA, request accountType='UNIFIED' and
        parse using multiple fallbacks in case per-asset buckets are not populated.
        """
        try:
            params = {}
            if self.x.id == "bybit" and self.unified_margin:
                params["accountType"] = "UNIFIED"

            bal = self.x.fetch_balance(params)

            # First, try the explicit quote bucket (works on classic accounts)
            try:
                usdt_bucket = bal.get(self.cfg.quote, {}) or {}
                usdt_direct = usdt_bucket.get("total") or usdt_bucket.get("free")
                if usdt_direct is not None:
                    v = float(usdt_direct)
                    if v > 0:
                        return v
            except Exception:
                pass

            # Unified fallbacks
            if self.x.id == "bybit" and self.unified_margin:
                uni = self._parse_unified_equity(bal)
                if uni is not None:
                    return float(uni)

            # Last resort: try any 'total' value aggregation
            try:
                total = bal.get("total") or {}
                if self.cfg.quote in total and total[self.cfg.quote] is not None:
                    return float(total[self.cfg.quote])
                # could be mapped under 'USD' on unified accounts
                if "USD" in total and total["USD"] is not None:
                    return float(total["USD"])
            except Exception:
                pass

            return 0.0
        except Exception as e:
            log.debug(f"fetch_balance_usdt failed: {e}")
            return 0.0

    def fetch_price(self, symbol: str) -> Optional[float]:
        try:
            t = self.x.fetch_ticker(symbol)
            px = t.get("last") or t.get("close")
            return float(px) if px is not None else None
        except Exception as e:
            log.debug(f"fetch_price error {symbol}: {e}")
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

    def create_order_safe(
        self,
        symbol: str,
        side: str,
        amount: float,
        price: Optional[float],
        post_only: bool,
        reduce_only: bool,
    ):
        params = {}
        if reduce_only:
            params["reduceOnly"] = True
        if post_only:
            # CCXT Bybit supports "PostOnly" via timeInForce param on v5 routes
            params["timeInForce"] = "PostOnly"
        if price is None:
            return self.x.create_market_order(symbol, side, abs(amount), params)
        else:
            return self.x.create_limit_order(symbol, side, abs(amount), price, params)

    def try_set_leverage(self, symbol: str, leverage: int):
        try:
            # Some ccxt versions expose setLeverage, newer ones may require set_margin_mode+set_leverage per market
            if hasattr(self.x, "setLeverage"):
                self.x.setLeverage(leverage, symbol, params={"buyLeverage": leverage, "sellLeverage": leverage})
                log.info(f"Set leverage {leverage}x for {symbol}")
            elif hasattr(self.x, "set_leverage"):
                self.x.set_leverage(leverage, symbol, params={"buyLeverage": leverage, "sellLeverage": leverage})
                log.info(f"Set leverage {leverage}x for {symbol}")
        except Exception as e:
            log.debug(f"setLeverage not supported or failed for {symbol}: {e}")
