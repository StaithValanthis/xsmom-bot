"""
Query interface for optimizer database.

Provides CLI commands to inspect historical optimization results.
"""
from __future__ import annotations

import argparse
import sys
import json
from pathlib import Path
from typing import Optional, List, Dict, Any
import logging

from .db import OptimizerDB

log = logging.getLogger("optimizer.query")


def list_studies(db_path: str) -> None:
    """List all studies with basic stats."""
    db = OptimizerDB(db_path)
    studies = db.list_studies()
    
    if not studies:
        print("No studies found.")
        return
    
    print(f"\n{'='*80}")
    print(f"{'ID':<6} {'Name':<40} {'Trials':<8} {'Best Score':<12} {'Created':<20}")
    print(f"{'='*80}")
    
    for study in studies:
        best_score = study["best_score"]
        best_str = f"{best_score:.4f}" if best_score is not None else "N/A"
        
        print(
            f"{study['id']:<6} "
            f"{study['name'][:38]:<40} "
            f"{study['trial_count']:<8} "
            f"{best_str:<12} "
            f"{study['created_at']:<20}"
        )
    
    print(f"{'='*80}\n")


def top_trials(
    db_path: str,
    study_name: Optional[str] = None,
    study_id: Optional[int] = None,
    limit: int = 20,
) -> None:
    """Show top N trials by score for a study."""
    db = OptimizerDB(db_path)
    
    # Get study ID
    if study_id is None:
        if study_name is None:
            print("Error: Must provide either --study-name or --study-id")
            return
        
        studies = db.list_studies()
        study = next((s for s in studies if s["name"] == study_name), None)
        if not study:
            print(f"Error: Study '{study_name}' not found")
            return
        study_id = study["id"]
    
    trials = db.get_study_trials(study_id, limit=limit, order_by="score DESC")
    
    if not trials:
        print(f"No trials found for study ID {study_id}")
        return
    
    print(f"\n{'='*100}")
    print(f"Top {len(trials)} trials for study ID {study_id}")
    print(f"{'='*100}")
    print(f"{'Trial #':<10} {'Score':<12} {'Status':<10} {'Key Params':<50} {'Created':<20}")
    print(f"{'='*100}")
    
    for trial in trials:
        params = trial["params"]
        # Show first few key params
        key_params = []
        for k, v in list(params.items())[:3]:
            if isinstance(v, float):
                key_params.append(f"{k}={v:.3f}")
            else:
                key_params.append(f"{k}={v}")
        params_str = ", ".join(key_params)
        if len(params_str) > 48:
            params_str = params_str[:45] + "..."
        
        score_str = f"{trial['score']:.4f}" if trial['score'] is not None else "N/A"
        
        print(
            f"{trial['optuna_trial_number']:<10} "
            f"{score_str:<12} "
            f"{trial['status']:<10} "
            f"{params_str:<50} "
            f"{trial['created_at']:<20}"
        )
    
    print(f"{'='*100}\n")


def show_trial(
    db_path: str,
    study_id: int,
    trial_number: int,
) -> None:
    """Show detailed information for a specific trial."""
    db = OptimizerDB(db_path)
    trials = db.get_study_trials(study_id, limit=None)
    
    trial = next((t for t in trials if t["optuna_trial_number"] == trial_number), None)
    if not trial:
        print(f"Error: Trial {trial_number} not found in study {study_id}")
        return
    
    print(f"\n{'='*80}")
    print(f"Trial {trial_number} (Study ID {study_id})")
    print(f"{'='*80}")
    print(f"Status: {trial['status']}")
    print(f"Score: {trial['score']:.4f}" if trial['score'] is not None else "Score: N/A")
    print(f"Created: {trial['created_at']}")
    print(f"\nParameters:")
    print(json.dumps(trial["params"], indent=2))
    
    if trial["metrics"]:
        print(f"\nMetrics:")
        print(json.dumps(trial["metrics"], indent=2))
    
    print(f"{'='*80}\n")


