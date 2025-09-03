# exchange.py
# v1.13.1 — 2025-09-03
#
# XSMOM-BOT ExchangeWrapper — Bybit & MEXC (USDT linear swaps)
#
# Key behaviors:
# - MEXC time sync + recv-window header (default 60000ms).
# - Idempotent submit via externalOid; on timeout we NEVER re-post:
#     1) parse last_http_response and synthesize if success;
#     2) confirm via GET order/external/{symbol}/{externalOid} with longer polling;
#     3) fallback: open-orders scan (paginated), then recent history scan.
# - MEXC quantity is contracts (vol). If MEXC_AMOUNT_MODE=base, convert base→contracts
#   using contractSize and round to volScale step (floor), min 1 step.
# - Duplicate submit suppressor (8s window).
#
# Env:
#   MEXC_AMOUNT_MODE=base|contracts   (default: contracts)
#   MEXC_RECV_WINDOW=60000            (ms, max 60000)
#   MEXC_OPEN_TYPE=1|2                (1=isolated, 2=cross; default 2)
#   MEXC_RETRY_IOC=1                  (optional one-shot IOC retry if confirm fails)
#
from __future__ import annotations

import json
import logging
import math
import os
import time
import uuid
from typing import Any, Dict, Iterable, List, Optional, Tuple

import ccxt  # type: ignore
from tenacity import retry, stop_after_attempt, wait_exponential

log = logging.getLogger("exchange")

# Optional project config shim
try:
    from .config import ExchangeCfg  # type: ignore
except Exception:
    class ExchangeCfg:  # minimal stub
        id: str = "mexc"               # "mexc" | "bybit"
        quote: str = "USDT"
        only_perps: bool = True
        account_type: str = "swap"     # "swap" or "spot"
        min_price: float = 0.0
        min_usd_volume_24h: float = 0.0
        max_symbols: int = 0
        testnet: bool = False
        unified_margin: bool = False
        margin_mode: str = "cross"
        leverage: Optional[int] = None
        default_leverage: Optional[int] = None


