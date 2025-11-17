"""
Metrics comparison module.

Computes and compares live vs staging performance metrics.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, Optional

import pandas as pd

from ..utils import read_json
from ..config import load_config

log = logging.getLogger("rollout.metrics")


@dataclass
class EnvironmentMetrics:
    """Performance metrics for a single environment (live or staging)."""
    # Cumulative metrics
    total_pnl_usdt: float = 0.0
    total_pnl_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    calmar_ratio: float = 0.0
    annualized_return: float = 0.0
    
    # Daily metrics
    daily_pnl_usdt: float = 0.0
    daily_pnl_pct: float = 0.0
    intraday_dd: float = 0.0
    
    # Trade metrics
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate_pct: float = 0.0
    largest_win_usdt: float = 0.0
    largest_loss_usdt: float = 0.0
    
    # Equity metrics
    current_equity: float = 0.0
    start_equity: float = 0.0
    peak_equity: float = 0.0
    
    # Window info
    window_start: Optional[str] = None
    window_end: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict."""
        return {
            "total_pnl_usdt": self.total_pnl_usdt,
            "total_pnl_pct": self.total_pnl_pct,
            "max_drawdown_pct": self.max_drawdown_pct,
            "sharpe_ratio": self.sharpe_ratio,
            "sortino_ratio": self.sortino_ratio,
            "calmar_ratio": self.calmar_ratio,
            "annualized_return": self.annualized_return,
            "daily_pnl_usdt": self.daily_pnl_usdt,
            "daily_pnl_pct": self.daily_pnl_pct,
            "intraday_dd": self.intraday_dd,
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate_pct": self.win_rate_pct,
            "largest_win_usdt": self.largest_win_usdt,
            "largest_loss_usdt": self.largest_loss_usdt,
            "current_equity": self.current_equity,
            "start_equity": self.start_equity,
            "peak_equity": self.peak_equity,
            "window_start": self.window_start,
            "window_end": self.window_end,
        }


