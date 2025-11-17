"""
Bayesian optimization runner using Optuna.

Provides TPE-based optimization over parameter space.
"""
from __future__ import annotations

import logging
from typing import Dict, Any, List, Optional, Callable, Tuple
from dataclasses import dataclass
import json

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
    """Bayesian optimizer using Optuna TPE sampler."""
    
    def __init__(
        self,
        param_space: ParameterSpace,
        objective_fn: Callable[[Dict[str, Any]], float],
        n_trials: int = 100,
        n_startup_trials: int = 10,
        seed: Optional[int] = None,
    ):
        """
        Initialize Bayesian optimizer.
        
        Args:
            param_space: Parameter space definition
            objective_fn: Function that takes params dict -> float score
            n_trials: Total number of trials
            n_startup_trials: Random trials before BO starts
            seed: Random seed
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
        
        self.study = optuna.create_study(
            direction="maximize",
            sampler=sampler,
        )
        
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
            
            # Evaluate objective
            try:
                score = self.objective_fn(params)
                
                # Store trial result
                self.trial_results.append({
                    "trial_number": trial.number,
                    "params": params,
                    "score": score,
                })
                
                return score
            except Exception as e:
                log.warning(f"Trial {trial.number} failed: {e}")
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
    
    **MAKE MONEY HARDENING**: Reduced to 15 core parameters to minimize overfitting risk.
    All other parameters are frozen to fixed values based on economic rationale.
    
    Core parameters (15 total):
    - Signal generation: signal_power, lookbacks (3), entry threshold
    - Position selection: k_min, k_max
    - Filters: regime filter (2 params)
    - Risk: atr_mult_sl, trail_atr_mult, gross_leverage, max_weight_per_asset
    - Sizing: portfolio_vol_target
    
    Args:
        core_params: Optional custom parameter definitions
    
    Returns:
        ParameterSpace object
    """
    if core_params is None:
        # Default core parameters (MAKE MONEY: reduced from ~130+ to 15 core params)
        # Based on PARAMETER_REVIEW.md and MAKE_MONEY_ASSESSMENT.md recommendations
        core_params = {
            # Signals
            "strategy.signal_power": {
                "type": "float",
                "low": 1.0,
                "high": 2.0,
            },
            "strategy.lookbacks[0]": {  # Short lookback (hours)
                "type": "int",
                "low": 6,
                "high": 24,
            },
            "strategy.lookbacks[1]": {  # Medium lookback (hours)
                "type": "int",
                "low": 12,
                "high": 48,
            },
            "strategy.lookbacks[2]": {  # Long lookback (hours)
                "type": "int",
                "low": 24,
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
            # Filters
            "strategy.regime_filter.ema_len": {
                "type": "int",
                "low": 100,
                "high": 300,
            },
            "strategy.regime_filter.slope_min_bps_per_day": {
                "type": "float",
                "low": 1.0,
                "high": 5.0,
            },
            "strategy.entry_zscore_min": {
                "type": "float",
                "low": 0.0,
                "high": 1.0,
            },
            # Risk (tight ranges)
            "risk.atr_mult_sl": {
                "type": "float",
                "low": 1.5,
                "high": 3.0,
            },
            "risk.trail_atr_mult": {
                "type": "float",
                "low": 0.5,
                "high": 1.5,
            },
            "strategy.gross_leverage": {
                "type": "float",
                "low": 1.0,
                "high": 2.0,
            },
            "strategy.max_weight_per_asset": {
                "type": "float",
                "low": 0.10,
                "high": 0.30,
            },
            # Sizing
            "strategy.portfolio_vol_target.target_ann_vol": {
                "type": "float",
                "low": 0.20,
                "high": 0.50,
            },
        }
    
    return ParameterSpace(space=core_params)