class ExchangeWrapper:
    # ------------------------ Init ------------------------
    def __init__(self, cfg: ExchangeCfg):
        self.cfg = cfg
        ex_id = str(cfg.id).lower()

        # Keys
        if ex_id == "mexc":
            api_key = os.getenv("MEXC_API_KEY") or os.getenv("API_KEY")
            secret = os.getenv("MEXC_API_SECRET") or os.getenv("API_SECRET")
            key_src = "MEXC_API_*" if (os.getenv("MEXC_API_KEY") and os.getenv("MEXC_API_SECRET")) else "API_*"
        else:
            api_key = os.getenv("BYBIT_API_KEY") or os.getenv("API_KEY")
            secret = os.getenv("BYBIT_API_SECRET") or os.getenv("API_SECRET")
            key_src = "BYBIT_API_*" if (os.getenv("BYBIT_API_KEY") and os.getenv("BYBIT_API_SECRET")) else "API_*"

        if api_key and secret:
            log.info("Using API key source: %s", key_src)
        else:
            log.warning("API keys missing. Private endpoints may fail; equity may appear as 0.")

        Klass = getattr(ccxt, cfg.id)
        timeout_ms = 45000 if ex_id == "mexc" else 20000
        self.x = Klass(
            {
                "apiKey": api_key,
                "secret": secret,
                "enableRateLimit": True,
                "timeout": timeout_ms,
                "options": {
                    "defaultType": "swap" if cfg.account_type == "swap" else "spot",
                    **({"warnOnFetchCurrencies": False} if ex_id == "mexc" else {}),
                },
            }
        )

        # MEXC: recv-window + time sync
        if ex_id == "mexc":
            try:
                hdrs = getattr(self.x, "headers", {}) or {}
                hdrs["recv-window"] = os.getenv("MEXC_RECV_WINDOW", "60000")  # max per docs
                self.x.headers = hdrs
            except Exception:
                pass
            self._mexc_sync_time()

        # Testnet
        try:
            if cfg.testnet and hasattr(self.x, "set_sandbox_mode"):
                self.x.set_sandbox_mode(True)
        except Exception:
            pass

        # Bybit unified margin hints
        self.unified_margin = bool(getattr(cfg, "unified_margin", False))
        if self.x.id == "bybit" and self.unified_margin:
            opts = getattr(self.x, "options", {}) or {}
            opts.update({"defaultType": "swap", "fetchBalance": {"coin": getattr(cfg, "quote", "USDT")}})
            self.x.options = opts

        # Avoid flaky fetch_currencies on MEXC
        if ex_id == "mexc":
            try:
                self.x.has["fetchCurrencies"] = False
            except Exception:
                pass
            try:
                def _no_fetch_currencies(_params=None):
                    return {}
                setattr(self.x, "fetch_currencies", _no_fetch_currencies)  # type: ignore[attr-defined]
            except Exception:
                pass
            try:
                opts = getattr(self.x, "options", {}) or {}
                opts["defaultType"] = "swap"
                self.x.options = opts
            except Exception:
                pass

        # Load markets
        try:
            self.x.load_markets(params={"type": "swap"} if cfg.account_type == "swap" else {})
        except Exception as e:
            log.warning(f"load_markets failed: {e}")

        log.info(
            "CCXT init: id=%s, testnet=%s, defaultType=%s, accountType=%s, unified_margin=%s",
            self.x.id,
            bool(getattr(self.x, "sandbox", False)),
            getattr(self.x, "options", {}).get("defaultType"),
            getattr(self.x, "options", {}).get("accountType"),
            self.unified_margin,
        )

        self._is_mexc = ex_id == "mexc"
        self._is_bybit = ex_id == "bybit"
        self._lev_bootstrapped: Dict[str, bool] = {}

        # in-flight anti-dup cache
        self._inflight: Dict[str, Dict[str, Any]] = {}

        if self._is_mexc:
            log.info("[MEXC] amount mode=%s", (os.getenv("MEXC_AMOUNT_MODE") or "contracts").lower())

    # ------------------------ MEXC helpers ------------------------
    def _mexc_sync_time(self) -> None:
        """Align signer time with MEXC server to reduce Request-Time/timeout issues."""
        try:
            server_ms: Optional[int] = None
            # futures ping
            try:
                j = self.x.fetch2("contract/ping", "contractPublic", "GET")  # type: ignore[attr-defined]
                if isinstance(j, dict):
                    server_ms = int(j.get("data") or 0) or None
            except Exception:
                pass
            # spot time fallback
            if server_ms is None:
                try:
                    j2 = self.x.fetch2("time", "public", "GET")
                    if isinstance(j2, dict):
                        server_ms = int(j2.get("serverTime") or 0) or None
                except Exception:
                    pass
            if server_ms:
                off = int(server_ms - int(time.time() * 1000))
                base_ms = getattr(self.x, "milliseconds", None)
                self.x.milliseconds = (lambda base=base_ms, off=off: int(time.time() * 1000 + off))
                log.info(f"[MEXC] time sync: offset={off}ms")
        except Exception as e:
            log.debug(f"[MEXC] time sync failed (non-fatal): {e}")

    def _mexc_open_type(self) -> int:
        """1=isolated, 2=cross (default)."""
        try:
            v = int(os.getenv("MEXC_OPEN_TYPE", "2"))
            return 1 if v == 1 else 2
        except Exception:
            return 2

    # ------------------------ Markets / Universe ------------------------
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    def load_markets(self):
        try:
            if self._is_mexc:
                self.x.has["fetchCurrencies"] = False
        except Exception:
            pass
        return self.x.load_markets(params={"type": "swap"} if self.cfg.account_type == "swap" else {})

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
            params: Dict[str, Any] = {}
            if self._is_bybit and self.unified_margin:
                params = {"accountType": "UNIFIED"}
            bal = self.x.fetch_balance(params=params)
            total = bal.get("total", {})
            usdt_equity = float(total.get("USDT", 0.0))
            if usdt_equity == 0.0 and self._is_bybit:
                usdt_equity = float(bal.get("info", {}).get("result", {}).get("list", [{}])[0].get("totalEquity", 0.0))
            return max(0.0, usdt_equity)
        except Exception as e:
            log.warning(f"fetch_balance failed: {e}")
            return 0.0

    def fetch_positions(self) -> Dict[str, dict]:
        """
        Normalize positions into:
            { symbol: { long_qty, short_qty, net_qty, entryPrice } }
        """
        consolidated: Dict[str, dict] = {}
        try:
            raw = self.x.fetch_positions() or []
        except Exception as e:
            log.debug(f"fetch_positions (exchange) failed: {e}")
            raw = []

        for p in raw:
            try:
                s = p.get("symbol") or p.get("info", {}).get("symbol")
                if not s:
                    continue
                side = (p.get("side") or "").lower()
                qty = float(p.get("contracts") or p.get("positionAmt") or p.get("size") or p.get("amount") or 0.0)
                entry = p.get("entryPrice")
                ep = float(entry) if entry not in (None, "") else 0.0

                c = consolidated.setdefault(s, {"long_qty": 0.0, "short_qty": 0.0, "net_qty": 0.0, "entryPrice": 0.0})
                if side == "long":
                    c["long_qty"] += abs(qty)
                elif side == "short":
                    c["short_qty"] += abs(qty)
                else:
                    if qty > 0:
                        c["long_qty"] += abs(qty)
                    elif qty < 0:
                        c["short_qty"] += abs(qty)
                if ep > 0:
                    c["entryPrice"] = ep
            except Exception:
                continue

        for s in list(consolidated.keys()):
            c = consolidated[s]
            c["net_qty"] = float(c.get("long_qty", 0.0)) - float(c.get("short_qty", 0.0))
            c["entryPrice"] = float(c.get("entryPrice") or 0.0)

        return consolidated

    # ------------------------ Precision & Limits ------------------------
    def _market_meta(self, symbol: str) -> Dict[str, Any]:
        try:
            return self.x.market(symbol)
        except Exception:
            return {}

    def _precise_amount(self, symbol: str, amount: float) -> float:
        try:
            return float(self.x.amount_to_precision(symbol, amount))
        except Exception:
            return float(amount)

    def _precise_price(self, symbol: str, price: float) -> float:
        try:
            return float(self.x.price_to_precision(symbol, price))
        except Exception:
            return float(price)

    def _amount_step(self, symbol: str) -> float:
        m = self._market_meta(symbol) or {}
        precision = m.get("precision") or {}
        amt_prec = precision.get("amount", None)
        step = None
        if isinstance(amt_prec, int) and amt_prec >= 0:
            step = 10 ** (-amt_prec)
        elif isinstance(amt_prec, float) and 0.0 < amt_prec < 1.0:
            step = amt_prec
        if not step or step <= 0:
            lim = (m.get("limits", {}) or {})
            min_amt = float((lim.get("amount", {}) or {}).get("min", 0.0) or 0.0)
            step = min_amt if min_amt > 0 else 1.0
        return float(step)

    def _ceil_to_step(self, value: float, step: float) -> float:
        if step <= 0:
            return value
        k = int((value + step - 1e-12) // step)
        return float(k * step)

    def _min_limits(self, symbol: str) -> Tuple[float, float]:
        m = self._market_meta(symbol)
        lim = (m.get("limits", {}) or {})
        return float((lim.get("amount", {}) or {}).get("min", 0) or 0), float((lim.get("cost", {}) or {}).get("min", 0) or 0)

    def _min_notional_fallback(self, symbol: str) -> float:
        return 5.0 if self._is_mexc else 0.0

    def _best_bid_ask(self, symbol: str) -> Tuple[float, float]:
        try:
            ob = self.x.fetch_order_book(symbol, limit=5) or {}
            bid = ob.get("bids", [[None]])[0][0]
            ask = ob.get("asks", [[None]])[0][0]
            return float(bid or 0.0), float(ask or 0.0)
        except Exception:
            return 0.0, 0.0

    # ------------------------ Sizing helpers ------------------------
    def _amount_mode(self) -> str:
        return (os.getenv("MEXC_AMOUNT_MODE") or "contracts").strip().lower()

    def _contracts_from_size(self, symbol: str, size: float) -> float:
        """
        Return contracts (vol).
        If MEXC & AMOUNT_MODE=base, convert base tokens → contracts using contractSize.
        Then round to volScale step (floor), min 1 step.
        """
        if not self._is_mexc or self.cfg.account_type != "swap":
            return size
        mode = self._amount_mode()
        try:
            m = self.x.market(symbol)
            csize = float(m.get("contractSize") or 1.0)
            vol_scale = int((m.get("info") or {}).get("volScale") or 0)
        except Exception:
            csize = 1.0
            vol_scale = 0

        qty = (size / csize) if mode == "base" else float(size)
        if vol_scale > 0:
            step = 10 ** (-vol_scale)
            qty = math.floor(qty / step) * step
            min_step = step
        else:
            qty = float(int(qty))
            min_step = 1.0

        if qty < min_step:
            qty = min_step

        log.info("[MEXC sizing] base→contracts: base_size=%s csize=%s => raw_contracts=%.8f", size, csize, qty)
        return qty

    # ------------------------ Leverage ------------------------
    def _ensure_lev_once(self, symbol: str, lev: int = 1) -> None:
        if getattr(self, "_lev_bootstrapped", {}).get(symbol):
            return
        try:
            if getattr(self.x, "has", {}).get("setLeverage") or hasattr(self.x, "set_leverage"):
                try:
                    self.x.set_leverage(lev, symbol, params={"buyLeverage": lev, "sellLeverage": lev})
                except AttributeError:
                    self.x.setLeverage(lev, symbol, params={"buyLeverage": lev, "sellLeverage": lev})  # type: ignore[attr-defined]
        except Exception as e:
            log.debug(f"bootstrap set_leverage({symbol},{lev}) failed: {e}")
        if not hasattr(self, "_lev_bootstrapped"):
            self._lev_bootstrapped = {}
        self._lev_bootstrapped[symbol] = True

    def set_leverage(self, symbol: str, lev: int):
        try:
            if getattr(self.x, "has", {}).get("setLeverage") or hasattr(self.x, "set_leverage"):
                try:
                    self.x.set_leverage(lev, symbol, params={"buyLeverage": lev, "sellLeverage": lev})
                except AttributeError:
                    self.x.setLeverage(lev, symbol, params={"buyLeverage": lev, "sellLeverage": lev})  # type: ignore[attr-defined]
        except Exception as e:
            log.debug(f"set_leverage({symbol},{lev}) failed: {e}")

    # ------------------------ Orders ------------------------
    def _gen_client_oid(self) -> str:
        return f"xsmom-{int(time.time()*1000)}-{uuid.uuid4().hex[:8]}"

    def _fingerprint(self, symbol: str, side: str, reduce_only: bool, price: Optional[float], qty: float) -> str:
        return f"{symbol}|{side}|{int(bool(reduce_only))}|{price if price is not None else 'MKT'}|{qty:.8f}"

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

        # Compute qty (contracts for swaps)
        raw_qty = abs(self._contracts_from_size(symbol, size))
        if self._is_mexc and self.cfg.account_type == "swap":
            q = raw_qty  # already rounded to volScale/int in _contracts_from_size
        else:
            step = self._amount_step(symbol)
            q = self._ceil_to_step(self._precise_amount(symbol, raw_qty), step)
            if q <= 0:
                q = step

        p = None if price is None else self._precise_price(symbol, float(price))

        # Min-notional guard for limit orders
        if p is not None:
            min_qty, min_cost = self._min_limits(symbol)
            min_cost = max(float(min_cost or 0.0), self._min_notional_fallback(symbol))
            notional = self._contracts_notional_usdt(symbol, q, p)
            if notional < min_cost and notional > 0:
                try:
                    m = self.x.market(symbol)
                    csize = float((m.get("contractSize") or 1.0) or 1.0)
                    vol_scale = int((m.get("info") or {}).get("volScale") or 0)
                except Exception:
                    csize = 1.0
                    vol_scale = 0
                need_contracts = (min_cost / max(csize * p, 1e-12))
                if self._is_mexc and self.cfg.account_type == "swap":
                    if vol_scale > 0:
                        step = 10 ** (-vol_scale)
                        q = math.ceil(need_contracts / step) * step
                    else:
                        q = float(math.ceil(need_contracts))
                else:
                    amt_step = self._amount_step(symbol)
                    q = self._ceil_to_step(max(q, need_contracts), amt_step)

        params: Dict[str, Any] = {}
        s_lower = side.lower()
        if reduce_only:
            params["reduceOnly"] = True  # supported one-way

        client_oid = self._gen_client_oid()
        params["clientOrderId"] = client_oid

        # ---- MEXC hardened path ----
        if self._is_mexc and self.cfg.account_type == "swap":
            open_type = self._mexc_open_type()
            params["openType"] = open_type

            lev = getattr(self.cfg, "leverage", None) or getattr(self.cfg, "default_leverage", None)
            if open_type == 1 and lev and not reduce_only:
                try:
                    params["leverage"] = int(lev)
                except Exception:
                    pass

            external_oid = client_oid
            params["externalOid"] = external_oid

            otype = "market" if p is None else "limit"
            params["type"] = 5 if p is None else 1  # numeric type per API

            # Anti-dup POST (8s)
            fp = self._fingerprint(symbol, s_lower, reduce_only, p, q)
            now = time.time()
            for k, v in list(self._inflight.items()):
                if now - v.get("ts", 0) > 15:
                    self._inflight.pop(k, None)

            if fp in self._inflight and (now - self._inflight[fp]["ts"] < 8):
                reuse_oid = self._inflight[fp]["oid"]
                log.info(
                    "[MEXC] duplicate call caught (%.2fs). Reusing externalOid=%s and confirming; no POST.",
                    now - self._inflight[fp]["ts"],
                    reuse_oid,
                )
                confirmed = self._mexc_confirm_by_external(symbol, reuse_oid, otype, s_lower, q, p, poll=True)
                if confirmed:
                    return confirmed
                recovered = self._recover_mexc_order_by_external_oid(symbol, reuse_oid)
                if recovered:
                    return recovered
                return {
                    "id": "",
                    "clientOrderId": reuse_oid,
                    "symbol": symbol,
                    "type": otype,
                    "side": s_lower,
                    "amount": float(q),
                    "price": None if p is None else float(p),
                    "status": "open",
                    "info": {"note": "pending-confirmation-no-repost"},
                }

            self._inflight[fp] = {"ts": now, "oid": external_oid}

            # Submit once
            try:
                order = self.x.create_order(symbol, otype, s_lower, float(q), None if p is None else float(p), params)
                out = self._ensure_id_from_info_or_http(symbol, otype, s_lower, q, p, order, params)
                return out
            except ccxt.RequestTimeout as e:
                # Try to synthesize from late body
                synth = self._synthesize_from_http_success(symbol, otype, s_lower, q, p, params)
                if synth:
                    return synth
                self._log_timeout_body(symbol, s_lower, q, p)
                log.error(
                    "Order timeout %s %s %s@%s | externalOid=%s — confirming (no re-post).",
                    symbol, s_lower, q, p if p is not None else "MKT", external_oid
                )
                confirmed = self._mexc_confirm_by_external(symbol, external_oid, otype, s_lower, q, p, poll=True)
                if confirmed:
                    log.info("[MEXC] confirmed by externalOid: id=%s status=%s", confirmed.get("id"), confirmed.get("status"))
                    return confirmed
                recovered = self._recover_mexc_order_by_external_oid(symbol, external_oid)
                if recovered:
                    log.info("[MEXC] recovered from open orders: id=%s", recovered.get("id"))
                    return recovered
                # Optional IOC retry
                try:
                    if os.getenv("MEXC_RETRY_IOC", "0") == "1":
                        params2 = dict(params)
                        params2["timeInForce"] = "IOC"
                        order2 = self.x.create_order(symbol, otype, s_lower, float(q), None if p is None else float(p), params2)
                        return self._ensure_id_from_info_or_http(symbol, otype, s_lower, q, p, order2, params2)
                except Exception:
                    pass
                self._log_order_error(symbol, s_lower, q, p, params, e)
                raise
            except Exception as e:
                # Late success body
                synth = self._synthesize_from_http_success(symbol, otype, s_lower, q, p, params)
                if synth:
                    return synth
                confirmed = self._mexc_confirm_by_external(symbol, external_oid, otype, s_lower, q, p, poll=True)
                if confirmed:
                    return confirmed
                recovered = self._recover_mexc_order_by_external_oid(symbol, external_oid)
                if recovered:
                    return recovered
                self._log_order_error(symbol, s_lower, q, p, params, e)
                raise

        # ---- Non-MEXC (Bybit etc.) ----
        try:
            if self.cfg.account_type == "swap":
                if p is None:
                    try:
                        return self.x.create_order(symbol, "market", s_lower, q, None, {"clientOrderId": client_oid})
                    except Exception as e:
                        recovered = self._recover_open_order_by_client_id(symbol, client_oid)
                        if recovered:
                            return recovered
                        self._log_order_error(symbol, s_lower, q, p, {"clientOrderId": client_oid}, e)
                        raise
                else:
                    params.setdefault("timeInForce", "GTC")
                    if post_only:
                        params["postOnly"] = True
                    try:
                        return self.x.create_order(symbol, "limit", s_lower, q, p, params)
                    except Exception as e:
                        recovered = self._recover_open_order_by_client_id(symbol, client_oid)
                        if recovered:
                            return recovered
                        self._log_order_error(symbol, s_lower, q, p, params, e)
                        raise
            else:
                # spot
                if p is None:
                    return self.x.create_market_order(symbol, s_lower, q, params={"clientOrderId": client_oid})
                else:
                    params.setdefault("timeInForce", "GTC")
                    if post_only:
                        params["postOnly"] = True
                    return self.x.create_limit_order(symbol, s_lower, q, p, params)
        except Exception as e:
            self._log_order_error(symbol, s_lower, q, p, params, e)
            raise

    # ------------------------ Helpers & Recoveries ------------------------
    def _recover_open_order_by_client_id(self, symbol: str, client_id: str) -> Optional[dict]:
        try:
            opens = self.fetch_open_orders(symbol) or []
            for od in opens:
                coid = od.get("clientOrderId") or (od.get("info") or {}).get("clientOrderId") \
                       or (od.get("info") or {}).get("orderLinkId")
                if coid and str(coid) == str(client_id):
                    return od
        except Exception:
            pass
        return None

    def _extract_mexc_order_id(self, j: Dict[str, Any]) -> Optional[str]:
        try:
            data = j.get("data")
            if isinstance(data, (str, int)):
                return str(data)
            if isinstance(data, dict):
                for k in ("orderId", "id", "order_id", "orderNo", "order_no"):
                    if k in data and data[k]:
                        return str(data[k])
            if isinstance(data, list) and data:
                first = data[0]
                if isinstance(first, (str, int)):
                    return str(first)
                if isinstance(first, dict):
                    for k in ("orderId", "id", "order_id", "orderNo", "order_no"):
                        if k in first and first[k]:
                            return str(first[k])
        except Exception:
            pass
        return None

    def _synthesize_from_http_success(
        self, symbol: str, otype: str, side: str, amount: float, price: Optional[float], params: Dict[str, Any]
    ) -> Optional[dict]:
        body = getattr(self.x, "last_http_response", None)
        if not body:
            return None
        try:
            j = json.loads(body)
            if j.get("success") is True and j.get("code") == 0:
                oid = self._extract_mexc_order_id(j)
                if oid:
                    return {
                        "id": str(oid),
                        "symbol": symbol,
                        "type": otype,
                        "side": side,
                        "amount": float(amount),
                        "price": None if price is None else float(price),
                        "status": "open",
                        "info": j,
                        "externalOid": params.get("externalOid"),
                    }
        except Exception:
            return None
        return None

    def _mexc_symbol_id(self, symbol: str) -> Optional[str]:
        try:
            m = self.x.market(symbol)
            mid = m.get("id")
            if mid:
                return str(mid)
        except Exception:
            pass
        try:
            base = symbol.split("/")[0]
            return f"{base}_USDT"
        except Exception:
            return None

    def _mexc_confirm_by_external(
        self,
        symbol: str,
        external_oid: Optional[str],
        otype: str,
        side: str,
        amount: float,
        price: Optional[float],
        *,
        poll: bool = False,
    ) -> Optional[dict]:
        if not external_oid:
            return None
        market_id = self._mexc_symbol_id(symbol)
        if not market_id:
            return None

        def _once() -> Optional[dict]:
            try:
                if hasattr(self.x, "contractPrivateGetOrderExternalSymbolExternalOid"):
                    j = self.x.contractPrivateGetOrderExternalSymbolExternalOid(  # type: ignore[attr-defined]
                        {"symbol": market_id, "externalOid": external_oid}
                    )
                else:
                    j = self.x.fetch2(
                        f"order/external/{market_id}/{external_oid}", api="contractPrivate", method="GET", params={}
                    )  # type: ignore[attr-defined]
                if isinstance(j, dict) and j.get("success") is True and j.get("code") == 0 and j.get("data"):
                    data = j["data"]
                    oid = str(data.get("orderId") or data.get("id") or "")
                    if oid:
                        return {
                            "id": oid,
                            "symbol": symbol,
                            "type": otype,
                            "side": side,
                            "amount": float(amount),
                            "price": None if price is None else float(price),
                            "status": data.get("state") or "open",
                            "info": j,
                            "externalOid": external_oid,
                        }
            except Exception:
                return None
            return None

        if not poll:
            return _once()

        # Poll longer: ~15s with easing intervals
        deadline = time.time() + 15.0
        delay = 0.4
        while time.time() < deadline:
            got = _once()
            if got:
                return got
            time.sleep(delay)
            delay = min(delay + 0.2, 1.0)

        # As a last resort, scan open orders (paginated) then recent history
        recovered = self._recover_mexc_order_by_external_oid(symbol, external_oid)
        if recovered:
            return recovered

        # Try recent history (last ~2 minutes)
        try:
            start_ms = int(time.time() * 1000) - 120_000
            if hasattr(self.x, "contractPrivateGetOrderListHistoryOrdersSymbol"):
                j = self.x.contractPrivateGetOrderListHistoryOrdersSymbol(  # type: ignore[attr-defined]
                    {"symbol": market_id, "page_num": 1, "page_size": 50, "start_time": start_ms}
                )
            else:
                j = self.x.fetch2(
                    f"order/list/history_orders/{market_id}",
                    api="contractPrivate",
                    method="GET",
                    params={"page_num": 1, "page_size": 50, "start_time": start_ms},
                )  # type: ignore[attr-defined]
            for row in (j or {}).get("data", []):
                if (row or {}).get("externalOid") == external_oid:
                    return {
                        "id": str(row.get("orderId") or ""),
                        "symbol": symbol,
                        "type": "limit" if int(row.get("orderType", 1)) in (1, 2, 3, 4) else "market",
                        "side": "buy" if int(row.get("side", 0)) in (1, 4) else "sell",
                        "amount": float(row.get("vol") or 0.0),
                        "price": float(row.get("price") or 0.0) or None,
                        "status": row.get("state") or "open",
                        "info": j,
                        "externalOid": external_oid,
                    }
        except Exception:
            pass

        return None

    def _recover_mexc_order_by_external_oid(self, symbol: str, external_oid: Optional[str]) -> Optional[dict]:
        if not external_oid:
            return None
        try:
            opens = self.fetch_open_orders(symbol) or []
            for od in opens:
                info = od.get("info") or {}
                if info.get("externalOid") == external_oid:
                    return {
                        "id": od.get("id") or info.get("orderId") or "",
                        "symbol": symbol,
                        "type": od.get("type") or "limit",
                        "side": od.get("side") or "",
                        "amount": float(od.get("amount") or 0.0),
                        "price": None if od.get("price") in (None, 0, "0") else float(od.get("price")),
                        "status": od.get("status") or "open",
                        "info": od,
                        "externalOid": external_oid,
                    }
        except Exception:
            pass
        # low-level open orders with pagination
        try:
            market_id = self._mexc_symbol_id(symbol)
            if market_id:
                if hasattr(self.x, "contractPrivateGetOrderListOpenOrdersSymbol"):
                    j = self.x.contractPrivateGetOrderListOpenOrdersSymbol(  # type: ignore[attr-defined]
                        {"symbol": market_id, "page_num": 1, "page_size": 50}
                    )
                else:
                    j = self.x.fetch2(
                        f"order/list/open_orders/{market_id}",
                        api="contractPrivate",
                        method="GET",
                        params={"page_num": 1, "page_size": 50},
                    )  # type: ignore[attr-defined]
                data = (j or {}).get("data") or []
                for row in data:
                    if (row or {}).get("externalOid") == external_oid:
                        return {
                            "id": str(row.get("orderId") or ""),
                            "symbol": symbol,
                            "type": "limit" if int(row.get("orderType", 1)) in (1, 2, 3, 4) else "market",
                            "side": "buy" if int(row.get("side", 0)) in (1, 4) else "sell",
                            "amount": float(row.get("vol") or 0.0),
                            "price": float(row.get("price") or 0.0) or None,
                            "status": row.get("state") or "open",
                            "info": j,
                            "externalOid": external_oid,
                        }
        except Exception:
            pass
        return None

    def fetch_open_orders(self, symbol: Optional[str] = None) -> List[dict]:
        try:
            if symbol:
                return self.x.fetch_open_orders(symbol)
            return self.x.fetch_open_orders()
        except Exception:
            return []

    # ------------------------ Misc ------------------------
    def _contracts_notional_usdt(self, symbol: str, contracts: float, px: Optional[float]) -> float:
        if px is None:
            return 0.0
        try:
            csize = float((self.x.market(symbol) or {}).get("contractSize") or 1.0)
            return float(contracts) * csize * float(px)
        except Exception:
            return float(contracts) * float(px)

    def _log_timeout_body(self, symbol, side, amount, price):
        try:
            body = getattr(self.x, "last_http_response", None)
            if body and isinstance(body, str):
                excerpt = body[:240].replace("\n", "")
                log.debug(f"[MEXC timeout body] {symbol} {side} {amount}@{price}: {excerpt}")
        except Exception:
            pass

    def _log_order_error(self, symbol, side, amount, price, params, exc: Exception):
        extras = ""
        try:
            body = getattr(self.x, "last_http_response", None)
            if body:
                try:
                    j = json.loads(body)
                    extras = f" | mexc success={j.get('success')} code={j.get('code')}"
                except Exception:
                    extras = ""
        except Exception:
            pass
        price_str = "MKT" if price is None else f"{price}"
        log.error(
            f"Order error {symbol} {side} {amount}@{price_str}{extras} | params={params} | {type(exc).__name__}: {exc}"
        )

    def close(self):
        try:
            if hasattr(self.x, "close"):
                self.x.close()
        except Exception:
            pass
