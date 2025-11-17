"""
Rollout state management.

Manages candidate queue, staging state, and promotion history.
"""
from __future__ import annotations

import logging
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Any

from ..utils import read_json, write_json_atomic

log = logging.getLogger("rollout.state")


class CandidateStatus(str, Enum):
    """Candidate status enum."""
    QUEUED = "queued"
    PAPER = "paper"  # Paper trading stage (MAKE MONEY: added for safety)
    STAGING = "staging"
    PROMOTED = "promoted"
    DISCARDED = "discarded"


class CandidateTier(str, Enum):
    """Candidate tier based on improvement size."""
    A = "A"  # High improvement (fast promotion)
    B = "B"  # Medium improvement (normal promotion)
    C = "C"  # Low improvement (slow promotion)


@dataclass
class Candidate:
    """Candidate config record."""
    id: str  # Unique ID (timestamp + hash)
    config_path: str  # Path to YAML config file
    metadata_path: str  # Path to metadata JSON
    score: float  # Optimizer composite score
    baseline_score: float  # Baseline score at optimization time
    improvement: float  # score - baseline_score
    tier: CandidateTier  # Tier label (A/B/C)
    status: CandidateStatus  # Current status
    created_at: str  # ISO timestamp
    staging_started_at: Optional[str] = None  # ISO timestamp or null
    staging_required_min_duration_days: float = 7.0  # Minimum staging duration
    staging_required_min_trades: int = 300  # Minimum trade count
    
    # Additional metadata from optimization
    baseline_metrics: Dict[str, Any] = field(default_factory=dict)
    candidate_metrics: Dict[str, Any] = field(default_factory=dict)
    params: Dict[str, Any] = field(default_factory=dict)
    
    # Promotion/discard tracking
    promoted_at: Optional[str] = None  # ISO timestamp
    discarded_at: Optional[str] = None  # ISO timestamp
    discard_reason: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for JSON serialization."""
        d = asdict(self)
        # Convert enums to strings
        d["tier"] = self.tier.value
        d["status"] = self.status.value
        return d
    
    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> Candidate:
        """Create from dict."""
        # Convert string enums back to enums
        if isinstance(d.get("tier"), str):
            d["tier"] = CandidateTier(d["tier"])
        if isinstance(d.get("status"), str):
            d["status"] = CandidateStatus(d["status"])
        return cls(**d)


@dataclass
class RolloutState:
    """Rollout state container."""
    live_config_id: Optional[str] = None  # ID of current live config
    staging_config_id: Optional[str] = None  # ID of currently staged candidate
    candidates: Dict[str, Candidate] = field(default_factory=dict)  # All candidates by ID
    queue: List[str] = field(default_factory=list)  # Queue of candidate IDs (sorted by improvement desc)
    updated_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for JSON serialization."""
        return {
            "live_config_id": self.live_config_id,
            "staging_config_id": self.staging_config_id,
            "candidates": {k: v.to_dict() for k, v in self.candidates.items()},
            "queue": self.queue,
            "updated_at": self.updated_at,
        }
    
    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> RolloutState:
        """Create from dict."""
        candidates = {
            k: Candidate.from_dict(v) 
            for k, v in d.get("candidates", {}).items()
        }
        return cls(
            live_config_id=d.get("live_config_id"),
            staging_config_id=d.get("staging_config_id"),
            candidates=candidates,
            queue=d.get("queue", []),
            updated_at=d.get("updated_at", datetime.utcnow().isoformat()),
        )


def compute_score(metrics: Dict[str, Any]) -> float:
    """
    Compute composite score from metrics (same as optimizer).
    
    Args:
        metrics: Metrics dict with sharpe, annualized, calmar, etc.
    
    Returns:
        Composite score (higher is better)
    """
    sharpe = metrics.get("oos_sharpe_mean", metrics.get("sharpe", 0.0))
    annualized = metrics.get("oos_annualized_mean", metrics.get("annualized", 0.0))
    calmar = metrics.get("oos_calmar_mean", metrics.get("calmar", 0.0))
    
    # Same weights as optimizer: sharpe * 0.5 + annualized * 10.0 * 0.3 + calmar * 0.2
    score = sharpe * 0.5 + annualized * 10.0 * 0.3 + calmar * 0.2
    return score


