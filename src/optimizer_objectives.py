# src/optimizer_objectives.py
# Objective and constraint helpers for the optimizer.
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Any, Optional


@dataclass
class Metrics:
    ann_return: float
    ann_vol: float
    max_drawdown: float
    sharpe: float
    sortino: float
    turnover: float
    trades: int

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Metrics":
        # Accept a variety of common keys defensively
        def pick(*names, default=None):
            for n in names:
                if n in d and d[n] is not None:
                    return float(d[n])
            return default

        return cls(
            ann_return=pick("ann_return", "annual_return", "annualized_return", default=0.0),
            ann_vol=pick("ann_vol", "annual_vol", "annualized_vol", default=0.0),
            max_drawdown=pick("max_drawdown", "max_dd", "drawdown", default=1.0),
            sharpe=pick("sharpe", default=0.0),
            sortino=pick("sortino", default=0.0),
            turnover=pick("turnover", "annual_turnover", default=0.0),
            trades=int(d.get("trades", d.get("n_trades", 0)) or 0),
        )


def objective_calmar_minus_lambda_turnover(m: Metrics, lam: float = 1e-3) -> float:
    # Calmar = annual return / max drawdown (cap denominator to avoid explosion)
    dd = max(m.max_drawdown, 1e-6)
    calmar = (m.ann_return / dd) if dd > 0 else 0.0
    return calmar - lam * m.turnover


def check_constraints(
    m: Metrics,
    max_drawdown_cap: Optional[float] = None,
    max_ann_vol_cap: Optional[float] = None,
    min_trades: Optional[int] = None,
) -> bool:
    if max_drawdown_cap is not None and m.max_drawdown > max_drawdown_cap:
        return False
    if max_ann_vol_cap is not None and m.ann_vol > max_ann_vol_cap:
        return False
    if min_trades is not None and m.trades < min_trades:
        return False
    return True
