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
        if not api_key or not secret:
            log.warning(
                "BYBIT_API_KEY/SECRET not found in environment. "
                "Private endpoints (balances, orders) will fail; equity will appear as 0. "
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

        # Unified Trading Account (UTA) setup for Bybit
        self.unified_margin = bool(getattr(cfg, "unified_margin", False))
        if self.x.id == "bybit" and self.unified_margin:
            opts = getattr(self.x, "options", {}) or {}
            opts.update(
                {
                    "defaultType": "swap",
                    "accountType": "UNIFIED",
                    # ensure CCXT uses these params when it builds balance requests internally
                    "fetchBalance": {"accountType": "UNIFIED", "coin": self.cfg.quote},
                }
            )
            self.x.options = opts

        # Testnet toggle
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
                # USDT linear perps (swap)
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

    # -------- Market Data --------

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int):
        return self.x.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)

    def fetch_tickers(self, symbols: List[str]):
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

    # -------- Positions --------

    def fetch_positions_map(self) -> Dict[str, dict]:
        """
        Map of symbol -> position dict.
        Under UTA, pass accountType='UNIFIED' (and settle='USDT' defensively).
        """
        try:
            params = {}
            if self.x.id == "bybit" and self.unified_margin:
                params = {"accountType": "UNIFIED", "settle": self.cfg.quote}
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

    # -------- Balance (robust UTA support) --------

    def _parse_unified_equity_from_info(self, info: dict) -> Optional[float]:
        """
        Parse Bybit v5 wallet response (privateGetV5AccountWalletBalance).
        Typical shape:
          {'retCode':0,'result':{'list':[{'totalAvailableBalance':'...','totalEquity':'...'}]}}
        """
        try:
            res = (info or {}).get("result") or {}
            lst = res.get("list") or []
            if lst:
                row = lst[0]
                for key in ("totalAvailableBalance", "availableBalance", "totalEquity"):
                    v = row.get(key)
                    if v is not None:
                        return float(v)
        except Exception:
            pass
        return None

    def _bybit_raw_wallet_balance(self) -> Optional[float]:
        """
        Fallback: call Bybit v5 wallet balance endpoint directly via ccxt's raw method
        when fetch_balance() does not surface UTA equity.
        """
        if self.x.id != "bybit":
            return None
        method_name = "privateGetV5AccountWalletBalance"
        if not hasattr(self.x, method_name):
            return None
        try:
            method = getattr(self.x, method_name)
            params = {"accountType": "UNIFIED", "coin": self.cfg.quote}
            data = method(params)
            val = self._parse_unified_equity_from_info(data)
            if val is not None:
                return float(val)
        except Exception as e:
            log.debug(f"raw bybit wallet balance call failed: {e}")
        return None

    def fetch_balance_usdt(self) -> float:
        """
        Fetch equity in USDT terms.
        For Bybit UTA, send params and include a direct v5 fallback if needed.
        """
        try:
            params = {}
            if self.x.id == "bybit" and self.unified_margin:
                params = {"accountType": "UNIFIED", "coin": self.cfg.quote}

            bal = self.x.fetch_balance(params)

            # 1) Try explicit quote bucket (classic accounts or when CCXT fills it)
            try:
                usdt_bucket = bal.get(self.cfg.quote, {}) or {}
                usdt_direct = usdt_bucket.get("total") or usdt_bucket.get("free")
                if usdt_direct is not None:
                    v = float(usdt_direct)
                    if v > 0:
                        return v
            except Exception:
                pass

            # 2) Unified-friendly totals exposed via CCXT mapping
            if self.x.id == "bybit" and self.unified_margin:
                try:
                    total = bal.get("total") or {}
                    if self.cfg.quote in total and total[self.cfg.quote] is not None:
                        v = float(total[self.cfg.quote])
                        if v >= 0:
                            return v
                    if "USD" in total and total["USD"] is not None:
                        v = float(total["USD"])
                        if v >= 0:
                            return v
                except Exception:
                    pass

                # 3) Parse raw info blob
                uni = self._parse_unified_equity_from_info(bal.get("info") or {})
                if uni is not None:
                    return float(uni)

                # 4) FINAL FALLBACK: call raw v5 wallet endpoint directly
                raw = self._bybit_raw_wallet_balance()
                if raw is not None:
                    return float(raw)

            # Non-unified or no data
            try:
                total = bal.get("total") or {}
                if self.cfg.quote in total and total[self.cfg.quote] is not None:
                    return float(total[self.cfg.quote])
                if "USD" in total and total["USD"] is not None:
                    return float(total["USD"])
            except Exception:
                pass

            return 0.0
        except Exception as e:
            # Make this LOUD so you see missing keys/permission issues
            log.warning(
                "fetch_balance_usdt failed (returning 0.0). "
                "This usually means missing BYBIT_API_KEY/SECRET or a Bybit permission problem: %s", e
            )
            return 0.0

    # -------- Precision / Orders / Leverage --------

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
            params["timeInForce"] = "PostOnly"
        if price is None:
            return self.x.create_market_order(symbol, side, abs(amount), params)
        else:
            return self.x.create_limit_order(symbol, side, abs(amount), price, params)

    def try_set_leverage(self, symbol: str, leverage: int):
        try:
            if hasattr(self.x, "setLeverage"):
                self.x.setLeverage(leverage, symbol, params={"buyLeverage": leverage, "sellLeverage": leverage})
                log.info(f"Set leverage {leverage}x for {symbol}")
            elif hasattr(self.x, "set_leverage"):
                self.x.set_leverage(leverage, symbol, params={"buyLeverage": leverage, "sellLeverage": leverage})
                log.info(f"Set leverage {leverage}x for {symbol}")
        except Exception as e:
            log.debug(f"setLeverage not supported or failed for {symbol}: {e}")
