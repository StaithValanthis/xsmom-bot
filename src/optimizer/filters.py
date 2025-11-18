"""
Parameter filtering and bad-combo detection.

Skips known parameter combinations and filters bad regions.
"""
from __future__ import annotations

import logging
from typing import Dict, Any, Optional, Tuple
from .db import OptimizerDB

log = logging.getLogger("optimizer.filters")


def should_skip_params(
    params: Dict[str, Any],
    study_id: int,
    db: OptimizerDB,
    skip_known: bool = True,
    check_bad_combos: bool = True,
    bad_combo_min_score: Optional[float] = None,
    bad_combo_dd_threshold: Optional[float] = None,
) -> Tuple[bool, Optional[str]]:
    """
    Determine if a parameter combination should be skipped.
    
    Args:
        params: Parameter dictionary
        study_id: Study ID
        db: Database instance
        skip_known: If True, skip already-tested params
        check_bad_combos: If True, check bad combination list
        bad_combo_min_score: Minimum score threshold (below = bad)
        bad_combo_dd_threshold: Max drawdown threshold (above = bad)
    
    Returns:
        Tuple of (should_skip, reason)
    """
    # Check if already tested
    if skip_known:
        existing = db.find_existing_trial_by_params(study_id, params)
        if existing:
            log.debug(
                f"Skipping duplicate params (trial {existing['optuna_trial_number']}, "
                f"score={existing['score']:.4f})"
            )
            return True, f"Already tested (trial {existing['optuna_trial_number']}, score={existing['score']:.4f})"
    
    # Check bad combinations
    if check_bad_combos:
        bad_combo = db.is_bad_combination(study_id, params)
        if bad_combo:
            reason = bad_combo.get("reason", "Marked as bad combination")
            log.debug(f"Skipping bad combo: {reason}")
            return True, reason
        
        # Check if metrics indicate bad combo (if we have historical data)
        if skip_known and existing:
            metrics = existing.get("metrics")
            if metrics:
                # Check score threshold
                if bad_combo_min_score is not None and existing.get("score") is not None:
                    if existing["score"] < bad_combo_min_score:
                        return True, f"Score {existing['score']:.4f} below threshold {bad_combo_min_score}"
                
                # Check drawdown threshold
                if bad_combo_dd_threshold is not None:
                    max_dd = abs(metrics.get("max_drawdown", 0.0))
                    if max_dd > bad_combo_dd_threshold:
                        return True, f"Max DD {max_dd:.2%} exceeds threshold {bad_combo_dd_threshold:.2%}"
    
    return False, None


def mark_bad_from_metrics(
    params: Dict[str, Any],
    metrics: Dict[str, Any],
    study_id: Optional[int],
    db: OptimizerDB,
    bad_combo_min_score: float = -1.0,
    bad_combo_dd_threshold: float = 0.3,
    bad_combo_sharpe_threshold: float = -0.5,
) -> bool:
    """
    Automatically mark a parameter combination as bad based on metrics.
    
    Args:
        params: Parameter dictionary
        metrics: Metrics dictionary
        study_id: Optional study ID
        db: Database instance
        bad_combo_min_score: Minimum score threshold
        bad_combo_dd_threshold: Max drawdown threshold
        bad_combo_sharpe_threshold: Minimum Sharpe threshold
    
    Returns:
        True if marked as bad
    """
    score = metrics.get("score") or metrics.get("objective_score")
    max_dd = abs(metrics.get("max_drawdown", 0.0))
    sharpe = metrics.get("sharpe", 0.0)
    
    reasons = []
    
    if score is not None and score < bad_combo_min_score:
        reasons.append(f"score {score:.4f} < {bad_combo_min_score}")
    
    if max_dd > bad_combo_dd_threshold:
        reasons.append(f"DD {max_dd:.2%} > {bad_combo_dd_threshold:.2%}")
    
    if sharpe < bad_combo_sharpe_threshold:
        reasons.append(f"Sharpe {sharpe:.2f} < {bad_combo_sharpe_threshold}")
    
    if reasons:
        reason = "Bad combo: " + ", ".join(reasons)
        db.mark_bad_combination(study_id, params, reason, score=score)
        log.info(f"Marked as bad combo: {reason}")
        return True
    
    return False

