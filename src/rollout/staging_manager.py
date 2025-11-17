"""
Staging manager.

Handles starting and stopping staging for candidates.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Optional
from datetime import datetime

from .state import (
    RolloutState,
    CandidateStatus,
    get_next_candidate_from_queue,
    update_candidate_status,
    load_rollout_state,
    save_rollout_state,
)

log = logging.getLogger("rollout.staging_manager")


def set_live_config(
    config_path: str,
    live_config_path: str = "config/config.live.yaml",
) -> bool:
    """
    Set live config by copying candidate config to live location.
    
    Args:
        config_path: Path to candidate config file
        live_config_path: Path to live config file
    
    Returns:
        True if successful
    """
    config_file = Path(config_path)
    live_file = Path(live_config_path)
    
    if not config_file.exists():
        log.error(f"Config file does not exist: {config_path}")
        return False
    
    try:
        # Create parent directory if needed
        live_file.parent.mkdir(parents=True, exist_ok=True)
        
        # Copy config to live location
        shutil.copy2(config_file, live_file)
        
        log.info(f"Set live config: {config_path} → {live_config_path}")
        return True
    except Exception as e:
        log.error(f"Failed to set live config: {e}", exc_info=True)
        return False


def set_staging_config(
    config_path: str,
    staging_config_path: str = "config/config.staging.yaml",
) -> bool:
    """
    Set staging config by copying candidate config to staging location.
    
    Args:
        config_path: Path to candidate config file
        staging_config_path: Path to staging config file
    
    Returns:
        True if successful
    """
    config_file = Path(config_path)
    staging_file = Path(staging_config_path)
    
    if not config_file.exists():
        log.error(f"Config file does not exist: {config_path}")
        return False
    
    try:
        # Create parent directory if needed
        staging_file.parent.mkdir(parents=True, exist_ok=True)
        
        # Copy config to staging location
        shutil.copy2(config_file, staging_file)
        
        log.info(f"Set staging config: {config_path} → {staging_config_path}")
        return True
    except Exception as e:
        log.error(f"Failed to set staging config: {e}", exc_info=True)
        return False


def start_staging_if_free(
    state: Optional[RolloutState] = None,
    state_path: str = "data/config_rollout_state.json",
    staging_config_path: str = "config/config.staging.yaml",
) -> Optional[str]:
    """
    Start staging if slot is free.
    
    Args:
        state: Optional RolloutState (if None, loads from file)
        state_path: Path to state file
        staging_config_path: Path to staging config file
    
    Returns:
        Candidate ID if staging started, None otherwise
    """
    if state is None:
        state = load_rollout_state(state_path)
    
    # Check if staging slot is busy
    if state.staging_config_id is not None:
        log.debug(f"Staging slot busy: {state.staging_config_id}")
        return None
    
    # Get next candidate from queue
    candidate = get_next_candidate_from_queue(state)
    if not candidate:
        log.debug("No candidates in queue")
        return None
    
    # Set staging config
    if not set_staging_config(candidate.config_path, staging_config_path):
        log.error(f"Failed to set staging config for candidate {candidate.id}")
        return None
    
    # Update candidate status
    update_candidate_status(state, candidate.id, CandidateStatus.STAGING, state_path)
    
    log.info(
        f"Started staging for candidate {candidate.id}: "
        f"tier={candidate.tier.value}, improvement={candidate.improvement:.4f}, "
        f"staging_req={candidate.staging_required_min_duration_days:.1f} days, "
        f"{candidate.staging_required_min_trades} trades"
    )
    
    # Log instructions for operator
    log.info(
        f"STAGING STARTED: Candidate {candidate.id} is now staged. "
        f"Ensure staging bot is running with config: {staging_config_path}"
    )
    
    return candidate.id


def stop_staging(
    candidate_id: str,
    state: Optional[RolloutState] = None,
    state_path: str = "data/config_rollout_state.json",
) -> None:
    """
    Stop staging for a candidate.
    
    Note: This only updates state; does not change candidate status.
    Use promotion/discard handlers to set final status.
    
    Args:
        candidate_id: Candidate ID
        state: Optional RolloutState (if None, loads from file)
        state_path: Path to state file
    """
    if state is None:
        state = load_rollout_state(state_path)
    
    candidate = state.candidates.get(candidate_id)
    if not candidate:
        raise ValueError(f"Candidate {candidate_id} not found")
    
    if candidate.status != CandidateStatus.STAGING:
        log.warning(f"Candidate {candidate_id} is not in staging status: {candidate.status.value}")
        return
    
    # Clear staging slot (but keep status as STAGING until promotion/discard)
    # The evaluator will update status to PROMOTED or DISCARDED
    log.info(f"Stopped staging for candidate {candidate_id} (status will be updated by evaluator)")

