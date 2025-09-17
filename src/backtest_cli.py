import os, sys, json, argparse, inspect, traceback
from typing import Any, Dict, List, Tuple, Optional

# ---------- deps ----------
try:
    import yaml  # type: ignore
except Exception:
    sys.stderr.write("ERROR: PyYAML not installed. Try: pip install pyyaml\n")
    raise

# ---------- tiny utils ----------
class _Attr:
    __slots__ = ("__dict__",)
    def __init__(self, d):
        for k, v in (d or {}).items():
            if isinstance(v, dict):
                v = _Attr(v)
            elif isinstance(v, list):
                v = [(_Attr(x) if isinstance(x, dict) else x) for x in v]
            setattr(self, k, v)
    def __repr__(self):
        return f"_Attr({self.__dict__})"
    def __getattr__(self, name):
        # tolerant missing attrs
        return None

def _coerce_int(val, default=None) -> Optional[int]:
    try:
        if val is None: return default
        return int(val)
    except Exception:
        return default

def _coerce_float(val, default=None) -> Optional[float]:
    try:
        if val is None: return default
        return float(val)
    except Exception:
        return default

def _coerce_equity_scalar(val) -> Optional[float]:
    """Return a scalar float for equity; accept list/tuple/Series/ndarray; fallback to 0.0."""
    try:
        if val is None:
            return None
        return float(val)
    except Exception:
        pass
    try:
        if isinstance(val, (list, tuple)) and len(val) > 0:
            return float(val[-1])
    except Exception:
        pass
    try:
        import pandas as pd  # type: ignore
        if isinstance(val, pd.Series):
            if not val.empty:
                return float(val.iloc[-1])
            return None
    except Exception:
        pass
    try:
        import numpy as np  # type: ignore
        if isinstance(val, np.ndarray) and val.size > 0:
            return float(val[-1])
    except Exception:
        pass
    return None

# ---------- config helpers ----------
def _build_cfg_obj(config_path: str, cfg_dict: Dict[str, Any]):
    """Try project-native loader then fall back to attribute wrapper."""
    try:
        C = __import__("src.config", fromlist=["*"])
    except Exception:
        C = None

    for name in ("load_config", "load_app_config", "read_config", "parse_config"):
        try:
            if C and hasattr(C, name):
                fn = getattr(C, name)
                obj = fn(config_path)  # type: ignore
                return obj
        except Exception:
            pass

    try:
        if C and hasattr(C, "AppConfig"):
            AppConfig = getattr(C, "AppConfig")
            if hasattr(AppConfig, "model_validate"):
                return AppConfig.model_validate(cfg_dict)  # pydantic v2
            if hasattr(AppConfig, "parse_obj"):
                return AppConfig.parse_obj(cfg_dict)      # pydantic v1
            try:
                return AppConfig(**cfg_dict)              # dataclass/constructor
            except Exception:
                pass
    except Exception:
        pass

    return _Attr(cfg_dict or {})

def _derive_symbols_from_cfg(cfg: Dict[str, Any]) -> List[str]:
    strat = (cfg or {}).get("strategy", {}) or {}
    ex = (cfg or {}).get("exchange", {}) or {}
    for key in ("symbols", "universe", "tickers"):
        v = strat.get(key) or ex.get(key)
        if v:
            if isinstance(v, str):
                return [s.strip() for s in v.split(",") if s.strip()]
            if isinstance(v, list):
                return [str(s) for s in v if s]
    return ["BTC/USDT", "ETH/USDT"]

# ---------- strategy shim ----------
_KAPPA_NAMES = ("kappa", "kappa_mult", "kappa_scale", "concentration_kappa")

def _get_any(src, names, default=None):
    for n in names:
        try:
            if isinstance(src, dict) and n in src:
                return src[n]
            v = getattr(src, n)
            if v is not None:
                return v
        except Exception:
            pass
    return default