def compute_tier(improvement: float) -> CandidateTier:
    """
    Compute tier based on improvement size.
    
    Args:
        improvement: Score improvement over baseline
    
    Returns:
        Tier (A/B/C)
    """
    # Tier thresholds (configurable)
    high_threshold = 0.15  # High improvement → Tier A
    medium_threshold = 0.05  # Medium improvement → Tier B
    
    if improvement >= high_threshold:
        return CandidateTier.A
    elif improvement >= medium_threshold:
        return CandidateTier.B
    else:
        return CandidateTier.C


def compute_staging_requirements(tier: CandidateTier) -> tuple[float, int]:
    """
    Compute staging requirements based on tier.
    
    Args:
        tier: Candidate tier
    
    Returns:
        (min_duration_days, min_trades)
    """
    # Tier A: Fast promotion (high confidence)
    if tier == CandidateTier.A:
        return (3.0, 100)
    # Tier B: Normal promotion
    elif tier == CandidateTier.B:
        return (7.0, 300)
    # Tier C: Slow promotion (low confidence)
    else:
        return (14.0, 500)


def generate_candidate_id(metadata_path: str) -> str:
    """
    Generate unique candidate ID from metadata path.
    
    Args:
        metadata_path: Path to metadata JSON
    
    Returns:
        Unique ID (timestamp from filename)
    """
    # Extract timestamp from metadata filename: metadata_YYYYMMDD_HHMMSS.json
    path = Path(metadata_path)
    stem = path.stem  # metadata_YYYYMMDD_HHMMSS
    if stem.startswith("metadata_"):
        ts_str = stem.replace("metadata_", "")
        # Validate format (should be YYYYMMDD_HHMMSS)
        if len(ts_str) == 15 and ts_str[8] == "_":
            return ts_str
    
    # Fallback: use file modification time as timestamp string
    import time
    mtime = path.stat().st_mtime
    dt = datetime.fromtimestamp(mtime)
    return dt.strftime("%Y%m%d_%H%M%S")


def load_rollout_state(state_path: str = "data/config_rollout_state.json") -> RolloutState:
    """
    Load rollout state from file.
    
    Args:
        state_path: Path to state file
    
    Returns:
        RolloutState object
    """
    try:
        data = read_json(state_path, default={})
        if not data:
            return RolloutState()
        return RolloutState.from_dict(data)
    except Exception as e:
        log.error(f"Failed to load rollout state: {e}", exc_info=True)
        return RolloutState()


def save_rollout_state(state: RolloutState, state_path: str = "data/config_rollout_state.json") -> None:
    """
    Save rollout state to file (atomic write).
    
    Args:
        state: RolloutState object
        state_path: Path to state file
    """
    state.updated_at = datetime.utcnow().isoformat()
    
    try:
        write_json_atomic(state_path, state.to_dict())
        log.debug(f"Saved rollout state to {state_path}")
    except Exception as e:
        log.error(f"Failed to save rollout state: {e}", exc_info=True)
        raise


def add_candidate_from_metadata(
    metadata_path: str,
    state: Optional[RolloutState] = None,
    state_path: str = "data/config_rollout_state.json",
) -> Candidate:
    """
    Add a new candidate from optimizer metadata.
    
    Args:
        metadata_path: Path to metadata JSON file
        state: Optional RolloutState (if None, loads from file)
        state_path: Path to state file
    
    Returns:
        Created Candidate object
    """
    if state is None:
        state = load_rollout_state(state_path)
    
    # Load metadata
    metadata = read_json(metadata_path, default={})
    if not metadata:
        raise ValueError(f"Failed to load metadata from {metadata_path}")
    
    # Extract paths
    config_path = metadata.get("config_path")
    if not config_path:
        # Infer from metadata path
        md_path = Path(metadata_path)
        config_path = str(md_path.parent / md_path.name.replace("metadata_", "config_").replace(".json", ".yaml"))
    
    # Compute scores
    baseline_metrics = metadata.get("baseline_metrics", {})
    candidate_metrics = metadata.get("candidate_metrics", {})
    
    baseline_score = compute_score(baseline_metrics)
    candidate_score = compute_score(candidate_metrics)
    improvement = candidate_score - baseline_score
    
    # Compute tier and requirements
    tier = compute_tier(improvement)
    min_days, min_trades = compute_staging_requirements(tier)
    
    # Generate ID
    candidate_id = generate_candidate_id(metadata_path)
    
    # Create candidate
    candidate = Candidate(
        id=candidate_id,
        config_path=config_path,
        metadata_path=metadata_path,
        score=candidate_score,
        baseline_score=baseline_score,
        improvement=improvement,
        tier=tier,
        status=CandidateStatus.QUEUED,
        created_at=metadata.get("timestamp", datetime.utcnow().isoformat()),
        staging_required_min_duration_days=min_days,
        staging_required_min_trades=min_trades,
        baseline_metrics=baseline_metrics,
        candidate_metrics=candidate_metrics,
        params=metadata.get("params", {}),
    )
    
    # Add to state
    state.candidates[candidate_id] = candidate
    
    # Insert into queue (sorted by improvement descending)
    queue_insert_sorted(state, candidate_id)
    
    # Save state
    save_rollout_state(state, state_path)
    
    log.info(
        f"Added candidate {candidate_id} to queue: "
        f"tier={tier.value}, improvement={improvement:.4f}, "
        f"staging_req={min_days:.1f} days, {min_trades} trades"
    )
    
    return candidate


