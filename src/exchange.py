# exchange.py
# v3.0.3 — 2025-09-03
#
# Clean MEXC (USDT-M futures) implementation + Bybit passthrough.
# - Uses CCXT implicit methods for MEXC contract API (avoids URL join bugs)
# - Direct submit + confirm-by-externalOid with backoff
# - Time sync + recv-window header; env timeouts
# - Base→contracts sizing via contractSize & volScale
# - Numeric side/type per MEXC docs
#
# Env knobs (defaults shown):
#   MEXC_HTTP_TIMEOUT_MS=90000
#   HTTP_TIMEOUT_MS=20000
#   MEXC_RECV_WINDOW=60000
#   MEXC_OPEN_TYPE=2           # 1=isolated, 2=cross
#   MEXC_AMOUNT_MODE=contracts # or base
#   MEXC_CONFIRM_TIMEOUT_SEC=60
#   MEXC_CONFIRM_BACKOFF_START_MS=300
#   MEXC_CONFIRM_BACKOFF_MULT=1.6
#   MEXC_CONFIRM_BACKOFF_MAX_MS=2500
#   MEXC_FORCE_NUMERIC_SIDE=1  # strongly recommended for MEXC futures
#   MEXC_RETRY_IOC=0           # optional one-shot IOC retry if needed
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

log = logging.getLogger("exchange")

# --- Minimal config shim (keep compatibility with your bot) ---
try:
    from .config import ExchangeCfg  # type: ignore
except Exception:
    class ExchangeCfg:
        id: str = "mexc"               # "mexc" | "bybit"
        quote: str = "USDT"
        only_perps: bool = True
        account_type: str = "swap"     # "swap" | "spot"
        min_price: float = 0.0
        min_usd_volume_24h: float = 0.0
        max_symbols: int = 0
        testnet: bool = False
        unified_margin: bool = False
        leverage: Optional[int] = None
        default_leverage: Optional[int] = None


