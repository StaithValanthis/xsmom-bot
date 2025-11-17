"""
Staging evaluator.

Decides whether to promote, discard, or continue staging a candidate.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, Tuple

from .state import Candidate, RolloutState, CandidateTier, CandidateStatus
from .metrics import compute_rollout_metrics, RolloutMetrics, EnvironmentMetrics

log = logging.getLogger("rollout.evaluator")


@dataclass
class EvaluationDecision:
    """Evaluation decision result."""
    decision: str  # "promote", "discard", or "continue"
    reason: str  # Human-readable reason
    metrics: Dict[str, Any]  # Metrics used for decision
    promotion_score: float = 0.0  # Promotion score (>0 = promote)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict."""
        return {
            "decision": self.decision,
            "reason": self.reason,
            "metrics": self.metrics,
            "promotion_score": self.promotion_score,
        }


def compute_promotion_score(
    live_metrics: EnvironmentMetrics,
    staging_metrics: EnvironmentMetrics,
    alpha: float = 0.4,
    beta: float = 0.3,
    gamma: float = 0.3,
) -> float:
    """
    Compute promotion score from live vs staging metrics.
    
    Args:
        live_metrics: Live environment metrics
        staging_metrics: Staging environment metrics
        alpha: Weight for CAGR improvement (default: 0.4)
        beta: Weight for Sharpe improvement (default: 0.3)
        gamma: Weight for drawdown penalty (default: 0.3)
    
    Returns:
        Promotion score (>0 = promote, <=0 = discard/continue)
    """
    # CAGR improvement
    cagr_stage = staging_metrics.annualized_return
    cagr_live = live_metrics.annualized_return
    cagr_improve = cagr_stage - cagr_live
    
    # Sharpe improvement
    sharpe_stage = staging_metrics.sharpe_ratio
    sharpe_live = live_metrics.sharpe_ratio
    sharpe_improve = sharpe_stage - sharpe_live
    
    # Drawdown penalty
    dd_stage = abs(staging_metrics.max_drawdown_pct)
    dd_live = abs(live_metrics.max_drawdown_pct)
    dd_increase = dd_stage - dd_live  # Positive = worse
    
    # Compute promotion score
    # Formula: α * CAGR_improve + β * Sharpe_improve - γ * DD_increase
    score = (
        alpha * cagr_improve +
        beta * sharpe_improve -
        gamma * dd_increase
    )
    
    return score


def check_eligibility(
    candidate: Candidate,
    staging_start_time: datetime,
    staging_metrics: EnvironmentMetrics,
) -> tuple[bool, str]:
    """
    Check if candidate meets minimum eligibility requirements.
    
    Args:
        candidate: Candidate object
        staging_start_time: When staging started
        staging_metrics: Staging environment metrics
    
    Returns:
        (is_eligible, reason)
    """
    now = datetime.utcnow()
    duration = (now - staging_start_time).total_seconds() / (24 * 3600)  # Days
    
    # Check minimum duration
    if duration < candidate.staging_required_min_duration_days:
        return False, f"Staging duration ({duration:.1f} days) < required ({candidate.staging_required_min_duration_days:.1f} days)"
    
    # Check minimum trades
    if staging_metrics.total_trades < candidate.staging_required_min_trades:
        return False, f"Staging trades ({staging_metrics.total_trades}) < required ({candidate.staging_required_min_trades})"
    
    return True, "Eligible"


def check_live_rollback(
    state: RolloutState,
    live_state_path: str,
    backtest_sharpe: float,
    rollback_sharpe_gap_threshold: float = 0.5,
    min_days_for_evaluation: int = 7,
) -> Tuple[bool, str, float]:
    """
    Check if live performance has degraded and rollback is needed.
    
    **MAKE MONEY HARDENING**: Monitors live vs backtest performance.
    If live Sharpe < backtest Sharpe by threshold, triggers rollback.
    
    Args:
        state: RolloutState
        live_state_path: Path to live state file
        backtest_sharpe: Backtest Sharpe ratio from config metadata
        rollback_sharpe_gap_threshold: Minimum Sharpe gap to trigger rollback (default: 0.5)
        min_days_for_evaluation: Minimum days of live data before evaluation (default: 7)
    
    Returns:
        Tuple of (should_rollback, reason, live_sharpe)
    """
    if not state.live_config_id:
        return False, "No live config to evaluate", 0.0
    
    # Get live metrics
    try:
        from .metrics import compute_rollout_metrics
        from datetime import datetime, timedelta
        
        now = datetime.utcnow()
        start = now - timedelta(days=min_days_for_evaluation)
        
        # Compute live metrics over evaluation window
        # (This is simplified - would need actual live metrics collection)
        # For now, return False (no rollback) - needs integration with live metrics
        return False, "Rollback check not yet integrated with live metrics", 0.0
    except Exception as e:
        log.warning(f"Rollback check failed: {e}")
        return False, f"Rollback check error: {e}", 0.0


