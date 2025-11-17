"""
Monte Carlo stress testing for trade equity curves.

Implements bootstrapping and perturbation methods to assess tail risk.
"""
from __future__ import annotations

import logging
from typing import Dict, Any, List, Tuple, Optional
from dataclasses import dataclass
import numpy as np
import pandas as pd

log = logging.getLogger("optimizer.monte_carlo")


@dataclass
class MCConfig:
    """Monte Carlo configuration."""
    n_runs: int = 1000
    block_size: int = 20  # For block bootstrapping (maintains autocorrelation)
    slippage_std_bps: float = 1.0  # Std dev of slippage perturbations (bps)
    fee_std_bps: float = 0.5  # Std dev of fee perturbations (bps)
    funding_std_bps: float = 0.2  # Std dev of funding perturbations (bps)
    seed: Optional[int] = None


def extract_trade_pnl(equity_curve: pd.Series) -> np.ndarray:
    """
    Extract per-period PnL from equity curve.
    
    Args:
        equity_curve: Series of equity values (indexed by datetime)
    
    Returns:
        Array of per-period returns (fractional)
    """
    returns = equity_curve.pct_change().dropna().values
    return returns


def bootstrap_trades(
    trade_returns: np.ndarray,
    n_runs: int,
    block_size: int = 1,
    seed: Optional[int] = None,
) -> List[np.ndarray]:
    """
    Bootstrap trade returns to create synthetic equity paths.
    
    Args:
        trade_returns: Array of per-trade returns
        n_runs: Number of bootstrap samples
        block_size: Block size for block bootstrapping (1 = simple bootstrap)
        seed: Random seed
    
    Returns:
        List of synthetic return arrays
    """
    if seed is not None:
        np.random.seed(seed)
    
    n_trades = len(trade_returns)
    synthetic_paths = []
    
    for _ in range(n_runs):
        if block_size == 1:
            # Simple bootstrap (resample with replacement)
            indices = np.random.randint(0, n_trades, size=n_trades)
            synthetic = trade_returns[indices]
        else:
            # Block bootstrap (maintains autocorrelation)
            n_blocks = (n_trades + block_size - 1) // block_size
            synthetic = []
            for _ in range(n_blocks):
                block_start = np.random.randint(0, max(1, n_trades - block_size + 1))
                block = trade_returns[block_start:block_start + block_size]
                synthetic.extend(block)
            synthetic = np.array(synthetic[:n_trades])
        
        synthetic_paths.append(synthetic)
    
    return synthetic_paths


def perturb_costs(
    trade_returns: np.ndarray,
    n_runs: int,
    slippage_std_bps: float = 1.0,
    fee_std_bps: float = 0.5,
    funding_std_bps: float = 0.2,
    seed: Optional[int] = None,
) -> List[np.ndarray]:
    """
    Create synthetic paths by perturbing costs (slippage, fees, funding).
    
    Args:
        trade_returns: Array of per-trade returns
        n_runs: Number of synthetic paths
        slippage_std_bps: Std dev of slippage perturbations (basis points)
        fee_std_bps: Std dev of fee perturbations (basis points)
        funding_std_bps: Std dev of funding perturbations (basis points)
        seed: Random seed
    
    Returns:
        List of perturbed return arrays
    """
    if seed is not None:
        np.random.seed(seed)
    
    synthetic_paths = []
    
    for _ in range(n_runs):
        # Generate cost perturbations (normal distribution)
        slippage_shock = np.random.normal(0, slippage_std_bps / 10_000, size=len(trade_returns))
        fee_shock = np.random.normal(0, fee_std_bps / 10_000, size=len(trade_returns))
        funding_shock = np.random.normal(0, funding_std_bps / 10_000, size=len(trade_returns))
        
        # Subtract costs from returns
        perturbed = trade_returns - slippage_shock - fee_shock - funding_shock
        synthetic_paths.append(perturbed)
    
    return synthetic_paths


def compute_equity_path(
    initial_equity: float,
    returns: np.ndarray,
) -> pd.Series:
    """
    Compute equity curve from returns.
    
    Args:
        initial_equity: Starting equity (typically 1.0)
        returns: Array of per-period returns
    
    Returns:
        Series of equity values
    """
    equity = initial_equity * np.cumprod(1.0 + returns)
    return pd.Series(equity, name="equity")


def compute_metrics(equity_curve: pd.Series) -> Dict[str, float]:
    """
    Compute risk metrics from equity curve.
    
    Args:
        equity_curve: Series of equity values
    
    Returns:
        Dict of metrics
    """
    returns = equity_curve.pct_change().dropna()
    
    if len(returns) == 0:
        return {
            "total_return": 0.0,
            "max_drawdown": 0.0,
            "sharpe": 0.0,
            "time_underwater": 0.0,
        }
    
    total_return = float(equity_curve.iloc[-1] / equity_curve.iloc[0] - 1.0)
    
    # Max drawdown
    running_max = equity_curve.cummax()
    drawdown = (equity_curve / running_max - 1.0).min()
    max_drawdown = float(drawdown)
    
    # Sharpe (annualized, assuming hourly returns)
    hours_per_year = 24 * 365
    ann_return = (1.0 + returns.mean()) ** hours_per_year - 1.0
    ann_vol = returns.std() * np.sqrt(hours_per_year)
    sharpe = ann_return / ann_vol if ann_vol > 0 else 0.0
    
    # Time underwater (bars below previous peak)
    underwater = (equity_curve < running_max).sum()
    time_underwater = float(underwater / len(equity_curve)) if len(equity_curve) > 0 else 0.0
    
    return {
        "total_return": total_return,
        "max_drawdown": max_drawdown,
        "sharpe": sharpe,
        "time_underwater": time_underwater,
    }