class ExchangeWrapper:
    # ------------------------ Init ------------------------
    def __init__(self, cfg: ExchangeCfg):
        self.cfg = cfg
        ex_id = cfg.id.lower()
        self._is_mexc = ex_id == "mexc"
        self._is_bybit = ex_id == "bybit"
        self._inflight: Dict[str, Dict[str, Any]] = {}
        self._lev_bootstrapped: Dict[str, bool] = {}
        self.unified_margin = bool(getattr(cfg, "unified_margin", False))

        # Keys
        if self._is_mexc:
            api_key = os.getenv("MEXC_API_KEY") or os.getenv("API_KEY")
            secret  = os.getenv("MEXC_API_SECRET") or os.getenv("API_SECRET")
            key_src = "MEXC_API_*" if (os.getenv("MEXC_API_KEY") and os.getenv("MEXC_API_SECRET")) else "API_*"
        else:
            api_key = os.getenv("BYBIT_API_KEY") or os.getenv("API_KEY")
            secret  = os.getenv("BYBIT_API_SECRET") or os.getenv("API_SECRET")
            key_src = "BYBIT_API_*" if (os.getenv("BYBIT_API_KEY") and os.getenv("BYBIT_API_SECRET")) else "API_*"

        if api_key and secret:
            log.info("Using API key source: %s", key_src)
        else:
            log.warning("API keys missing. Private endpoints may fail; equity may appear as 0.")

        # Timeouts
        def _env_int(name: str, default: int) -> int:
            try:
                return int(os.getenv(name, str(default)))
            except Exception:
                return default
        timeout_ms = _env_int("MEXC_HTTP_TIMEOUT_MS", 90000) if self._is_mexc else _env_int("HTTP_TIMEOUT_MS", 20000)

        # CCXT client
        Klass = getattr(ccxt, cfg.id)
        self.x = Klass({
            "apiKey": api_key,
            "secret": secret,
            "enableRateLimit": True,
            "timeout": timeout_ms,
            "options": {
                "defaultType": "swap" if cfg.account_type == "swap" else "spot",
            },
        })

        # Testnet
        try:
            if cfg.testnet and hasattr(self.x, "set_sandbox_mode"):
                self.x.set_sandbox_mode(True)
        except Exception:
            pass

        # Bybit UTA hints
        if self._is_bybit and self.unified_margin:
            opts = getattr(self.x, "options", {}) or {}
            opts.update({"defaultType": "swap", "fetchBalance": {"coin": getattr(self.cfg, "quote", "USDT")}})
            self.x.options = opts

        # MEXC headers + time sync
        if self._is_mexc:
            try:
                hdrs = getattr(self.x, "headers", {}) or {}
                hdrs["recv-window"] = os.getenv("MEXC_RECV_WINDOW", "60000")
                self.x.headers = hdrs
            except Exception:
                pass
            self._mexc_sync_time()
            # CCXT quirk: avoid spot currency probing for futures
            try:
                self.x.has["fetchCurrencies"] = False
                setattr(self.x, "fetch_currencies", lambda *_: {})  # type: ignore[attr-defined]
                opts = getattr(self.x, "options", {}) or {}
                opts["defaultType"] = "swap"
                self.x.options = opts
            except Exception:
                pass

        # Load markets
        try:
            self.x.load_markets(params={"type": "swap"} if self.cfg.account_type == "swap" else {})
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

        if self._is_mexc:
            log.info("[MEXC] amount mode=%s", (os.getenv("MEXC_AMOUNT_MODE") or "contracts").lower())

    # ------------------------ MEXC basics ------------------------
    def _mexc_sync_time(self) -> None:
        """Align signer millis with MEXC server to avoid Request-Time issues."""
        try:
            server_ms: Optional[int] = None
            try:
                j = self.x.fetch2("contract/ping", "contractPublic", "GET")
                if isinstance(j, dict):
                    server_ms = int(j.get("data") or 0) or None
            except Exception:
                pass
            if server_ms is None:
                try:
                    j2 = self.x.fetch2("time", "public", "GET")
                    if isinstance(j2, dict):
                        server_ms = int(j2.get("serverTime") or 0) or None
                except Exception:
                    pass
            if server_ms:
                off = int(server_ms - int(time.time() * 1000))
                self.x.milliseconds = (lambda off=off: int(time.time() * 1000 + off))
                log.info(f"[MEXC] time sync: offset={off}ms")
        except Exception:
            pass

    def _mexc_open_type(self) -> int:
        try:
            v = int(os.getenv("MEXC_OPEN_TYPE", "2"))
            return 1 if v == 1 else 2
        except Exception:
            return 2

    def _mexc_symbol_id(self, symbol: str) -> Optional[str]:
        """
        Return MEXC futures raw symbol id, e.g. 'BTC_USDT' for 'BTC/USDT:USDT'.
        Tries ccxt helpers first, then falls back to deterministic normalization.
        """
        # 1) ccxt fast path
        try:
            if hasattr(self.x, "market_id"):
                mid = self.x.market_id(symbol)
                if isinstance(mid, str) and mid:
                    return mid
        except Exception:
            pass
        # 2) from market() dict
        try:
            m = self.x.market(symbol)
            mid = m.get("id")
            if isinstance(mid, str) and mid:
                return mid
        except Exception:
            pass
        # 3) robust normalization fallbacks
        try:
            s = str(symbol).upper().strip()
            if s.endswith(":USDT"):
                s = s.replace("/USDT:USDT", "_USDT")
                s = s.replace("/USD:USDT", "_USDT")
            elif "/USDT" in s:
                s = s.replace("/USDT", "_USDT")
            elif s.endswith("USDT"):
                if not s.endswith("_USDT"):
                    s = s[:-4] + "_USDT"
            s = s.replace("__", "_").replace("://", "_")
            if s.count("_") == 1 and s.endswith("_USDT"):
                return s
        except Exception:
            pass
        return None

    # ------------------------ CCXT implicit call helper ------------------------
    def _mexc_call(self, method: str, *, payload: Optional[dict] = None, path_params: Optional[dict] = None, fallback_path: Optional[str] = None, http_method: str = "GET") -> Any:
        """
        Prefer CCXT implicit method (e.g., 'contractPrivatePostOrderSubmit').
        If unavailable, fall back to fetch2(fallback_path, 'contractPrivate', http_method, ...).
        For methods with path params, pass them via path_params (used by implicit method),
        and we also compose fetch2 fallback path if provided.
        """
        payload = payload or {}
        path_params = path_params or {}
        # 1) implicit method
        fn = getattr(self.x, method, None)
        if callable(fn):
            try:
                # implicit methods accept both url params and body for POST/GET
                args = dict(payload)
                args.update(path_params)
                return fn(args)
            except Exception as e:
                raise
        # 2) fallback to fetch2
        if fallback_path is None:
            raise ccxt.ExchangeError(f"MEXC call {method} not supported by this CCXT build and no fallback path provided.")
        # If path has templates like {symbol}/{externalOid}, fill them
        path = fallback_path
        for k, v in path_params.items():
            path = path.replace("{" + k + "}", str(v))
        return self.x.fetch2(path, "contractPrivate", http_method, payload)

    # ------------------------ Universe & market data ------------------------
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

    # >>> Needed by your live loop
    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int):
        """Thin wrapper used by your strategy to pull bars."""
        return self.x.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)

    def fetch_tickers(self, symbols: Iterable[str]) -> Dict[str, dict]:
        """Thin wrapper used by your strategy to pull quote snapshots."""
        try:
            return self.x.fetch_tickers(list(symbols))
        except Exception:
            return {}
    # <<<

    # ------------------------ Account / positions ------------------------
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
        consolidated: Dict[str, dict] = {}
        try:
            raw = self.x.fetch_positions() or []
        except Exception as e:
            log.debug(f"fetch_positions failed: {e}")
            raw = []
        for p in raw:
            try:
                s = p.get("symbol") or p.get("info", {}).get("symbol")
                if not s: continue
                side = (p.get("side") or "").lower()
                qty = float(p.get("contracts") or p.get("positionAmt") or p.get("size") or p.get("amount") or 0.0)
                ep = p.get("entryPrice")
                epf = float(ep) if ep not in (None, "") else 0.0
                c = consolidated.setdefault(s, {"long_qty": 0.0, "short_qty": 0.0, "net_qty": 0.0, "entryPrice": 0.0})
                if side == "long":
                    c["long_qty"] += abs(qty)
                elif side == "short":
                    c["short_qty"] += abs(qty)
                else:
                    if qty > 0: c["long_qty"] += abs(qty)
                    elif qty < 0: c["short_qty"] += abs(qty)
                if epf > 0: c["entryPrice"] = epf
            except Exception:
                continue
        for s in list(consolidated.keys()):
            c = consolidated[s]
            c["net_qty"] = float(c.get("long_qty", 0.0)) - float(c.get("short_qty", 0.0))
            c["entryPrice"] = float(c.get("entryPrice") or 0.0)
        return consolidated

    # ------------------------ Precision / sizing ------------------------
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
        m = (self.x.market(symbol) or {})
        precision = m.get("precision") or {}
        ap = precision.get("amount")
        step = None
        if isinstance(ap, int) and ap >= 0:
            step = 10 ** (-ap)
        elif isinstance(ap, float) and 0.0 < ap < 1.0:
            step = ap
        if not step or step <= 0:
            lim = (m.get("limits", {}) or {})
            min_amt = float((lim.get("amount", {}) or {}).get("min", 0.0) or 0.0)
            step = min_amt if min_amt > 0 else 1.0
        return float(step)

    def _amount_mode(self) -> str:
        return (os.getenv("MEXC_AMOUNT_MODE") or "contracts").strip().lower()

    def _contracts_from_size(self, symbol: str, size: float) -> float:
        """For MEXC perps, convert base size → contracts using contractSize and volScale."""
        if not self._is_mexc or self.cfg.account_type != "swap":
            return size
        mode = self._amount_mode()
        try:
            m = self.x.market(symbol)
            csize = float(m.get("contractSize") or 1.0)
            vol_scale = int((m.get("info") or {}).get("volScale") or 0)
        except Exception:
            csize, vol_scale = 1.0, 0
        qty = (size / csize) if mode == "base" else float(size)
        if vol_scale > 0:
            step = 10 ** (-vol_scale)
            qty = math.floor(qty / step) * step
            qty = max(qty, step)
        else:
            qty = max(float(int(qty)), 1.0)
        log.info("[MEXC sizing] base→contracts: base=%s csize=%s -> contracts=%.8f", size, csize, qty)
        return qty

    def _contracts_notional_usdt(self, symbol: str, contracts: float, px: Optional[float]) -> float:
        if px is None:
            return 0.0
        try:
            csize = float((self.x.market(symbol) or {}).get("contractSize") or 1.0)
            return float(contracts) * csize * float(px)
        except Exception:
            return float(contracts) * float(px)

    # ------------------------ Leverage ------------------------
    def _ensure_lev_once(self, symbol: str, lev: int = 1) -> None:
        if self._lev_bootstrapped.get(symbol): return
        try:
            if getattr(self.x, "has", {}).get("setLeverage") or hasattr(self.x, "set_leverage"):
                try:
                    self.x.set_leverage(lev, symbol, params={"buyLeverage": lev, "sellLeverage": lev})
                except AttributeError:
                    self.x.setLeverage(lev, symbol, params={"buyLeverage": lev, "sellLeverage": lev})  # type: ignore[attr-defined]
        except Exception as e:
            log.debug(f"bootstrap set_leverage({symbol},{lev}) failed: {e}")
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

    # ------------------------ Orders (MEXC, implicit methods) ------------------------
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

        # qty
        raw_qty = abs(self._contracts_from_size(symbol, size))
        if self._is_mexc and self.cfg.account_type == "swap":
            q = raw_qty
        else:
            step = self._amount_step(symbol)
            q = max(self._precise_amount(symbol, raw_qty), step)

        p = None if price is None else self._precise_price(symbol, float(price))

        # Min-notional guard (esp. for limit)
        if p is not None:
            notional = self._contracts_notional_usdt(symbol, q, p)
            min_cost = 5.0 if self._is_mexc else 0.0
            if 0 < notional < min_cost:
                try:
                    m = self.x.market(symbol)
                    csize = float((m.get("contractSize") or 1.0) or 1.0)
                    vol_scale = int((m.get("info") or {}).get("volScale") or 0) if self._is_mexc else 0
                except Exception:
                    csize, vol_scale = 1.0, 0
                need_contracts = (min_cost / max(csize * p, 1e-12))
                if self._is_mexc and self.cfg.account_type == "swap":
                    if vol_scale > 0:
                        step = 10 ** (-vol_scale)
                        q = math.ceil(need_contracts / step) * step
                    else:
                        q = float(math.ceil(need_contracts))
                else:
                    amt_step = self._amount_step(symbol)
                    q = max(q, amt_step, need_contracts)

        params: Dict[str, Any] = {}
        s_lower = side.lower()
        if reduce_only:
            params["reduceOnly"] = True  # one-way only; ignored on hedge

        client_oid = self._gen_client_oid()
        params["clientOrderId"] = client_oid

        # ---- MEXC FUTURES path ----
        if self._is_mexc and self.cfg.account_type == "swap":
            open_type = self._mexc_open_type()
            external_oid = client_oid  # idempotency key

            otype = "market" if p is None else "limit"
            mexc_type = 5 if p is None else 1

            # Numeric side mapping per docs
            force_numeric = True if os.getenv("MEXC_FORCE_NUMERIC_SIDE", "1") == "1" else False
            if force_numeric:
                if reduce_only:
                    mexc_side = 2 if s_lower == "buy" else 4
                else:
                    mexc_side = 1 if s_lower == "buy" else 3
            else:
                mexc_side = 1 if s_lower == "buy" else 3

            # anti-dup (8s)
            fp = self._fingerprint(symbol, s_lower, reduce_only, p, q)
            now = time.time()
            self._inflight = {k: v for k, v in self._inflight.items() if (now - v.get("ts", 0)) <= 15}
            if fp in self._inflight and (now - self._inflight[fp]["ts"] < 8):
                reuse_oid = self._inflight[fp]["oid"]
                log.info("[MEXC] dup call (%.2fs) reuse externalOid=%s; confirming only.", now - self._inflight[fp]["ts"], reuse_oid)
                confirmed = self._mexc_confirm_by_external(symbol, reuse_oid, otype, s_lower, q, p, poll=True)
                if confirmed: return confirmed
                rec = self._recover_mexc_order_by_external_oid(symbol, reuse_oid)
                if rec: return rec
                return {"id": "", "clientOrderId": reuse_oid, "symbol": symbol, "type": otype, "side": s_lower,
                        "amount": float(q), "price": None if p is None else float(p), "status": "open",
                        "info": {"note": "pending-confirmation-no-repost"}}

            self._inflight[fp] = {"ts": now, "oid": external_oid}

            # --- derive raw market id robustly and guard ---
            market_id = self._mexc_symbol_id(symbol)
            if not market_id:
                raise ccxt.BadSymbol(
                    f"MEXC raw symbol id could not be derived from '{symbol}'. "
                    f"Check load_markets() or add a manual mapping."
                )

            # Build submit payload
            payload = {
                "symbol": market_id,         # e.g. BTC_USDT
                "vol": float(q),
                "type": mexc_type,
                "openType": open_type,
                "side": int(mexc_side),
                "externalOid": external_oid,
            }
            if p is not None:
                payload["price"] = float(p)

            # leverage (needed when opening isolated positions)
            lev = getattr(self.cfg, "leverage", None) or getattr(self.cfg, "default_leverage", None)
            if open_type == 1 and lev and not reduce_only:
                payload["leverage"] = int(lev)

            if params.get("reduceOnly"):
                payload["reduceOnly"] = True

            # Submit via CCXT implicit method (fallback to fetch2)
            try:
                # implicit: contractPrivatePostOrderSubmit
                j = self._mexc_call(
                    "contractPrivatePostOrderSubmit",
                    payload=payload,
                    fallback_path="order/submit",
                    http_method="POST",
                )
            except ccxt.RequestTimeout:
                log.error("Order timeout %s %s %s@%s | externalOid=%s — confirming (no re-post).",
                          symbol, s_lower, q, p if p is not None else "MKT", external_oid)
                confirmed = self._mexc_confirm_by_external(symbol, external_oid, otype, s_lower, q, p, poll=True)
                if confirmed: return confirmed
                rec = self._recover_mexc_order_by_external_oid(symbol, external_oid)
                if rec: return rec
                body = getattr(self.x, "last_http_response", None)
                if body:
                    try:
                        jb = json.loads(body)
                        if jb.get("success") is True and jb.get("code") == 0:
                            return {"id": "", "clientOrderId": external_oid, "symbol": symbol, "type": otype, "side": s_lower,
                                    "amount": float(q), "price": None if p is None else float(p), "status": "open",
                                    "info": {"note": "submitted-awaiting-confirmation"}, "externalOid": external_oid}
                    except Exception:
                        pass
                raise

            if isinstance(j, dict) and j.get("success") is True and j.get("code") == 0:
                oid = self._extract_mexc_order_id(j)
                if oid:
                    return {"id": str(oid), "symbol": symbol, "type": otype, "side": s_lower,
                            "amount": float(q), "price": None if p is None else float(p),
                            "status": "open", "info": j, "externalOid": external_oid}
                confirmed = self._mexc_confirm_by_external(symbol, external_oid, otype, s_lower, q, p, poll=True)
                if confirmed: return confirmed
                return {"id": "", "clientOrderId": external_oid, "symbol": symbol, "type": otype, "side": s_lower,
                        "amount": float(q), "price": None if p is None else float(p), "status": "open",
                        "info": {"note": "submitted-awaiting-confirmation"}, "externalOid": external_oid}

            confirmed = self._mexc_confirm_by_external(symbol, external_oid, otype, s_lower, q, p, poll=True)
            if confirmed: return confirmed
            raise ccxt.ExchangeError(f"MEXC submit failed: {j}")

        # ---- Non-MEXC (Bybit etc.) ----
        try:
            if self.cfg.account_type == "swap":
                if p is None:
                    return self.x.create_order(symbol, "market", s_lower, q, None, {"clientOrderId": client_oid, "reduceOnly": reduce_only})
                else:
                    prms = {"clientOrderId": client_oid, "reduceOnly": reduce_only}
                    if post_only: prms["postOnly"] = True
                    return self.x.create_order(symbol, "limit", s_lower, q, p, prms)
            else:
                if p is None:
                    return self.x.create_market_order(symbol, s_lower, q, params={"clientOrderId": client_oid})
                else:
                    prms = {"clientOrderId": client_oid}
                    if post_only: prms["postOnly"] = True
                    return self.x.create_limit_order(symbol, s_lower, q, p, prms)
        except Exception as e:
            log.debug(f"create_order failed: {e}")
            raise

    # ------------------------ Confirm / recovery (MEXC) ------------------------
    def _extract_mexc_order_id(self, j: Dict[str, Any]) -> Optional[str]:
        try:
            data = j.get("data")
            if isinstance(data, (str, int)):
                return str(data)
            if isinstance(data, dict):
                for k in ("orderId", "id", "orderNo", "order_id", "order_no"):
                    if k in data and data[k]:
                        return str(data[k])
            if isinstance(data, list) and data:
                first = data[0]
                if isinstance(first, (str, int)):
                    return str(first)
                if isinstance(first, dict):
                    for k in ("orderId", "id", "orderNo", "order_id", "order_no"):
                        if k in first and first[k]:
                            return str(first[k])
        except Exception:
            pass
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

        def _env_int(name: str, default: int) -> int:
            try: return int(os.getenv(name, str(default)))
            except Exception: return default

        timeout_sec = _env_int("MEXC_CONFIRM_TIMEOUT_SEC", 60)
        backoff_start_ms = _env_int("MEXC_CONFIRM_BACKOFF_START_MS", 300)
        try:
            backoff_mult = float(os.getenv("MEXC_CONFIRM_BACKOFF_MULT", "1.6"))
        except Exception:
            backoff_mult = 1.6
        if backoff_mult <= 0: backoff_mult = 1.0
        backoff_max_ms = _env_int("MEXC_CONFIRM_BACKOFF_MAX_MS", 2500)

        def _once() -> Optional[dict]:
            try:
                # implicit: contractPrivateGetOrderExternalSymbolExternalOid
                j = self._mexc_call(
                    "contractPrivateGetOrderExternalSymbolExternalOid",
                    path_params={"symbol": market_id, "externalOid": external_oid},
                    fallback_path="order/external/{symbol}/{externalOid}",
                    http_method="GET",
                )
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

        deadline = time.time() + float(timeout_sec)
        delay_ms = float(backoff_start_ms)
        while time.time() < deadline:
            got = _once()
            if got:
                return got
            time.sleep(max(delay_ms, 50.0) / 1000.0)
            delay_ms = min(delay_ms * backoff_mult, float(backoff_max_ms))

        # fallback: open orders then recent history (~2 min)
        rec = self._recover_mexc_order_by_external_oid(symbol, external_oid)
        if rec:
            return rec
        try:
            start_ms = int(time.time() * 1000) - 120_000
            j = self._mexc_call(
                "contractPrivateGetOrderListHistoryOrdersSymbol",
                path_params={"symbol": market_id},
                payload={"page_num": 1, "page_size": 50, "start_time": start_ms},
                fallback_path="order/list/history_orders/{symbol}",
                http_method="GET",
            )
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
        # low-level open orders
        try:
            market_id = self._mexc_symbol_id(symbol)
            if market_id:
                j = self._mexc_call(
                    "contractPrivateGetOrderListOpenOrdersSymbol",
                    path_params={"symbol": market_id},
                    payload={"page_num": 1, "page_size": 50},
                    fallback_path="order/list/open_orders/{symbol}",
                    http_method="GET",
                )
                for row in (j or {}).get("data") or []:
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

    # ------------------------ Open orders / wrappers ------------------------
    def fetch_open_orders(self, symbol: Optional[str] = None) -> List[dict]:
        try:
            return self.x.fetch_open_orders(symbol) if symbol else self.x.fetch_open_orders()
        except Exception:
            return []

    def close(self):
        try:
            if hasattr(self.x, "close"):
                self.x.close()
        except Exception:
            pass
