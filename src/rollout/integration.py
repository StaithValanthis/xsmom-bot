"""
Integration with optimizer.

Adds rollout candidate creation after optimizer runs.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Any, Optional

from .state import add_candidate_from_metadata, load_rollout_state, RolloutState

log = logging.getLogger("rollout.integration")


def add_optimizer_candidate(
    optimizer_result: Dict[str, Any],
    state_path: str = "data/config_rollout_state.json",
    auto_start_staging: bool = False,
) -> Optional[str]:
    """
    Add a candidate from optimizer result to rollout queue.
    
    Args:
        optimizer_result: Result dict from optimizer (with best_candidate and config_path)
        state_path: Path to rollout state file
        auto_start_staging: If True, automatically start staging if slot is free (default: False)
    
    Returns:
        Candidate ID if added, None otherwise
    """
    try:
        best_candidate = optimizer_result.get("best_candidate")
        if not best_candidate:
            log.info("No best candidate in optimizer result, skipping rollout")
            return None
        
        # Get metadata path from config_path
        config_path = best_candidate.get("config_path")
        if not config_path:
            log.warning("No config_path in optimizer result, skipping rollout")
            return None
        
        config_file = Path(config_path)
        if not config_file.exists():
            log.error(f"Config file does not exist: {config_path}")
            return None
        
        # Infer metadata path from config path
        metadata_path = str(config_file.parent / config_file.name.replace("config_", "metadata_").replace(".yaml", ".json"))
        
        if not Path(metadata_path).exists():
            log.error(f"Metadata file does not exist: {metadata_path}")
            return None
        
        # Add candidate to rollout queue
        candidate = add_candidate_from_metadata(metadata_path, state_path=state_path)
        
        log.info(
            f"Added optimizer candidate {candidate.id} to rollout queue: "
            f"tier={candidate.tier.value}, improvement={candidate.improvement:.4f}, "
            f"queue_position={candidate.id in load_rollout_state(state_path).queue}"
        )
        
        # Optionally start staging if slot is free
        if auto_start_staging:
            from .staging_manager import start_staging_if_free
            start_staging_if_free(state_path=state_path)
        
        return candidate.id
        
    except Exception as e:
        log.error(f"Failed to add optimizer candidate to rollout: {e}", exc_info=True)
        return None