def queue_insert_sorted(state: RolloutState, candidate_id: str) -> None:
    """
    Insert candidate ID into queue sorted by improvement (descending).
    
    Args:
        state: RolloutState
        candidate_id: Candidate ID to insert
    """
    candidate = state.candidates.get(candidate_id)
    if not candidate:
        raise ValueError(f"Candidate {candidate_id} not found")
    
    # Only add queued candidates to queue
    if candidate.status != CandidateStatus.QUEUED:
        log.debug(f"Candidate {candidate_id} not queued (status={candidate.status.value}), skipping queue insertion")
        return
    
    # Remove from queue if already present
    if candidate_id in state.queue:
        state.queue.remove(candidate_id)
    
    # Insert in sorted position (by improvement descending)
    inserted = False
    for i, existing_id in enumerate(state.queue):
        existing = state.candidates.get(existing_id)
        if existing and candidate.improvement > existing.improvement:
            state.queue.insert(i, candidate_id)
            inserted = True
            break
    
    # Append to end if not inserted
    if not inserted:
        state.queue.append(candidate_id)
    
    log.debug(f"Inserted candidate {candidate_id} into queue at position {state.queue.index(candidate_id)} (improvement={candidate.improvement:.4f})")


def get_next_candidate_from_queue(state: RolloutState) -> Optional[Candidate]:
    """
    Get next candidate from queue (highest improvement).
    
    Args:
        state: RolloutState
    
    Returns:
        Next candidate or None if queue is empty
    """
    # Filter to queued candidates only
    queued_ids = [cid for cid in state.queue if state.candidates.get(cid, CandidateStatus.QUEUED) == CandidateStatus.QUEUED]
    
    if not queued_ids:
        return None
    
    # Return first (highest improvement)
    candidate_id = queued_ids[0]
    return state.candidates.get(candidate_id)


def update_candidate_status(
    state: RolloutState,
    candidate_id: str,
    status: CandidateStatus,
    state_path: str = "data/config_rollout_state.json",
) -> None:
    """
    Update candidate status.
    
    Args:
        state: RolloutState
        candidate_id: Candidate ID
        status: New status
        state_path: Path to state file
    """
    candidate = state.candidates.get(candidate_id)
    if not candidate:
        raise ValueError(f"Candidate {candidate_id} not found")
    
    old_status = candidate.status
    candidate.status = status
    
    # Update timestamps
    now = datetime.utcnow().isoformat()
    if status == CandidateStatus.STAGING:
        candidate.staging_started_at = now
    elif status == CandidateStatus.PROMOTED:
        candidate.promoted_at = now
    elif status == CandidateStatus.DISCARDED:
        candidate.discarded_at = now
    
    # Remove from queue if not queued
    if status != CandidateStatus.QUEUED and candidate_id in state.queue:
        state.queue.remove(candidate_id)
    
    # Update staging_config_id
    if status == CandidateStatus.STAGING:
        state.staging_config_id = candidate_id
    elif old_status == CandidateStatus.STAGING:
        state.staging_config_id = None
    
    # Update live_config_id
    if status == CandidateStatus.PROMOTED:
        state.live_config_id = candidate_id
    
    save_rollout_state(state, state_path)
    
    log.info(f"Updated candidate {candidate_id} status: {old_status.value} → {status.value}")

