"""
Promotion and discard handlers.

Handles promoting staged candidates to live or discarding them.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from .state import (
    RolloutState,
    Candidate,
    CandidateStatus,
    update_candidate_status,
    load_rollout_state,
    save_rollout_state,
)
from .staging_manager import set_live_config, set_staging_config

log = logging.getLogger("rollout.promotion")


def promote_staging_candidate(
    candidate_id: str,
    state: Optional[RolloutState] = None,
    state_path: str = "data/config_rollout_state.json",
    live_config_path: str = "config/config.live.yaml",
) -> bool:
    """
    Promote a staged candidate to live.
    
    Args:
        candidate_id: Candidate ID
        state: Optional RolloutState (if None, loads from file)
        state_path: Path to state file
        live_config_path: Path to live config file
    
    Returns:
        True if successful
    """
    if state is None:
        state = load_rollout_state(state_path)
    
    candidate = state.candidates.get(candidate_id)
    if not candidate:
        log.error(f"Candidate {candidate_id} not found")
        return False
    
    if candidate.status != CandidateStatus.STAGING:
        log.error(f"Candidate {candidate_id} is not in staging status: {candidate.status.value}")
        return False
    
    # Set live config
    if not set_live_config(candidate.config_path, live_config_path):
        log.error(f"Failed to set live config for candidate {candidate_id}")
        return False
    
    # Update state
    old_live_id = state.live_config_id
    update_candidate_status(state, candidate_id, CandidateStatus.PROMOTED, state_path)
    
    log.info(
        f"✅ PROMOTED: Candidate {candidate_id} (tier={candidate.tier.value}, "
        f"improvement={candidate.improvement:.4f}) is now LIVE. "
        f"Previous live: {old_live_id or 'none'}"
    )
    
    # Log instructions for operator
    log.info(
        f"PROMOTION COMPLETE: Candidate {candidate_id} is now live. "
        f"Ensure live bot is running with config: {live_config_path}. "
        f"Restart live bot to pick up new config: "
        f"sudo systemctl restart xsmom-bot.service"
    )
    
    return True


def discard_staging_candidate(
    candidate_id: str,
    reason: str,
    state: Optional[RolloutState] = None,
    state_path: str = "data/config_rollout_state.json",
) -> bool:
    """
    Discard a staged candidate.
    
    Args:
        candidate_id: Candidate ID
        reason: Discard reason
        state: Optional RolloutState (if None, loads from file)
        state_path: Path to state file
    
    Returns:
        True if successful
    """
    if state is None:
        state = load_rollout_state(state_path)
    
    candidate = state.candidates.get(candidate_id)
    if not candidate:
        log.error(f"Candidate {candidate_id} not found")
        return False
    
    if candidate.status != CandidateStatus.STAGING:
        log.error(f"Candidate {candidate_id} is not in staging status: {candidate.status.value}")
        return False
    
    # Update candidate with discard reason
    candidate.discard_reason = reason
    candidate.discarded_at = datetime.utcnow().isoformat()
    
    # Update state
    update_candidate_status(state, candidate_id, CandidateStatus.DISCARDED, state_path)
    
    log.info(
        f"❌ DISCARDED: Candidate {candidate_id} (tier={candidate.tier.value}, "
        f"improvement={candidate.improvement:.4f}) was discarded. "
        f"Reason: {reason}"
    )
    
    return True

