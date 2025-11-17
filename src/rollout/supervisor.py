"""
Rollout supervisor.

Main orchestrator for staging and promotion lifecycle.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Optional

from .state import load_rollout_state, save_rollout_state, RolloutState
from .staging_manager import start_staging_if_free
from .evaluator import evaluate_staging_candidate, EvaluationDecision
from .promotion import promote_staging_candidate, discard_staging_candidate

log = logging.getLogger("rollout.supervisor")


def run_supervisor(
    state_path: str = "data/config_rollout_state.json",
    live_state_path: str = "state/state.live.json",
    staging_state_path: str = "state/state.staging.json",
    live_config_path: str = "config/config.live.yaml",
    staging_config_path: str = "config/config.staging.yaml",
    promotion_score_threshold: float = 0.0,
    max_dd_increase_tolerance: float = 0.05,
) -> int:
    """
    Run rollout supervisor cycle.
    
    This function:
    1. Loads rollout state
    2. If staging active: evaluates and promotes/discards
    3. If staging free: starts next candidate from queue
    
    Args:
        state_path: Path to rollout state file
        live_state_path: Path to live state file
        staging_state_path: Path to staging state file
        live_config_path: Path to live config file
        staging_config_path: Path to staging config file
        promotion_score_threshold: Minimum promotion score (default: 0.0)
        max_dd_increase_tolerance: Max drawdown increase tolerance (default: 0.05)
    
    Returns:
        Exit code (0 = success, 1 = error)
    """
    log.info("=== ROLLOUT SUPERVISOR ===")
    
    try:
        # Load rollout state
        state = load_rollout_state(state_path)
        
        # If staging is active, evaluate it
        if state.staging_config_id:
            log.info(f"Evaluating active staging candidate: {state.staging_config_id}")
            
            decision = evaluate_staging_candidate(
                state,
                live_state_path,
                staging_state_path,
                promotion_score_threshold,
                max_dd_increase_tolerance,
            )
            
            if not decision:
                log.warning(f"Failed to evaluate staging candidate {state.staging_config_id}")
                return 1
            
            # Handle decision
            if decision.decision == "promote":
                log.info(f"Decision: PROMOTE candidate {state.staging_config_id}")
                
                if promote_staging_candidate(
                    state.staging_config_id,
                    state=state,
                    state_path=state_path,
                    live_config_path=live_config_path,
                ):
                    log.info(f"✅ Candidate {state.staging_config_id} promoted to live")
                else:
                    log.error(f"❌ Failed to promote candidate {state.staging_config_id}")
                    return 1
                
                # Staging slot is now free (promote_staging_candidate updates state)
                # Will start next candidate below
                
            elif decision.decision == "discard":
                log.info(f"Decision: DISCARD candidate {state.staging_config_id}")
                
                if discard_staging_candidate(
                    state.staging_config_id,
                    reason=decision.reason,
                    state=state,
                    state_path=state_path,
                ):
                    log.info(f"✅ Candidate {state.staging_config_id} discarded")
                else:
                    log.error(f"❌ Failed to discard candidate {state.staging_config_id}")
                    return 1
                
                # Staging slot is now free (discard_staging_candidate updates state)
                # Will start next candidate below
                
            elif decision.decision == "continue":
                log.info(f"Decision: CONTINUE staging candidate {state.staging_config_id}: {decision.reason}")
                # Staging continues, nothing to do
                return 0
                
            else:
                log.error(f"Unknown decision: {decision.decision}")
                return 1
        
        # If staging is free, start next candidate from queue
        if not state.staging_config_id:
            log.info("Staging slot is free, checking queue...")
            
            candidate_id = start_staging_if_free(
                state=state,
                state_path=state_path,
                staging_config_path=staging_config_path,
            )
            
            if candidate_id:
                log.info(f"✅ Started staging for candidate {candidate_id}")
            else:
                log.info("No candidates in queue or staging start failed")
        else:
            log.info(f"Staging slot is busy: {state.staging_config_id}")
        
        log.info("=== SUPERVISOR CYCLE COMPLETE ===")
        return 0
        
    except Exception as e:
        log.error(f"Supervisor cycle failed: {e}", exc_info=True)
        return 1


def main():
    """CLI entrypoint for rollout supervisor."""
    parser = argparse.ArgumentParser(
        description="Rollout supervisor: manages staging and promotion lifecycle"
    )
    parser.add_argument(
        "--state",
        type=str,
        default="data/config_rollout_state.json",
        help="Path to rollout state file",
    )
    parser.add_argument(
        "--live-state",
        type=str,
        default="state/state.live.json",
        help="Path to live state file",
    )
    parser.add_argument(
        "--staging-state",
        type=str,
        default="state/state.staging.json",
        help="Path to staging state file",
    )
    parser.add_argument(
        "--live-config",
        type=str,
        default="config/config.live.yaml",
        help="Path to live config file",
    )
    parser.add_argument(
        "--staging-config",
        type=str,
        default="config/config.staging.yaml",
        help="Path to staging config file",
    )
    parser.add_argument(
        "--promotion-score-threshold",
        type=float,
        default=0.0,
        help="Minimum promotion score (default: 0.0)",
    )
    parser.add_argument(
        "--max-dd-increase-tolerance",
        type=float,
        default=0.05,
        help="Max drawdown increase tolerance (default: 0.05 = 5%)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )
    
    args = parser.parse_args()
    
    # Setup logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    
    exit_code = run_supervisor(
        state_path=args.state,
        live_state_path=args.live_state,
        staging_state_path=args.staging_state,
        live_config_path=args.live_config,
        staging_config_path=args.staging_config,
        promotion_score_threshold=args.promotion_score_threshold,
        max_dd_increase_tolerance=args.max_dd_increase_tolerance,
    )
    
    sys.exit(exit_code)


if __name__ == "__main__":
    main()

