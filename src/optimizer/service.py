"""
Full optimizer service for continuous parameter optimization.

Supports both one-shot runs and continuous watch mode.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import Optional, Dict, Any
from datetime import datetime, timezone
import json
import hashlib

from ..config import load_config, AppConfig
from .db import OptimizerDB
from .full_cycle import run_full_cycle

log = logging.getLogger("optimizer.service")


def compute_study_name(cfg: AppConfig) -> str:
    """
    Compute deterministic study name from config.
    
    Includes:
    - Strategy version/schema
    - Symbol universe (hash)
    - Timeframe
    
    Args:
        cfg: App config
    
    Returns:
        Study name string
    """
    # Get symbol universe (first few symbols as identifier)
    symbols = cfg.exchange.include_symbols or []
    if not symbols:
        # Use a placeholder - in practice, symbols are fetched dynamically
        symbol_str = "all"
    else:
        symbol_str = "_".join(sorted(symbols[:5]))  # First 5 sorted
    
    # Create a hash of key config elements
    config_hash_input = {
        "timeframe": cfg.exchange.timeframe,
        "symbols": symbol_str,
        "strategy_mode": getattr(cfg.strategy, "mode", "auto"),
    }
    config_hash = hashlib.sha256(
        json.dumps(config_hash_input, sort_keys=True).encode()
    ).hexdigest()[:8]
    
    study_name = f"xsmom_wfo_v1_{symbol_str}_{cfg.exchange.timeframe}_{config_hash}"
    return study_name


def run_optimizer_cycle(
    base_config_path: str,
    live_config_path: str,
    db: OptimizerDB,
    study_id: int,
    study_name: str,
    storage_url: str,
    opt_cfg: Dict[str, Any],
    trials_per_run: int = 25,
    **full_cycle_kwargs,
) -> Dict[str, Any]:
    """
    Run one optimization cycle.
    
    Args:
        base_config_path: Path to base config
        live_config_path: Path to live config
        db: Database instance
        study_id: Study ID
        study_name: Optuna study name
        storage_url: Optuna storage URL
        opt_cfg: Optimizer config dict
        trials_per_run: Number of trials to run this cycle
        **full_cycle_kwargs: Additional args for run_full_cycle
    
    Returns:
        Result dict from run_full_cycle
    """
    log.info(f"Starting optimizer cycle (study_id={study_id}, trials={trials_per_run})")
    
    # Override bo_n_trials with trials_per_run
    full_cycle_kwargs["bo_n_trials"] = trials_per_run
    
    # Run full cycle (this will internally use the DB-aware BayesianOptimizer)
    # Note: We need to update run_wfo_bo_segment to pass DB context
    result = run_full_cycle(
        base_config_path=base_config_path,
        live_config_path=live_config_path,
        db=db,
        study_id=study_id,
        study_name=study_name,
        storage_url=storage_url,
        opt_cfg=opt_cfg,
        **full_cycle_kwargs,
    )
    
    log.info(f"Optimizer cycle complete: {result.get('candidates_evaluated', 0)} candidates evaluated")
    return result


def run_once(
    base_config_path: str,
    live_config_path: str,
    db_path: str,
    trials: int = 25,
    **kwargs,
) -> int:
    """
    Run optimizer once and exit.
    
    Args:
        base_config_path: Path to base config
        live_config_path: Path to live config
        db_path: Path to database file
        trials: Number of trials to run
        **kwargs: Additional args for run_full_cycle
    
    Returns:
        Exit code (0 = success)
    """
    try:
        # Load config
        cfg = load_config(base_config_path)
        
        # Initialize database
        db = OptimizerDB(db_path)
        
        # Compute study name and get/create study
        study_name = compute_study_name(cfg)
        study_id = db.get_or_create_study(
            study_name=study_name,
            description=f"Full-cycle optimizer for {cfg.exchange.timeframe}",
        )
        
        # Build storage URL
        storage_url = f"sqlite:///{db_path}"
        
        # Get optimizer config
        opt_cfg = cfg.optimizer.model_dump() if hasattr(cfg, "optimizer") else {}
        
        # Run cycle
        result = run_optimizer_cycle(
            base_config_path=base_config_path,
            live_config_path=live_config_path,
            db=db,
            study_id=study_id,
            study_name=study_name,
            storage_url=storage_url,
            opt_cfg=opt_cfg,
            trials_per_run=trials,
            **kwargs,
        )
        
        log.info("Optimizer run complete")
        return 0
        
    except Exception as e:
        log.error(f"Optimizer run failed: {e}", exc_info=True)
        return 1


def watch(
    base_config_path: str,
    live_config_path: str,
    db_path: str,
    trials_per_iter: int = 10,
    sleep_seconds: int = 1800,
    **kwargs,
) -> int:
    """
    Run optimizer continuously (watch mode).
    
    Args:
        base_config_path: Path to base config
        live_config_path: Path to live config
        db_path: Path to database file
        trials_per_iter: Number of trials per iteration
        sleep_seconds: Sleep time between iterations
        **kwargs: Additional args for run_full_cycle
    
    Returns:
        Exit code (0 = success, should not return unless error)
    """
    log.info(f"Starting optimizer watch mode (trials_per_iter={trials_per_iter}, sleep={sleep_seconds}s)")
    
    try:
        # Load config once
        cfg = load_config(base_config_path)
        
        # Initialize database
        db = OptimizerDB(db_path)
        
        # Compute study name and get/create study
        study_name = compute_study_name(cfg)
        study_id = db.get_or_create_study(
            study_name=study_name,
            description=f"Full-cycle optimizer for {cfg.exchange.timeframe}",
        )
        
        # Build storage URL
        storage_url = f"sqlite:///{db_path}"
        
        # Get optimizer config
        opt_cfg = cfg.optimizer.model_dump() if hasattr(cfg, "optimizer") else {}
        
        iteration = 0
        while True:
            iteration += 1
            log.info(f"=== Watch iteration {iteration} ===")
            
            try:
                result = run_optimizer_cycle(
                    base_config_path=base_config_path,
                    live_config_path=live_config_path,
                    db=db,
                    study_id=study_id,
                    study_name=study_name,
                    storage_url=storage_url,
                    opt_cfg=opt_cfg,
                    trials_per_run=trials_per_iter,
                    **kwargs,
                )
                
                log.info(f"Iteration {iteration} complete")
                
            except Exception as e:
                log.error(f"Iteration {iteration} failed: {e}", exc_info=True)
                # Continue to next iteration
            
            # Sleep before next iteration
            log.info(f"Sleeping {sleep_seconds}s before next iteration...")
            time.sleep(sleep_seconds)
    
    except KeyboardInterrupt:
        log.info("Watch mode interrupted by user")
        return 0
    except Exception as e:
        log.error(f"Watch mode failed: {e}", exc_info=True)
        return 1


def main():
    """CLI entrypoint for optimizer service."""
    parser = argparse.ArgumentParser(
        description="Optimizer service (database-backed, continuous optimization)"
    )
    
    subparsers = parser.add_subparsers(dest="command", help="Command to run")
    
    # run-once command
    run_once_parser = subparsers.add_parser("run-once", help="Run optimizer once and exit")
    run_once_parser.add_argument(
        "--base-config",
        default="config/config.yaml",
        help="Path to base config",
    )
    run_once_parser.add_argument(
        "--live-config",
        default="config/config.yaml",
        help="Path to live config",
    )
    run_once_parser.add_argument(
        "--db-path",
        default="data/optimizer.db",
        help="Path to optimizer database",
    )
    run_once_parser.add_argument(
        "--trials",
        type=int,
        default=25,
        help="Number of trials to run",
    )
    # Add full_cycle args
    run_once_parser.add_argument("--train-days", type=int, default=120)
    run_once_parser.add_argument("--oos-days", type=int, default=30)
    run_once_parser.add_argument("--embargo-days", type=int, default=2)
    run_once_parser.add_argument("--bo-startup", type=int, default=10)
    run_once_parser.add_argument("--mc-runs", type=int, default=1000)
    run_once_parser.add_argument("--deploy", action="store_true")
    run_once_parser.add_argument("--seed", type=int, default=None)
    
    # watch command
    watch_parser = subparsers.add_parser("watch", help="Run optimizer continuously")
    watch_parser.add_argument(
        "--base-config",
        default="config/config.yaml",
        help="Path to base config",
    )
    watch_parser.add_argument(
        "--live-config",
        default="config/config.yaml",
        help="Path to live config",
    )
    watch_parser.add_argument(
        "--db-path",
        default="data/optimizer.db",
        help="Path to optimizer database",
    )
    watch_parser.add_argument(
        "--trials-per-iter",
        type=int,
        default=10,
        help="Number of trials per iteration",
    )
    watch_parser.add_argument(
        "--sleep-seconds",
        type=int,
        default=1800,
        help="Sleep time between iterations (seconds)",
    )
    # Add full_cycle args
    watch_parser.add_argument("--train-days", type=int, default=120)
    watch_parser.add_argument("--oos-days", type=int, default=30)
    watch_parser.add_argument("--embargo-days", type=int, default=2)
    watch_parser.add_argument("--bo-startup", type=int, default=10)
    watch_parser.add_argument("--mc-runs", type=int, default=1000)
    watch_parser.add_argument("--deploy", action="store_true")
    watch_parser.add_argument("--seed", type=int, default=None)
    
    args = parser.parse_args()
    
    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    
    if args.command == "run-once":
        kwargs = {
            "train_days": args.train_days,
            "oos_days": args.oos_days,
            "embargo_days": args.embargo_days,
            "bo_n_startup": args.bo_startup,
            "mc_n_runs": args.mc_runs,
            "deploy": args.deploy,
            "seed": args.seed,
        }
        sys.exit(run_once(
            base_config_path=args.base_config,
            live_config_path=args.live_config,
            db_path=args.db_path,
            trials=args.trials,
            **kwargs,
        ))
    
    elif args.command == "watch":
        kwargs = {
            "train_days": args.train_days,
            "oos_days": args.oos_days,
            "embargo_days": args.embargo_days,
            "bo_n_startup": args.bo_startup,
            "mc_n_runs": args.mc_runs,
            "deploy": args.deploy,
            "seed": args.seed,
        }
        sys.exit(watch(
            base_config_path=args.base_config,
            live_config_path=args.live_config,
            db_path=args.db_path,
            trials_per_iter=args.trials_per_iter,
            sleep_seconds=args.sleep_seconds,
            **kwargs,
        ))
    
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()

