"""
Bayesian optimization runner using Optuna.

Provides TPE-based optimization over parameter space with database persistence.
"""
from __future__ import annotations

import logging
from typing import Dict, Any, List, Optional, Callable, Tuple
from dataclasses import dataclass
import json
from pathlib import Path

try:
    import optuna
    from optuna.samplers import TPESampler
    OPTUNA_AVAILABLE = True
except ImportError:
    OPTUNA_AVAILABLE = False
    optuna = None
    TPESampler = None

log = logging.getLogger("optimizer.bo_runner")


@dataclass
class ParameterSpace:
    """
    Parameter space definition for Bayesian optimization.
    
    Each parameter can be:
    - Continuous: {"type": "float", "low": 1.0, "high": 2.0}
    - Integer: {"type": "int", "low": 2, "high": 8}
    - Categorical: {"type": "categorical", "choices": [True, False]}
    - Log-uniform: {"type": "loguniform", "low": 0.001, "high": 0.1}
    """
    space: Dict[str, Dict[str, Any]]
    
    def validate(self) -> None:
        """Validate parameter space definition."""
        for name, spec in self.space.items():
            ptype = spec.get("type")
            if ptype not in ["float", "int", "categorical", "loguniform"]:
                raise ValueError(f"Invalid parameter type for {name}: {ptype}")
            
            if ptype in ["float", "int", "loguniform"]:
                if "low" not in spec or "high" not in spec:
                    raise ValueError(f"Missing bounds for {name}")
                if spec["low"] >= spec["high"]:
                    raise ValueError(f"Invalid bounds for {name}: low >= high")


class BayesianOptimizer:
    """Bayesian optimizer using Optuna TPE sampler with database persistence."""
    
    def __init__(
        self,
        param_space: ParameterSpace,
        objective_fn: Callable[[Dict[str, Any]], float],
        n_trials: int = 100,
        n_startup_trials: int = 10,
        seed: Optional[int] = None,
        study_name: Optional[str] = None,
        storage_url: Optional[str] = None,
        db: Optional[Any] = None,  # OptimizerDB
        study_id: Optional[int] = None,
        skip_known_params: bool = True,
        check_bad_combos: bool = True,
        bad_combo_min_score: Optional[float] = None,
        bad_combo_dd_threshold: Optional[float] = None,
    ):
        """
        Initialize Bayesian optimizer.
        
        Args:
            param_space: Parameter space definition
            objective_fn: Function that takes params dict -> float score
            n_trials: Total number of trials
            n_startup_trials: Random trials before BO starts
            seed: Random seed
            study_name: Optuna study name (for persistence)
            storage_url: Optuna storage URL (e.g., "sqlite:///data/optimizer.db")
            db: OptimizerDB instance (for trial recording)
            study_id: Study ID in database
            skip_known_params: Skip already-tested parameter combinations
            check_bad_combos: Check bad combination list
            bad_combo_min_score: Minimum score threshold
            bad_combo_dd_threshold: Max drawdown threshold
        """
        if not OPTUNA_AVAILABLE:
            raise ImportError(
                "Optuna not installed. Install with: pip install optuna"
            )
        
        param_space.validate()
        self.param_space = param_space
        self.objective_fn = objective_fn
        self.n_trials = n_trials
        self.n_startup_trials = n_startup_trials
        self.seed = seed
        self.study_name = study_name
        self.storage_url = storage_url
        self.db = db
        self.study_id = study_id
        self.skip_known_params = skip_known_params
        self.check_bad_combos = check_bad_combos
        self.bad_combo_min_score = bad_combo_min_score
        self.bad_combo_dd_threshold = bad_combo_dd_threshold
        
        self.study: Optional[optuna.Study] = None
        self.trial_results: List[Dict[str, Any]] = []
    
    def optimize(self) -> Tuple[Dict[str, Any], float]:
        """
        Run Bayesian optimization.
        
        Returns:
            Tuple of (best_params, best_score)
        """
        sampler = TPESampler(
            n_startup_trials=self.n_startup_trials,
            seed=self.seed,
        )
        
        # Create study with persistence if storage_url provided
        study_kwargs = {
            "direction": "maximize",
            "sampler": sampler,
        }
        
        if self.storage_url and self.study_name:
            study_kwargs["storage"] = self.storage_url
            study_kwargs["study_name"] = self.study_name
            study_kwargs["load_if_exists"] = True
        
        self.study = optuna.create_study(**study_kwargs)
        
        # Import filters here to avoid circular import
        from .filters import should_skip_params
        
        def objective(trial: optuna.Trial) -> float:
            # Suggest parameters based on space
            params = {}
            for name, spec in self.param_space.space.items():
                ptype = spec["type"]
                
                if ptype == "float":
                    params[name] = trial.suggest_float(
                        name, spec["low"], spec["high"]
                    )
                elif ptype == "int":
                    params[name] = trial.suggest_int(
                        name, spec["low"], spec["high"]
                    )
                elif ptype == "categorical":
                    params[name] = trial.suggest_categorical(
                        name, spec["choices"]
                    )
                elif ptype == "loguniform":
                    params[name] = trial.suggest_loguniform(
                        name, spec["low"], spec["high"]
                    )
            
            # Check if we should skip this parameter combination
            if self.db and self.study_id is not None:
                should_skip, reason = should_skip_params(
                    params,
                    self.study_id,
                    self.db,
                    skip_known=self.skip_known_params,
                    check_bad_combos=self.check_bad_combos,
                    bad_combo_min_score=self.bad_combo_min_score,
                    bad_combo_dd_threshold=self.bad_combo_dd_threshold,
                )
                
                if should_skip:
                    # If we have existing results, reuse them
                    existing = self.db.find_existing_trial_by_params(self.study_id, params)
                    if existing and existing.get("score") is not None:
                        log.info(f"Trial {trial.number}: Reusing existing result - {reason}")
                        # Record to Optuna study
                        trial.set_user_attr("skipped", True)
                        trial.set_user_attr("skip_reason", reason)
                        # Return existing score (Optuna will record it)
                        return existing["score"]
                    else:
                        # Prune trial if it's a bad combo
                        log.info(f"Trial {trial.number}: Pruning - {reason}")
                        raise optuna.TrialPruned(reason)
            
            # Evaluate objective
            try:
                score = self.objective_fn(params)
                
                # Store trial result in memory
                self.trial_results.append({
                    "trial_number": trial.number,
                    "params": params,
                    "score": score,
                })
                
                # Record to database if available
                if self.db and self.study_id is not None:
                    # Try to get metrics from objective_fn if it returns a dict
                    metrics = None
                    if hasattr(self.objective_fn, '__annotations__'):
                        # If objective_fn returns a dict with metrics, extract it
                        # For now, we'll just store the score
                        pass
                    
                    self.db.record_trial_result(
                        study_id=self.study_id,
                        optuna_trial_number=trial.number,
                        params=params,
                        metrics=metrics,
                        score=score,
                        status="complete",
                    )
                
                return score
            except Exception as e:
                log.warning(f"Trial {trial.number} failed: {e}")
                
                # Record failure to database
                if self.db and self.study_id is not None:
                    self.db.record_trial_result(
                        study_id=self.study_id,
                        optuna_trial_number=trial.number,
                        params=params,
                        metrics=None,
                        score=float("-inf"),
                        status="fail",
                    )
                
                return float("-inf")
        
        self.study.optimize(objective, n_trials=self.n_trials)
        
        if not self.study.best_trial:
            raise RuntimeError("Optimization produced no valid trials")
        
        best_params = self.study.best_trial.params
        best_score = self.study.best_trial.value
        
        log.info(
            f"BO complete: {self.n_trials} trials, "
            f"best_score={best_score:.4f}"
        )
        
        return best_params, best_score
    
    def get_trial_history(self) -> List[Dict[str, Any]]:
        """Get all trial results."""
        return self.trial_results.copy()