def bad_regions(
    db_path: str,
    study_id: Optional[int] = None,
    limit: int = 20,
) -> None:
    """Show worst trials / parameter clusters."""
    db = OptimizerDB(db_path)
    
    if study_id is None:
        # Get worst trials across all studies
        studies = db.list_studies()
        all_trials = []
        for study in studies:
            trials = db.get_study_trials(study["id"], limit=None, order_by="score ASC")
            for trial in trials[:limit]:
                trial["study_name"] = study["name"]
                all_trials.append(trial)
        
        # Sort by score (worst first)
        all_trials.sort(key=lambda t: t["score"] or float("-inf"))
        trials = all_trials[:limit]
    else:
        trials = db.get_study_trials(study_id, limit=limit, order_by="score ASC")
    
    if not trials:
        print("No trials found")
        return
    
    print(f"\n{'='*100}")
    print(f"Worst {len(trials)} trials")
    print(f"{'='*100}")
    print(f"{'Study':<30} {'Trial #':<10} {'Score':<12} {'Status':<10} {'Key Params':<30}")
    print(f"{'='*100}")
    
    for trial in trials:
        params = trial["params"]
        key_params = []
        for k, v in list(params.items())[:2]:
            if isinstance(v, float):
                key_params.append(f"{k}={v:.3f}")
            else:
                key_params.append(f"{k}={v}")
        params_str = ", ".join(key_params)
        if len(params_str) > 28:
            params_str = params_str[:25] + "..."
        
        study_name = trial.get("study_name", f"ID {study_id}")
        score_str = f"{trial['score']:.4f}" if trial['score'] is not None else "N/A"
        
        print(
            f"{study_name[:28]:<30} "
            f"{trial['optuna_trial_number']:<10} "
            f"{score_str:<12} "
            f"{trial['status']:<10} "
            f"{params_str:<30}"
        )
    
    print(f"{'='*100}\n")


def main():
    """CLI entrypoint for query interface."""
    parser = argparse.ArgumentParser(
        description="Query optimizer database"
    )
    
    parser.add_argument(
        "--db-path",
        default="data/optimizer.db",
        help="Path to optimizer database",
    )
    
    subparsers = parser.add_subparsers(dest="command", help="Command to run")
    
    # list-studies
    subparsers.add_parser("list-studies", help="List all studies")
    
    # top-trials
    top_trials_parser = subparsers.add_parser("top-trials", help="Show top trials by score")
    top_trials_parser.add_argument("--study-name", help="Study name")
    top_trials_parser.add_argument("--study-id", type=int, help="Study ID")
    top_trials_parser.add_argument("--limit", type=int, default=20, help="Number of trials to show")
    
    # show-trial
    show_trial_parser = subparsers.add_parser("show-trial", help="Show detailed trial info")
    show_trial_parser.add_argument("--study-id", type=int, required=True, help="Study ID")
    show_trial_parser.add_argument("--trial-number", type=int, required=True, help="Trial number")
    
    # bad-regions
    bad_regions_parser = subparsers.add_parser("bad-regions", help="Show worst trials")
    bad_regions_parser.add_argument("--study-id", type=int, help="Study ID (optional, shows all if not provided)")
    bad_regions_parser.add_argument("--limit", type=int, default=20, help="Number of trials to show")
    
    args = parser.parse_args()
    
    # Setup logging (quiet for query interface)
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    
    if args.command == "list-studies":
        list_studies(args.db_path)
    
    elif args.command == "top-trials":
        top_trials(
            args.db_path,
            study_name=args.study_name,
            study_id=args.study_id,
            limit=args.limit,
        )
    
    elif args.command == "show-trial":
        show_trial(
            args.db_path,
            study_id=args.study_id,
            trial_number=args.trial_number,
        )
    
    elif args.command == "bad-regions":
        bad_regions(
            args.db_path,
            study_id=args.study_id,
            limit=args.limit,
        )
    
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()