def _make_strategy_shim(cfg_obj):
    strat_src = getattr(cfg_obj, "strategy", None)
    class StrategyShim:
        def __init__(self, src):
            self._src = src
            def g(name, default=None):
                try:
                    return getattr(src, name)
                except Exception:
                    try:
                        return src.get(name, default) if isinstance(src, dict) else default
                    except Exception:
                        return default
            # core
            self.lookbacks = g("lookbacks", [])
            self.lookback_weights = g("lookback_weights", [])
            self.vol_lookback = g("vol_lookback", 72)
            self.k_min = g("k_min", 2)
            self.k_max = g("k_max", 8)
            self.k = g("k", None)
            self.top_k = g("top_k", None)
            self.k_long = g("k_long", None)
            self.k_short = g("k_short", None)
            # knobs
            self.z_power = g("z_power", 1.0)
            self.rebalance_delta_frac = g("rebalance_delta_frac", 0.3)
            self.turnover_lambda = g("turnover_lambda", 0.0)
            # nested blocks
            nb = g("no_trade_bands", {})
            self.no_trade_bands = _Attr({
                "enabled": bool(_get_any(nb, ["enabled"], False)),
                "upper": _get_any(nb, ["upper"], 0.0),
                "lower": _get_any(nb, ["lower"], 0.0),
                "hysteresis": _get_any(nb, ["hysteresis"], 0.0),
            })
            sel = g("selection", {})
            sel_kappa = _get_any(sel, _KAPPA_NAMES, None)
            if sel_kappa is None:
                sel_kappa = _get_any(src, _KAPPA_NAMES, 1.0)
            try:
                sel_kappa = float(sel_kappa)
            except Exception:
                sel_kappa = 1.0
            self.selection = _Attr({
                "enabled": bool(_get_any(sel, ["enabled"], False)),
                "method": _get_any(sel, ["method"], "zscore"),
                "top_k": _get_any(sel, ["top_k"], None),
                "threshold": _get_any(sel, ["threshold"], None),
                "kappa": sel_kappa,
            })
            # NEW: portfolio vol targeting block
            pvt = g("portfolio_vol_target", {})
            self.portfolio_vol_target = _Attr({
                "enabled": bool(_get_any(pvt, ["enabled"], False)),
                "target": _coerce_float(_get_any(pvt, ["target", "vol_target", "annual_target"]), 0.0),
                "lookback": _coerce_int(_get_any(pvt, ["lookback", "lb"]), 60),
                "max_leverage": _coerce_float(_get_any(pvt, ["max_leverage", "max_lev"]), 1.0),
            })
        def __getattr__(self, name):
            src = self._src
            try: return getattr(src, name)
            except Exception: pass
            try:
                if isinstance(src, dict): return src[name]
            except Exception: pass
            # treat unknown nested blocks as empty structs (avoid None.enabled AttributeError)
            if name.endswith(("_bands", "_filters", "_target", "_cfg", "_config")) or name in (
                "no_trade_bands","diversify","carry","regime","microstructure","selection","portfolio_vol_target",
                "position_limits","risk_limits","risk_parity"
            ):
                return _Attr({"enabled": False})
            return None
    return StrategyShim(strat_src)