def define_parameter_space(
    core_params: Optional[Dict[str, Dict[str, Any]]] = None,
) -> ParameterSpace:
    """
    Define default parameter space for xsmom-bot optimization.
    
    **ROADMAP IMPLEMENTATION**: Reduced to 10-12 core parameters per roadmap recommendations.
    All other parameters are frozen to fixed values based on economic rationale.
    
    Core parameters (10-12 total per roadmap):
    - Signal generation: signal_power (narrowed), lookbacks[0] (short), lookbacks[2] (long, skip medium)
    - Position selection: k_min, k_max
    - Filters: regime_filter.slope_min_bps_per_day (ema_len locked at 200, entry_zscore_min locked at 0.0)
    - Risk: atr_mult_sl (trail_atr_mult locked at 1.0, max_weight_per_asset locked at 0.10)
    - Sizing: gross_leverage (narrowed), portfolio_vol_target (narrowed), vol_lookback (NEW), carry.budget_frac (NEW)
    
    Args:
        core_params: Optional custom parameter definitions
    
    Returns:
        ParameterSpace object
    """
    if core_params is None:
        # Roadmap: Reduced to 10-12 core parameters (from 15)
        # Based on STRATEGY_IMPROVEMENT_ROADMAP.md recommendations
        core_params = {
            # Signals (narrowed ranges)
            "strategy.signal_power": {
                "type": "float",
                "low": 1.0,
                "high": 1.5,  # Narrowed from 2.0 (roadmap)
            },
            "strategy.lookbacks[0]": {  # Short lookback (hours)
                "type": "int",
                "low": 6,
                "high": 24,
            },
            # Medium lookback removed (roadmap: skip optimizing medium, derive or fix)
            "strategy.lookbacks[2]": {  # Long lookback (hours)
                "type": "int",
                "low": 48,  # Narrowed from 24 (roadmap)
                "high": 96,
            },
            "strategy.k_min": {
                "type": "int",
                "low": 2,
                "high": 4,
            },
            "strategy.k_max": {
                "type": "int",
                "low": 4,
                "high": 8,
            },
            # Filters (ema_len and entry_zscore_min removed, locked at defaults)
            "strategy.regime_filter.slope_min_bps_per_day": {
                "type": "float",
                "low": 1.0,
                "high": 5.0,
            },
            # Risk (trail_atr_mult and max_weight_per_asset removed, locked at defaults)
            "risk.atr_mult_sl": {
                "type": "float",
                "low": 1.5,
                "high": 3.0,
            },
            "strategy.gross_leverage": {
                "type": "float",
                "low": 0.75,  # Widened to include lower leverage (roadmap)
                "high": 1.5,  # Narrowed from 2.0 (roadmap)
            },
            # Sizing (narrowed ranges)
            "strategy.portfolio_vol_target.target_ann_vol": {
                "type": "float",
                "low": 0.15,  # Narrowed from 0.20 (roadmap)
                "high": 0.40,  # Narrowed from 0.50 (roadmap)
            },
            # NEW: Volatility lookback (roadmap)
            "strategy.vol_lookback": {
                "type": "int",
                "low": 48,
                "high": 144,
            },
            # NEW: Carry budget fraction (roadmap)
            "strategy.carry.budget_frac": {
                "type": "float",
                "low": 0.0,
                "high": 0.40,
            },
        }
    
    return ParameterSpace(space=core_params)

