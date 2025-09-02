# v1.7.7 – 2025-09-03
# Fix: prevent MEXC spot-private fetch_currencies during load_markets (10072 "Api key info invalid")
# - Force swap-only market load and hard-disable fetch_currencies for MEXC
# - Keep hardened MEXC submit behavior, precision, min-notional, leverage, and logging

from __future__ import annotations
import logging
import os
import json
import time
import uuid
from typing import Any, Dict, Iterable, List, Optional, Tuple

import ccxt
from tenacity import retry, stop_after_attempt, wait_exponential
from .config import ExchangeCfg

log = logging.getLogger("exchange")


class ExchangeWrapper:
    """
    CCXT-only unified wrapper for USDT-perp trading on Bybit or MEXC.
    Public methods stable for live.py/backtester.
    """

    def __init__(self, cfg: ExchangeCfg):
        self.cfg = cfg
        ex_id = str(cfg.id).lower()

        # ---- API keys from env (exchange-aware) ----
        if ex_id == "mexc":
            api_key = os.getenv("MEXC_API_KEY") or os.getenv("API_KEY")
            secret  = os.getenv("MEXC_API_SECRET") or os.getenv("API_SECRET")
            key_src = "MEXC_API_*" if (os.getenv("MEXC_API_KEY") and os.getenv("MEXC_API_SECRET")) else "API_*"
        else:
            api_key = os.getenv("BYBIT_API_KEY") or os.getenv("API_KEY")
            secret  = os.getenv("BYBIT_API_SECRET") or os.getenv("API_SECRET")
            key_src = "BYBIT_API_*" if (os.getenv("BYBIT_API_KEY") and os.getenv("BYBIT_API_SECRET")) else "API_*"

        if not api_key or not secret:
            log.warning("API keys missing. Private endpoints may fail; equity may appear as 0.")
        else:
            log.info("Using API key source: %s", key_src)

        klass = getattr(ccxt, cfg.id)
        self.x = klass({
            "apiKey": api_key,
            "secret": secret,
            "enableRateLimit": True,
            "timeout": 20000,
            "options": {
                "defaultType": "swap" if cfg.account_type == "swap" else "spot",
                # keep CCXT from nagging about currencies on MEXC
                **({"warnOnFetchCurrencies": False} if ex_id == "mexc" else {}),
            },
        })

        # Best-effort sandbox (testnet) toggle
        try:
            if cfg.testnet and hasattr(self.x, "set_sandbox_mode"):
                self.x.set_sandbox_mode(True)
        except Exception:
            pass

        # Bybit UTA hints
        self.unified_margin = bool(getattr(cfg, "unified_margin", False))
        if self.x.id == "bybit" and self.unified_margin:
            opts = getattr(self.x, "options", {}) or {}
            opts.update({
                "defaultType": "swap",
                "fetchBalance": {"coin": self.cfg.quote},
            })
            self.x.options = opts

        # ---------- HARD BLOCK MEXC fetch_currencies ----------
        if ex_id == "mexc":
            try:
                # Ensure we never attempt the spot-private /capital/config/getall call
                self.x.has["fetchCurrencies"] = False
            except Exception:
                pass
            try:
                # Monkey-patch to a no-op in case CCXT still tries to call it
                def _no_fetch_currencies(_params=None):
                    return {}
                setattr(self.x, "fetch_currencies", _no_fetch_currencies)  # type: ignore[attr-defined]
            except Exception:
                pass
            # Make absolutely sure defaultType sticks to swap
            try:
                opts = getattr(self.x, "options", {}) or {}
                opts["defaultType"] = "swap"
                self.x.options = opts
            except Exception:
                pass
        # ------------------------------------------------------

        # Load markets (prefer swap)
        try:
            load_params = {"type": "swap"} if self.cfg.account_type == "swap" else {}
            self.x.load_markets(params=load_params)
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

        # cache for set_leverage-once per symbol
        self._lev_bootstrapped: Dict[str, bool] = {}

        # memo for mexc check
        self._is_mexc = (ex_id == "mexc")

    # ------------------------ Markets / Universe ------------------------

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    def load_markets(self):
        params = {"type": "swap"} if self.cfg.account_type == "swap" else {}
        try:
            if self._is_mexc:
                self.x.has["fetchCurrencies"] = False  # belt-and-braces
        except Exception:
            pass
        return self.x.load_markets(params=params)

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
        try:
            return self.x.fetch_tickers(list(symbols))
        except Exception:
            return {}

    def fetch_funding_rates(self, symbols: List[str]) -> Dict[str, float]:
        out: Dict[str, float] = {}
        try:
            if getattr(self.x, "has", {}).get("fetchFundingRates"):
                data = self.x.fetch_funding_rates(symbols)
                for d in data or []:
                    sym = d.get("symbol")
                    if sym:
                        out[sym] = float(d.get("fundingRate") or 0.0)
            elif getattr(self.x, "has", {}).get("fetchFundingRate"):
                for s in symbols:
                    d = self.x.fetch_funding_rate(s)
                    out[s] = float((d or {}).get("fundingRate") or 0.0)
        except Exception:
            pass
        return out

    # ------------------------ Account / Positions ------------------------

    def get_equity_usdt(self) -> float:
        try:
            ex_id = getattr(self.x, "id", "")
            params: Dict[str, Any] = {}
            if ex_id == "bybit" and self.unified_margin:
                params = {"accountType": "UNIFIED"}
            bal = self.x.fetch_balance(params=params)
            total = bal.get("total", {})
            usdt_equity = float(total.get("USDT", 0.0))
            if usdt_equity == 0.0 and ex_id == "bybit":
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
                try:
                    ep = float(p.get("entryPrice") or 0.0) or None
                except Exception:
                    ep = None
                c = consolidated.setdefault(s, {"long_qty": 0.0, "short_qty": 0.0, "net_qty": 0.0, "entryPrice": None})
                if side == "long":
                    c["long_qty"] += abs(qty)
                elif side == "short":
                    c["short_qty"] += abs(qty)
                else:
                    if qty > 0: c["long_qty"] += abs(qty)
                    elif qty < 0: c["short_qty"] += abs(qty)
                if ep:
                    c["entryPrice"] = ep
            for s, c in consolidated.items():
                c["net_qty"] = float(c["long_qty"]) - float(c["short_qty"])
                if c["entryPrice"] is None:
                    c["entryPrice"] = 0.0
            return consolidated
        except Exception:
            return consolidated

    # ------------------------ Precision & Limits ------------------------

    def _market_meta(self, symbol: str) -> Dict[str, Any]:
        try: return self.x.market(symbol)
        except Exception: return {}

    def _precise_amount(self, symbol: str, amount: float) -> float:
        try: return float(self.x.amount_to_precision(symbol, amount))
        except Exception: return float(amount)

    def _precise_price(self, symbol: str, price: float) -> float:
        try: return float(self.x.price_to_precision(symbol, price))
        except Exception: return float(price)

    def _min_limits(self, symbol: str) -> Tuple[float, float]:
        m = self._market_meta(symbol)
        lim = (m.get("limits", {}) or {})
        return float((lim.get("amount", {}) or {}).get("min", 0) or 0), float((lim.get("cost", {}) or {}).get("min", 0) or 0)

    def _min_notional_fallback(self, symbol: str) -> float:
        return 5.0 if self._is_mexc else 0.0  # conservative default for MEXC futures

    def _best_bid_ask(self, symbol: str) -> Tuple[float, float]:
        try:
            ob = self.x.fetch_order_book(symbol, limit=5) or {}
            bid = ob.get("bids", [[None]])[0][0]
            ask = ob.get("asks", [[None]])[0][0]
            return float(bid or 0.0), float(ask or 0.0)
        except Exception:
            return 0.0, 0.0

    # ------------------------ Trading ------------------------

    def _ensure_lev_once(self, symbol: str, lev: int = 1) -> None:
        if getattr(self, "_lev_bootstrapped", {}).get(symbol): return
        try:
            if getattr(self.x, "has", {}).get("setLeverage") or hasattr(self.x, "set_leverage"):
                try: self.x.set_leverage(lev, symbol, params={"buyLeverage": lev, "sellLeverage": lev})
                except AttributeError: self.x.setLeverage(lev, symbol, params={"buyLeverage": lev, "sellLeverage": lev})  # type: ignore
        except Exception as e:
            log.debug(f"bootstrap set_leverage({symbol},{lev}) failed: {e}")
        if not hasattr(self, "_lev_bootstrapped"):
            self._lev_bootstrapped = {}
        self._lev_bootstrapped[symbol] = True

    def set_leverage(self, symbol: str, lev: int):
        try:
            if getattr(self.x, "has", {}).get("setLeverage") or hasattr(self.x, "set_leverage"):
                try: self.x.set_leverage(lev, symbol, params={"buyLeverage": lev, "sellLeverage": lev})
                except AttributeError: self.x.setLeverage(lev, symbol, params={"buyLeverage": lev, "sellLeverage": lev})  # type: ignore
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

        self._ensure_lev_once(symbol, lev=1)

        # snap to precision + ensure min-notional
        q = self._precise_amount(symbol, abs(size))
        p = None if price is None else self._precise_price(symbol, float(price))
        min_qty, min_cost = self._min_limits(symbol)
        min_cost = max(float(min_cost or 0.0), self._min_notional_fallback(symbol))
        if q < max(min_qty, 0.0):
            q = self._precise_amount(symbol, max(q, min_qty))
        if p is not None and (q * p) < min_cost:
            q = self._precise_amount(symbol, max(q, (min_cost / max(p, 1e-12))))

        params: Dict[str, Any] = {}
        s_lower = side.lower()
        if reduce_only:
            params["reduceOnly"] = True  # MEXC futures ignores this; we also map side below

        try:
            if self._is_mexc and self.cfg.account_type == "swap":
                # MEXC futures params
                params["openType"] = 2 if getattr(self.cfg, "margin_mode", "cross") == "cross" else 1
                if reduce_only:
                    params["side"] = 2 if s_lower == "buy" else 4
                else:
                    params["side"] = 1 if s_lower == "buy" else 3
                params.pop("reduceOnly", None)
                params["externalOid"] = f"xsmom-{int(time.time()*1000)}-{uuid.uuid4().hex[:8]}"

                if p is None:
                    params["type"] = 5  # market
                    return self._mexc_create_and_confirm(symbol, "market", s_lower, q, None, params)
                else:
                    params["type"] = 1  # limit (post-only disabled)
                    return self._mexc_create_and_confirm(symbol, "limit", s_lower, q, p, params)

            # Non-MEXC path
            if self.cfg.account_type == "swap":
                if p is None:
                    return self.x.create_order(symbol, "market", s_lower, q, None, params)
                else:
                    params.setdefault("timeInForce", "GTC")
                    if post_only:
                        params["postOnly"] = True
                    return self.x.create_order(symbol, "limit", s_lower, q, p, params)
            else:
                if p is None:
                    return self.x.create_market_order(symbol, s_lower, q, params=params)
                else:
                    params.setdefault("timeInForce", "GTC")
                    if post_only:
                        params["postOnly"] = True
                    return self.x.create_limit_order(symbol, s_lower, q, p, params)

        except Exception as e:
            self._log_order_error(symbol, s_lower, q, p, params, e)
            raise

    # ---------- MEXC submit + confirmation ----------

    def _mexc_create_and_confirm(self, symbol, otype, side, amount, price, params):
        """
        Submit via CCXT; if CCXT raises but HTTP body says success with orderId, synthesize order.
        """
        try:
            resp = self.x.create_order(symbol, otype, side, amount, price, params)
            body = getattr(self.x, "last_http_response", None)
            if body:
                try:
                    j = json.loads(body)
                    if j.get("success") is False:
                        msg = j.get("message") or j.get("msg")
                        code = j.get("code")
                        raise ccxt.ExchangeError(f"MEXC submit success=false (code={code}, msg={msg})")
                except Exception:
                    pass
            return resp
        except Exception as e:
            try:
                body = getattr(self.x, "last_http_response", None)
                if body:
                    j = json.loads(body)
                    if j.get("success") is True and j.get("code") == 0 and j.get("data"):
                        order_id = j.get("data")
                        return {
                            "id": str(order_id),
                            "symbol": symbol,
                            "type": otype,
                            "side": side,
                            "amount": float(amount),
                            "price": None if price is None else float(price),
                            "status": "open",
                            "info": j,
                            "clientOrderId": params.get("externalOid"),
                        }
            except Exception:
                pass
            self._log_order_error(symbol, side, amount, price, params, e)
            raise

    def _log_order_error(self, symbol, side, amount, price, params, e):
        body = None
        try:
            if hasattr(e, "response") and getattr(e, "response") is not None:
                body = getattr(e.response, "text", None)
            if body is None:
                body = getattr(e, "body", None)
            if body is None:
                body = getattr(self.x, "last_http_response", None)
        except Exception:
            body = None

        extras = []
        try:
            if body:
                j = json.loads(body)
                if "success" in j: extras.append(f"success={j.get('success')}")
                if "code" in j:    extras.append(f"code={j.get('code')}")
                if "errorCode" in j and j.get("errorCode") not in (None, 0):
                    extras.append(f"errorCode={j.get('errorCode')}")
                msg = j.get("message") or j.get("msg")
                if msg:            extras.append(f"msg={msg}")
        except Exception:
            pass

        log.error(
            "Order error %s %s %s%s | params=%s",
            symbol,
            side,
            (amount if price is None else f"{amount}@{price}"),
            (" | mexc " + " ".join(extras)) if extras else "",
            {k: v for k, v in (params or {}).items() if v is not None},
        )

    # ---------- Open orders / cancel ----------

    def fetch_open_orders(self, symbol: Optional[str] = None) -> List[dict]:
        try:
            return self.x.fetch_open_orders(symbol)
        except Exception:
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
        try: self.x.close()
        except Exception: pass

    # ------------------------ Helpers (venue-agnostic) ------------------------

    def get_funding_rate(self, symbol: str):
        try:
            if not getattr(self.x, "has", {}).get("fetchFundingRate"):
                return None
            fr = self.x.fetch_funding_rate(symbol)
            return fr.get("fundingRate") if isinstance(fr, dict) else None
        except Exception:
            return None

    def get_min_limits(self, symbol: str):
        try:
            m = self.x.market(symbol)
            lim = (m.get("limits", {}) or {})
            return float((lim.get("amount", {}) or {}).get("min", 0) or 0), float((lim.get("cost", {}) or {}).get("min", 0) or 0)
        except Exception:
            return 0.0, 0.0