def evaluate_candidate(
    candidate: Candidate,
    state: RolloutState,
    live_state_path: str,
    staging_state_path: str,
    promotion_score_threshold: float = 0.0,
    max_dd_increase_tolerance: float = 0.05,
) -> EvaluationDecision:
    """
    Evaluate a staged candidate to decide promote/discard/continue.
    
    Args:
        candidate: Candidate object
        state: RolloutState
        live_state_path: Path to live state file
        staging_state_path: Path to staging state file
        promotion_score_threshold: Minimum promotion score (default: 0.0)
        max_dd_increase_tolerance: Max drawdown increase tolerance (default: 0.05 = 5%)
    
    Returns:
        EvaluationDecision object
    """
    if not candidate.staging_started_at:
        return EvaluationDecision(
            decision="continue",
            reason="Staging not started yet",
            metrics={},
            promotion_score=0.0,
        )
    
    # Parse staging start time
    staging_start = datetime.fromisoformat(candidate.staging_started_at.replace("Z", "+00:00"))
    now = datetime.utcnow()
    
    # Compute metrics
    rollout_metrics = compute_rollout_metrics(
        live_state_path,
        staging_state_path,
        staging_start,
        now,
    )
    
    # Check eligibility
    is_eligible, eligibility_reason = check_eligibility(
        candidate,
        staging_start,
        rollout_metrics.staging,
    )
    
    if not is_eligible:
        return EvaluationDecision(
            decision="continue",
            reason=eligibility_reason,
            metrics=rollout_metrics.to_dict(),
            promotion_score=0.0,
        )
    
    # Compute promotion score
    promotion_score = compute_promotion_score(
        rollout_metrics.live,
        rollout_metrics.staging,
    )
    
    # Check drawdown increase
    dd_increase = (
        abs(rollout_metrics.staging.max_drawdown_pct) - 
        abs(rollout_metrics.live.max_drawdown_pct)
    )
    dd_too_worse = dd_increase > max_dd_increase_tolerance
    
    # Decision logic
    if promotion_score > promotion_score_threshold and not dd_too_worse:
        decision = "promote"
        reason = (
            f"Promotion score ({promotion_score:.4f}) > threshold ({promotion_score_threshold:.4f}) "
            f"and drawdown increase ({dd_increase:.2%}) < tolerance ({max_dd_increase_tolerance:.2%}). "
            f"Staging: CAGR={rollout_metrics.staging.annualized_return:.2%}, "
            f"Sharpe={rollout_metrics.staging.sharpe_ratio:.2f}, "
            f"DD={rollout_metrics.staging.max_drawdown_pct:.2%}. "
            f"Live: CAGR={rollout_metrics.live.annualized_return:.2%}, "
            f"Sharpe={rollout_metrics.live.sharpe_ratio:.2f}, "
            f"DD={rollout_metrics.live.max_drawdown_pct:.2%}"
        )
    elif dd_too_worse:
        decision = "discard"
        reason = (
            f"Drawdown increase ({dd_increase:.2%}) > tolerance ({max_dd_increase_tolerance:.2%}). "
            f"Staging DD={rollout_metrics.staging.max_drawdown_pct:.2%}, "
            f"Live DD={rollout_metrics.live.max_drawdown_pct:.2%}"
        )
    elif promotion_score <= promotion_score_threshold:
        decision = "discard"
        reason = (
            f"Promotion score ({promotion_score:.4f}) <= threshold ({promotion_score_threshold:.4f}). "
            f"Staging underperformed live."
        )
    else:
        decision = "continue"
        reason = "Performance inconclusive, continue staging"
    
    return EvaluationDecision(
        decision=decision,
        reason=reason,
        metrics=rollout_metrics.to_dict(),
        promotion_score=promotion_score,
    )


def evaluate_staging_candidate(
    state: RolloutState,
    live_state_path: str,
    staging_state_path: str,
    promotion_score_threshold: float = 0.0,
    max_dd_increase_tolerance: float = 0.05,
) -> Optional[EvaluationDecision]:
    """
    Evaluate currently staged candidate.
    
    Args:
        state: RolloutState
        live_state_path: Path to live state file
        staging_state_path: Path to staging state file
        promotion_score_threshold: Minimum promotion score (default: 0.0)
        max_dd_increase_tolerance: Max drawdown increase tolerance (default: 0.05)
    
    Returns:
        EvaluationDecision or None if no staging candidate
    """
    if not state.staging_config_id:
        log.debug("No staging candidate to evaluate")
        return None
    
    candidate = state.candidates.get(state.staging_config_id)
    if not candidate:
        log.error(f"Staging candidate {state.staging_config_id} not found in state")
        return None
    
    if candidate.status != CandidateStatus.STAGING:
        log.warning(f"Candidate {candidate.id} is not in staging status: {candidate.status.value}")
        return None
    
    log.info(f"Evaluating staging candidate {candidate.id}: tier={candidate.tier.value}, improvement={candidate.improvement:.4f}")
    
    decision = evaluate_candidate(
        candidate,
        state,
        live_state_path,
        staging_state_path,
        promotion_score_threshold,
        max_dd_increase_tolerance,
    )
    
    log.info(
        f"Evaluation result for candidate {candidate.id}: {decision.decision} "
        f"(promotion_score={decision.promotion_score:.4f}, reason={decision.reason})"
    )
    
    return decision