@dataclass
class RolloutMetrics:
    """Comparison metrics for live vs staging."""
    live: EnvironmentMetrics = field(default_factory=EnvironmentMetrics)
    staging: EnvironmentMetrics = field(default_factory=EnvironmentMetrics)
    window_start: str = ""
    window_end: str = ""
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict."""
        return {
            "live": self.live.to_dict(),
            "staging": self.staging.to_dict(),
            "window_start": self.window_start,
            "window_end": self.window_end,
        }


def load_state_metrics(
    state_path: str,
    env: str = "live",  # "live" or "staging" (for logging only)
) -> Dict[str, Any]:
    """
    Load metrics from state file for an environment.
    
    Args:
        state_path: Path to state file
        env: Environment name ("live" or "staging")
    
    Returns:
        Metrics dict from state file
    """
    try:
        state = read_json(state_path, default={})
        
        # Extract metrics from state
        metrics = {
            "current_equity": state.get("current_equity", 0.0),
            "day_start_equity": state.get("day_start_equity", 0.0),
            "day_high_equity": state.get("day_high_equity", 0.0),
            "start_equity": state.get("start_equity", state.get("day_start_equity", 0.0)),
            "peak_equity": state.get("peak_equity", state.get("day_high_equity", 0.0)),
            "max_drawdown_pct": state.get("max_drawdown_pct", 0.0),
            "sym_stats": state.get("sym_stats", {}),
        }
        
        return metrics
    except Exception as e:
        log.error(f"Failed to load state metrics from {state_path}: {e}", exc_info=True)
        return {}


def compute_metrics_from_state(
    state_path: str,
    window_start: datetime,
    window_end: Optional[datetime] = None,
    env: str = "live",
) -> EnvironmentMetrics:
    """
    Compute metrics from state file for a given window.
    
    Args:
        state_path: Path to state file
        window_start: Window start time
        window_end: Window end time (defaults to now)
        env: Environment name ("live" or "staging")
    
    Returns:
        EnvironmentMetrics object
    """
    if window_end is None:
        window_end = datetime.utcnow()
    
    state_data = load_state_metrics(state_path, env)
    
    current_equity = float(state_data.get("current_equity", 0.0))
    day_start_equity = float(state_data.get("day_start_equity", current_equity))
    day_high_equity = float(state_data.get("day_high_equity", current_equity))
    start_equity = float(state_data.get("start_equity", current_equity))
    peak_equity = float(state_data.get("peak_equity", current_equity))
    
    # Daily PnL
    daily_pnl_usdt = current_equity - day_start_equity if day_start_equity > 0 else 0.0
    daily_pnl_pct = (daily_pnl_usdt / day_start_equity) if day_start_equity > 0 else 0.0
    
    # Intraday drawdown
    intraday_dd = (current_equity / day_high_equity - 1.0) if day_high_equity > 0 else 0.0
    
    # Cumulative PnL (since window_start)
    # Note: For now, we'll use current equity vs start_equity
    # In a full implementation, we'd track equity at window_start
    total_pnl_usdt = current_equity - start_equity if start_equity > 0 else 0.0
    total_pnl_pct = (total_pnl_usdt / start_equity) if start_equity > 0 else 0.0
    
    # Max drawdown
    max_drawdown_pct = float(state_data.get("max_drawdown_pct", 0.0))
    if peak_equity > 0:
        current_dd = (current_equity / peak_equity - 1.0)
        max_drawdown_pct = min(max_drawdown_pct, current_dd)
    
    # Trade stats (from sym_stats)
    sym_stats = state_data.get("sym_stats", {})
    total_trades = 0
    wins = 0
    losses = 0
    largest_win = 0.0
    largest_loss = 0.0
    
    for symbol, stats in sym_stats.items():
        n = stats.get("n", 0)
        sym_wins = stats.get("wins", 0)
        sym_losses = stats.get("losses", 0)
        sym_pnl = stats.get("ema_pnl", 0.0) or 0.0
        
        total_trades += n
        wins += sym_wins
        losses += sym_losses
        
        if sym_pnl > 0:
            largest_win = max(largest_win, sym_pnl)
        else:
            largest_loss = min(largest_loss, abs(sym_pnl))
    
    win_rate_pct = (wins / total_trades * 100.0) if total_trades > 0 else 0.0
    
    # Approximate Sharpe/Sortino (simplified - would need daily returns series in full implementation)
    # For now, use rule of thumb: sharpe ≈ annualized_return / (volatility)
    # Assuming volatility ≈ abs(daily_pnl_pct) * sqrt(365) for rough estimate
    daily_vol = abs(daily_pnl_pct) * (365 ** 0.5) if abs(daily_pnl_pct) > 0 else 0.01
    annualized_return = daily_pnl_pct * 365  # Rough estimate
    sharpe_ratio = (annualized_return / daily_vol) if daily_vol > 0 else 0.0
    
    # Calmar (annualized_return / max_drawdown)
    calmar_ratio = (annualized_return / abs(max_drawdown_pct)) if abs(max_drawdown_pct) > 0 else 0.0
    
    return EnvironmentMetrics(
        total_pnl_usdt=total_pnl_usdt,
        total_pnl_pct=total_pnl_pct,
        max_drawdown_pct=max_drawdown_pct,
        sharpe_ratio=sharpe_ratio,
        sortino_ratio=sharpe_ratio,  # Simplified (same as Sharpe for now)
        calmar_ratio=calmar_ratio,
        annualized_return=annualized_return,
        daily_pnl_usdt=daily_pnl_usdt,
        daily_pnl_pct=daily_pnl_pct,
        intraday_dd=intraday_dd,
        total_trades=total_trades,
        wins=wins,
        losses=losses,
        win_rate_pct=win_rate_pct,
        largest_win_usdt=largest_win,
        largest_loss_usdt=largest_loss,
        current_equity=current_equity,
        start_equity=start_equity,
        peak_equity=peak_equity,
        window_start=window_start.isoformat(),
        window_end=window_end.isoformat(),
    )


def compute_rollout_metrics(
    live_state_path: str,
    staging_state_path: str,
    window_start: datetime,
    window_end: Optional[datetime] = None,
) -> RolloutMetrics:
    """
    Compute comparison metrics for live vs staging.
    
    Args:
        live_state_path: Path to live state file
        staging_state_path: Path to staging state file
        window_start: Window start time (staging start)
        window_end: Window end time (defaults to now)
    
    Returns:
        RolloutMetrics object
    """
    if window_end is None:
        window_end = datetime.utcnow()
    
    live_metrics = compute_metrics_from_state(live_state_path, window_start, window_end, env="live")
    staging_metrics = compute_metrics_from_state(staging_state_path, window_start, window_end, env="staging")
    
    return RolloutMetrics(
        live=live_metrics,
        staging=staging_metrics,
        window_start=window_start.isoformat(),
        window_end=window_end.isoformat(),
    )