def run_monte_carlo_stress_test(
    equity_curve: pd.Series,
    cfg: MCConfig,
    method: str = "bootstrap",
) -> Dict[str, Any]:
    """
    Run Monte Carlo stress test on equity curve.
    
    Args:
        equity_curve: Original equity curve
        cfg: Monte Carlo configuration
        method: "bootstrap", "perturb_costs", or "both"
    
    Returns:
        Dict of MC summary statistics
    """
    trade_returns = extract_trade_pnl(equity_curve)
    
    if len(trade_returns) == 0:
        log.warning("No trade returns to bootstrap")
        return {}
    
    all_metrics: List[Dict[str, float]] = []
    
    if method in ["bootstrap", "both"]:
        # Bootstrap method
        synthetic_returns = bootstrap_trades(
            trade_returns,
            n_runs=cfg.n_runs,
            block_size=cfg.block_size,
            seed=cfg.seed,
        )
        
        for ret in synthetic_returns:
            synthetic_equity = compute_equity_path(1.0, ret)
            metrics = compute_metrics(synthetic_equity)
            all_metrics.append(metrics)
    
    if method in ["perturb_costs", "both"]:
        # Cost perturbation method
        perturbed_returns = perturb_costs(
            trade_returns,
            n_runs=cfg.n_runs,
            slippage_std_bps=cfg.slippage_std_bps,
            fee_std_bps=cfg.fee_std_bps,
            funding_std_bps=cfg.funding_std_bps,
            seed=cfg.seed + cfg.n_runs if cfg.seed is not None else None,
        )
        
        for ret in perturbed_returns:
            synthetic_equity = compute_equity_path(1.0, ret)
            metrics = compute_metrics(synthetic_equity)
            all_metrics.append(metrics)
    
    if not all_metrics:
        return {}
    
    # Aggregate statistics
    metrics_df = pd.DataFrame(all_metrics)
    
    summary = {
        "n_runs": len(all_metrics),
        "mean_total_return": float(metrics_df["total_return"].mean()),
        "std_total_return": float(metrics_df["total_return"].std()),
        "p1_total_return": float(metrics_df["total_return"].quantile(0.01)),
        "p5_total_return": float(metrics_df["total_return"].quantile(0.05)),
        "p95_total_return": float(metrics_df["total_return"].quantile(0.95)),
        "p99_total_return": float(metrics_df["total_return"].quantile(0.99)),
        "mean_max_drawdown": float(metrics_df["max_drawdown"].mean()),
        "std_max_drawdown": float(metrics_df["max_drawdown"].std()),
        "p95_max_drawdown": float(metrics_df["max_drawdown"].quantile(0.95)),
        "p99_max_drawdown": float(metrics_df["max_drawdown"].quantile(0.99)),
        "worst_max_drawdown": float(metrics_df["max_drawdown"].min()),
        "mean_sharpe": float(metrics_df["sharpe"].mean()),
        "std_sharpe": float(metrics_df["sharpe"].std()),
        "p5_sharpe": float(metrics_df["sharpe"].quantile(0.05)),
        "mean_time_underwater": float(metrics_df["time_underwater"].mean()),
        "p95_time_underwater": float(metrics_df["time_underwater"].quantile(0.95)),
    }
    
    log.info(
        f"MC stress test complete: {len(all_metrics)} runs, "
        f"mean DD={summary['mean_max_drawdown']:.2%}, "
        f"p95 DD={summary['p95_max_drawdown']:.2%}"
    )
    
    return summary


def compute_tail_risk_penalty(
    mc_summary: Dict[str, Any],
    max_drawdown_limit: float = 0.50,
    tail_dd_threshold: float = 0.70,
) -> float:
    """
    Compute risk penalty from MC results.
    
    Args:
        mc_summary: MC summary statistics
        max_drawdown_limit: Acceptable max DD threshold
        tail_dd_threshold: Catastrophic DD threshold (reject if exceeded)
    
    Returns:
        Penalty score (0.0 = no penalty, >0 = penalty, large = reject)
    """
    p95_dd = mc_summary.get("p95_max_drawdown", 0.0)
    p99_dd = mc_summary.get("p99_max_drawdown", 0.0)
    worst_dd = mc_summary.get("worst_max_drawdown", 0.0)
    
    # Catastrophic risk: reject if tail DD exceeds threshold
    if abs(worst_dd) > tail_dd_threshold:
        return 1e6  # Large penalty = reject
    
    # Penalty for exceeding acceptable DD
    penalty = 0.0
    if abs(p95_dd) > max_drawdown_limit:
        penalty += abs(p95_dd) - max_drawdown_limit
    
    if abs(p99_dd) > max_drawdown_limit * 1.5:
        penalty += abs(p99_dd) - (max_drawdown_limit * 1.5)
    
    return penalty