# ---------- K-family helpers ----------
def _compute_k_from_symbols(symbols) -> int:
    try:
        N = max(1, len(symbols) if isinstance(symbols, (list, tuple)) else int(symbols))
    except Exception:
        N = 20
    base = max(1, N // 10) if N >= 10 else 1
    return max(1, min(base, max(1, N // 2)))

def _apply_kappa_to_obj(obj, kappa_val: float):
    if obj is None:
        return
    try:
        for nm in _KAPPA_NAMES:
            try: setattr(obj, nm, float(kappa_val))
            except Exception: pass
        sel = getattr(obj, "selection", None)
        if sel is None:
            try: obj.selection = _Attr({}); sel = obj.selection
            except Exception: sel = None
        if sel is not None:
            for nm in _KAPPA_NAMES:
                try: setattr(sel, nm, float(kappa_val))
                except Exception: pass
            try: sel.enabled = True
            except Exception: pass
    except Exception:
        pass

def _inject_valid_k_family(real_params: List[str], a2: Tuple[Any, ...], k2: Dict[str, Any], cfg_obj) -> Tuple[Tuple[Any, ...], Dict[str, Any]]:
    # Universe size
    sym_val = None
    if "symbols" in real_params:
        s_idx = real_params.index("symbols")
        if s_idx < len(a2):
            sym_val = a2[s_idx]
    if sym_val is None and "symbols" in k2:
        sym_val = k2["symbols"]
    try:
        N = len(sym_val) if isinstance(sym_val, (list, tuple)) else int(sym_val)
        N = max(1, N)
    except Exception:
        N = 20
    halfN = max(1, N // 2)

    strat = getattr(cfg_obj, "strategy", None)

    sel_top_k = None
    try:
        sel = getattr(strat, "selection", None)
        sel_top_k = getattr(sel, "top_k", None) if sel is not None else None
    except Exception:
        pass
    if sel_top_k is None and isinstance(strat, dict):
        sel_top_k = strat.get("selection", {}).get("top_k")

    k_guess = _coerce_int(k2.get("k"), None)
    if k_guess is None:
        k_guess = _coerce_int(k2.get("top_k"), None)
    if k_guess is None:
        k_guess = _coerce_int(sel_top_k, None)
    if k_guess is None:
        try:
            k_guess = _coerce_int(getattr(strat, "k_max", None), None)
        except Exception:
            k_guess = None
    if k_guess is None:
        k_guess = _compute_k_from_symbols(sym_val)

    kmin = _coerce_int(k2.get("k_min"), None)
    kmax = _coerce_int(k2.get("k_max"), None)
    if kmin is None and strat is not None:
        try: kmin = _coerce_int(getattr(strat, "k_min", None), None)
        except Exception: pass
    if kmax is None and strat is not None:
        try: kmax = _coerce_int(getattr(strat, "k_max", None), None)
        except Exception: pass
    if kmax is None: kmax = min(max(1, k_guess), halfN)
    if kmin is None: kmin = max(1, min(kmax, max(1, k_guess // 2)))
    kmax = max(1, min(int(kmax), halfN))
    kmin = max(1, min(int(kmin), kmax))
    k_guess = max(kmin, min(int(k_guess), kmax))

    kap = None
    for nm in _KAPPA_NAMES:
        kap = _coerce_float(k2.get(nm), None)
        if kap is not None:
            break
    if kap is None and strat is not None:
        try:
            kap = _coerce_float(getattr(getattr(strat, "selection", None), "kappa", None), None)
        except Exception:
            kap = None
    if kap is None and strat is not None:
        for nm in _KAPPA_NAMES:
            try:
                kap = _coerce_float(getattr(strat, nm), None)
                if kap is not None:
                    break
            except Exception:
                pass
    if kap is None:
        kap = 1.0

    try:
        strobj = getattr(cfg_obj, "strategy", None)
        if strobj is not None:
            sel = getattr(strobj, "selection", None)
            if sel is None:
                strobj.selection = _Attr({}); sel = strobj.selection
            try: sel.top_k = int(k_guess)
            except Exception: pass
            try: sel.enabled = True
            except Exception: pass
            _apply_kappa_to_obj(strobj, kap)
            for nm, val in (("k", k_guess), ("top_k", k_guess), ("k_long", k_guess), ("k_short", k_guess),
                            ("k_min", kmin), ("k_max", kmax)):
                try: setattr(strobj, nm, int(val))
                except Exception: pass
    except Exception:
        pass

    try:
        sc_idx = real_params.index("strategy_cfg")
    except ValueError:
        sc_idx = None
    def _apply_to_sc(sc):
        try:
            sel = getattr(sc, "selection", None)
            if sel is None:
                sc.selection = _Attr({}); sel = sc.selection
            try: sel.top_k = int(k_guess)
            except Exception: pass
            try: sel.enabled = True
            except Exception: pass
            _apply_kappa_to_obj(sc, kap)
            for nm, val in (("k", k_guess), ("top_k", k_guess), ("k_long", k_guess), ("k_short", k_guess),
                            ("k_min", kmin), ("k_max", kmax)):
                try: setattr(sc, nm, int(val))
                except Exception: pass
            # ensure portfolio vol target exists
            if getattr(sc, "portfolio_vol_target", None) is None:
                sc.portfolio_vol_target = _Attr({"enabled": False, "target": 0.0, "lookback": 60, "max_leverage": 1.0})
        except Exception:
            pass
    if sc_idx is not None and sc_idx < len(a2):
        _apply_to_sc(a2[sc_idx])
    elif "strategy_cfg" in k2:
        _apply_to_sc(k2["strategy_cfg"])

    for pname, val in (("k", k_guess), ("top_k", k_guess), ("k_min", kmin), ("k_max", kmax)):
        if pname in real_params:
            p_idx = real_params.index(pname)
            if p_idx < len(a2):
                if _coerce_int(a2[p_idx], None) is None:
                    a2 = list(a2); a2[p_idx] = int(val); a2 = tuple(a2)
                if pname in k2:
                    k2.pop(pname, None)
            else:
                if _coerce_int(k2.get(pname), None) is None:
                    k2[pname] = int(val)

    if "symbols" not in k2:
        sym = None
        if "symbols" in real_params:
            s_idx = real_params.index("symbols")
            if s_idx < len(a2):
                sym = a2[s_idx]
        if sym is not None:
            k2["symbols"] = sym

    return a2, k2

# ---------- wrappers for sizing helpers ----------
def _wrap_dynamic_k(module):
    if module is None:
        return
    dk = getattr(module, "_dynamic_k", None)
    if not callable(dk):
        return
    sig = inspect.signature(dk)
    params = list(sig.parameters.keys())

    def _get_val(name, a_list, kwargs):
        idx = None
        if name in params:
            idx = params.index(name)
            if idx < len(a_list):
                return a_list[idx], idx
        if name in kwargs:
            return kwargs[name], None
        return None, None

    def _ensure_unique(name, idx, kwargs):
        if idx is not None and name in kwargs:
            kwargs.pop(name, None)

    def wrapper(*args, **kwargs):
        a = list(args)
        kappa_val, kappa_idx = _get_val("kappa", a, kwargs)
        if kappa_val is None:
            for nm in ("kappa_mult", "kappa_scale", "concentration_kappa"):
                kappa_val, kappa_idx = _get_val(nm, a, kwargs)
                if kappa_val is not None:
                    break
        kappa_val = _coerce_float(kappa_val, 1.0)

        disp_val, disp_idx = _get_val("disp", a, kwargs)
        disp_val = _coerce_float(disp_val, 1.0)

        kmin_val, kmin_idx = _get_val("k_min", a, kwargs)
        kmax_val, kmax_idx = _get_val("k_max", a, kwargs)
        if kmin_val is None and "kmin" in params:
            kmin_val, kmin_idx = _get_val("kmin", a, kwargs)
        if kmax_val is None and "kmax" in params:
            kmax_val, kmax_idx = _get_val("kmax", a, kwargs)

        k_prov = int(max(1, round(float(kappa_val) * float(disp_val))))
        kmin_val = _coerce_int(kmin_val, 1)
        kmax_val = _coerce_int(kmax_val, max(kmin_val, k_prov))

        if "kappa" in params:
            if kappa_idx is not None:
                a[kappa_idx] = float(kappa_val); _ensure_unique("kappa", kappa_idx, kwargs)
            else:
                kwargs.setdefault("kappa", float(kappa_val))
        if "disp" in params:
            if disp_idx is not None:
                a[disp_idx] = float(disp_val); _ensure_unique("disp", disp_idx, kwargs)
            else:
                kwargs.setdefault("disp", float(disp_val))

        if "k_min" in params:
            if kmin_idx is not None:
                a[kmin_idx] = int(kmin_val); _ensure_unique("k_min", kmin_idx, kwargs)
            else:
                kwargs.pop("kmin", None)
                kwargs.setdefault("k_min", int(kmin_val))
            kwargs.pop("kmin", None)
        elif "kmin" in params:
            if kmin_idx is not None:
                a[kmin_idx] = int(kmin_val); _ensure_unique("kmin", kmin_idx, kwargs)
            else:
                kwargs.setdefault("kmin", int(kmin_val))
            kwargs.pop("k_min", None)
        else:
            kwargs.pop("k_min", None); kwargs.pop("kmin", None)

        if "k_max" in params:
            if kmax_idx is not None:
                a[kmax_idx] = int(kmax_val); _ensure_unique("k_max", kmax_idx, kwargs)
            else:
                kwargs.pop("kmax", None)
                kwargs.setdefault("k_max", int(kmax_val))
            kwargs.pop("kmax", None)
        elif "kmax" in params:
            if kmax_idx is not None:
                a[kmax_idx] = int(kmax_val); _ensure_unique("kmax", kmax_idx, kwargs)
            else:
                kwargs.setdefault("kmax", int(kmax_val))
            kwargs.pop("k_max", None)
        else:
            kwargs.pop("k_max", None); kwargs.pop("kmax", None)

        return dk(*tuple(a), **kwargs)
    setattr(module, "_dynamic_k", wrapper)

# ---------- wrapper construction ----------
def _normalize_strategy_arg(args: Tuple[Any, ...], kwargs: Dict[str, Any], real_params: List[str], cfg_obj) -> Tuple[Tuple[Any, ...], Dict[str, Any]]:
    if "strategy_cfg" not in real_params:
        return args, kwargs
    idx = real_params.index("strategy_cfg")
    args_list = list(args)
    shim = _make_strategy_shim(cfg_obj)

    if idx < len(args_list):
        val = args_list[idx]
        if isinstance(val, (list, dict)) or not hasattr(val, "lookbacks"):
            args_list[idx] = shim
    else:
        val = kwargs.get("strategy_cfg", None)
        if isinstance(val, (list, dict)) or (val is None) or not hasattr(val, "lookbacks"):
            kwargs["strategy_cfg"] = shim

    for k in ("lookbacks", "lookback_weights", "vol_lookback"):
        if k in kwargs and k not in real_params:
            kwargs.pop(k, None)

    return tuple(args_list), kwargs

def _wrap_build_targets(module, cfg_obj):
    if module is None:
        return
    bt = getattr(module, "build_targets", None)
    if bt is None:
        for name, obj in module.__dict__.items():
            if callable(obj) and name == "build_targets":
                bt = obj
                break
    if bt is None or not callable(bt):
        return

    sig = inspect.signature(bt)
    real_params = [p for p in sig.parameters.keys()]
    real_param_set = set(real_params)

    def wrapper(*args, **kwargs):
        a2, k2 = _normalize_strategy_arg(args, dict(kwargs), real_params, cfg_obj)
        a2, k2 = _inject_valid_k_family(real_params, a2, k2, cfg_obj)

        try:
            import pandas as pd  # type: ignore
        except Exception:
            pd = None
        if pd is not None and "prev_weights" in real_param_set:
            sym_val = None
            if "symbols" in real_params:
                s_idx = real_params.index("symbols")
                if s_idx < len(a2):
                    sym_val = a2[s_idx]
            if sym_val is None and "symbols" in k2:
                sym_val = k2["symbols"]
            pw_val = None; pw_idx = None
            if "prev_weights" in real_params:
                pw_idx = real_params.index("prev_weights")
                if pw_idx < len(a2):
                    pw_val = a2[pw_idx]
                else:
                    pw_val = k2.get("prev_weights")
            need_fix = (pw_val is None) or (not hasattr(pw_val, "reindex"))
            if need_fix:
                if isinstance(sym_val, (list, tuple)):
                    new_pw = pd.Series(0.0, index=list(sym_val))
                else:
                    new_pw = pd.Series(dtype=float)
                if pw_idx is not None and pw_idx < len(a2):
                    a2 = list(a2); a2[pw_idx] = new_pw; a2 = tuple(a2)
                else:
                    k2["prev_weights"] = new_pw

        if "equity" in real_param_set:
            eq_idx = real_params.index("equity")
            eq_val = None
            if eq_idx < len(a2):
                eq_val = a2[eq_idx]
            elif "equity" in k2:
                eq_val = k2.get("equity")
            new_eq = _coerce_equity_scalar(eq_val)
            if new_eq is None:
                new_eq = 0.0
            if eq_idx < len(a2):
                a2 = list(a2); a2[eq_idx] = float(new_eq); a2 = tuple(a2)
                if "equity" in k2:
                    k2.pop("equity", None)
            else:
                k2["equity"] = float(new_eq)

        kf = {k: v for k, v in k2.items() if k in real_param_set}

        try:
            return bt(*a2, **kf)
        except TypeError:
            return bt(*a2, **{})

    setattr(module, "build_targets", wrapper)

# ---------- backtester import ----------
def _import_backtest_and_module():
    candidates = [
        ("src.backtester", "run_backtest"),
        ("src.backtest", "run_backtest"),
        ("src.bt", "run_backtest"),
    ]
    for mod, func in candidates:
        try:
            m = __import__(mod, fromlist=[func])
            fn = getattr(m, func, None)
            if callable(fn):
                return fn, m
        except Exception:
            continue
    return None, None

# ---------- main ----------
def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="xsmom backtest CLI (hardened)")
    p.add_argument("--config", required=True, help="Path to config.yaml")
    p.add_argument("--symbols", default=None, help="Comma-separated symbols override")
    p.add_argument("--json", default="-", help="Output path for JSON ('-' for stdout)")
    args = p.parse_args(argv)

    with open(args.config, "r") as f:
        cfg_dict = yaml.safe_load(f) or {}

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()] if args.symbols else _derive_symbols_from_cfg(cfg_dict)

    fn, module = _import_backtest_and_module()
    if fn is None or module is None:
        sys.stderr.write("ERROR: Could not import a Python backtester (src.backtester/backtest/bt)\n")
        return 2

    cfg_obj = _build_cfg_obj(args.config, cfg_dict)

    try:
        sizing_mod = __import__("src.sizing", fromlist=["*"])
    except Exception:
        sizing_mod = None
    for mod in (sizing_mod, module):
        _wrap_build_targets(mod, cfg_obj)
        _wrap_dynamic_k(mod)

    try:
        sig = inspect.signature(fn)
        params = list(sig.parameters.keys())
        if len(params) == 1:
            res = fn(cfg_obj)  # type: ignore
        elif "symbols" in params:
            res = fn(cfg_obj, symbols)  # type: ignore
        else:
            res = fn(cfg=cfg_obj, symbols=symbols)  # type: ignore
    except Exception as e:
        sys.stderr.write(f"ERROR: run_backtest invocation failed: {e}\n")
        traceback.print_exc()
        return 3

    if not isinstance(res, dict):
        sys.stderr.write("ERROR: backtester did not return a dict of metrics\n")
        return 4

    out = json.dumps(res, indent=2)
    if args.json == "-":
        print(out)
    else:
        with open(args.json, "w") as f:
            f.write(out)

    return 0

if __name__ == "__main__":
    import sys as _sys
    _sys.exit(main())
