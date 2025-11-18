"""
Full-cycle automated optimizer.

Orchestrates WFO + Bayesian Optimization + Monte Carlo stress testing.
"""
from __future__ import annotations

import logging
import argparse
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, timezone
import json

from ..config import load_config, AppConfig
from .backtest_runner import (
    run_backtest_with_params,
    fetch_historical_data,
)
from .walk_forward import (
    generate_wfo_segments,
    evaluate_on_segments,
    aggregate_segment_results,
    WFOConfig,
)
from .bo_runner import (
    BayesianOptimizer,
    define_parameter_space,
    ParameterSpace,
)
from .monte_carlo import (
    run_monte_carlo_stress_test,
    compute_tail_risk_penalty,
    MCConfig,
)
from .config_manager import ConfigManager

log = logging.getLogger("optimizer.full_cycle")


def compute_objective_score(
    metrics: Dict[str, float],
    weights: Optional[Dict[str, float]] = None,
) -> float:
    """
    Compute composite objective score from metrics.
    
    Args:
        metrics: Dict of performance metrics
        weights: Optional weights dict (defaults to reasonable values)
    
    Returns:
        Composite score (higher is better)
    """
    if weights is None:
        weights = {
            "sharpe": 0.40,
            "annualized": 0.25,
            "calmar": 0.20,
            "total_return": 0.10,
            "turnover": -0.05,  # Penalty
        }
    
    score = 0.0
    
    sharpe = metrics.get("sharpe", 0.0)
    annualized = metrics.get("annualized", 0.0)
    calmar = metrics.get("calmar", 0.0)
    total_return = metrics.get("total_return", 0.0)
    turnover = metrics.get("gross_turnover_per_year", 0.0)
    
    # Normalize metrics
    score += weights.get("sharpe", 0.0) * sharpe
    score += weights.get("annualized", 0.0) * annualized / 10.0  # Scale to ~1.0
    score += weights.get("calmar", 0.0) * calmar
    score += weights.get("total_return", 0.0) * total_return
    score += weights.get("turnover", 0.0) * turnover / 1000.0  # Penalty for high turnover
    
    return score


def make_eval_fn(
    base_cfg: AppConfig,
    param_space: ParameterSpace,
) -> callable:
    """
    Create evaluation function for Bayesian optimization.
    
    Args:
        base_cfg: Base config
        param_space: Parameter space definition
    
    Returns:
        Function that takes params dict -> score float
    """
    def eval_fn(params: Dict[str, Any]) -> float:
        try:
            stats = run_backtest_with_params(
                base_cfg=base_cfg,
                param_overrides=params,
                return_curve=False,
            )
            
            if not stats:
                return float("-inf")
            
            # Check minimum trade count
            # (Assuming we can infer from stats, or skip this check)
            
            score = compute_objective_score(stats)
            return score
        except Exception as e:
            log.warning(f"Eval failed for params {params}: {e}")
            return float("-inf")
    
    return eval_fn


def run_wfo_bo_segment(
    segment,
    base_cfg: AppConfig,
    param_space: ParameterSpace,
    bo_config: Dict[str, Any],
) -> Tuple[Dict[str, Any], float, List[Dict[str, Any]]]:
    """
    Run Bayesian optimization on a WFO training segment.
    
    Args:
        segment: WFOSegment object
        base_cfg: Base config
        param_space: Parameter space definition
        bo_config: BO configuration (n_trials, etc.)
    
    Returns:
        Tuple of (best_params, best_score, trial_history)
    """
    log.info(
        f"Running BO on segment {segment.segment_id} "
        f"({segment.train_start.date()} to {segment.train_end.date()})"
    )
    
    # Create eval function that uses training data
    def eval_fn(params: Dict[str, Any]) -> float:
        try:
            stats = run_backtest_with_params(
                base_cfg=base_cfg,
                param_overrides=params,
                symbols=segment.symbols,
                prefetch_bars=segment.train_bars,
                return_curve=False,
            )
            
            if not stats:
                return float("-inf")
            
            score = compute_objective_score(stats)
            return score
        except Exception as e:
            log.warning(f"Eval failed: {e}")
            return float("-inf")
    
    # Run Bayesian optimization
    optimizer = BayesianOptimizer(
        param_space=param_space,
        objective_fn=eval_fn,
        n_trials=bo_config.get("n_trials", 100),
        n_startup_trials=bo_config.get("n_startup_trials", 10),
        seed=bo_config.get("seed"),
    )
    
    best_params, best_score = optimizer.optimize()
    trial_history = optimizer.get_trial_history()
    
    log.info(
        f"Segment {segment.segment_id} BO complete: "
        f"best_score={best_score:.4f}"
    )
    
    return best_params, best_score, trial_history


def run_full_cycle(
    base_config_path: str,
    live_config_path: str,
    symbol_universe: Optional[List[str]] = None,
    train_days: int = 120,
    oos_days: int = 30,
    embargo_days: int = 2,
    bo_n_trials: int = 100,
    bo_n_startup: int = 10,
    mc_n_runs: int = 1000,
    min_improve_sharpe: float = 0.05,
    min_improve_annualized: float = 0.03,
    max_dd_increase: float = 0.05,
    tail_dd_limit: float = 0.70,
    deploy: bool = False,
    seed: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Run full optimization cycle: WFO + BO + MC + deployment.
    
    Args:
        base_config_path: Path to base config
        live_config_path: Path to live config (for comparison)
        symbol_universe: Optional symbol list (if None, fetched from exchange)
        train_days: Training window size (days)
        oos_days: Out-of-sample window size (days)
        embargo_days: Embargo between train and OOS (days)
        bo_n_trials: Number of BO trials per segment
        bo_n_startup: Random trials before BO
        mc_n_runs: Number of MC runs
        min_improve_sharpe: Minimum Sharpe improvement to deploy
        min_improve_annualized: Minimum annualized return improvement
        max_dd_increase: Maximum allowed drawdown increase
        tail_dd_limit: Catastrophic DD threshold (reject if exceeded)
        deploy: If True, deploy new config if it passes checks
        seed: Random seed
    
    Returns:
        Dict with optimization results
    """
    log.info("=== FULL CYCLE OPTIMIZER ===")
    
    run_start_time = datetime.now(timezone.utc)
    
    # Load configs
    base_cfg = load_config(base_config_path)
    live_cfg = load_config(live_config_path) if live_config_path != base_config_path else base_cfg
    
    # Initialize config manager
    config_manager = ConfigManager(
        live_config_path=live_config_path,
        optimized_dir="config/optimized",
    )
    
    # Define parameter space
    param_space = define_parameter_space()
    
    # Fetch historical data
    log.info("Fetching historical data...")
    bars, symbols = fetch_historical_data(base_cfg, symbols=symbol_universe)
    
    if not bars:
        raise RuntimeError("No historical data available")
    
    if symbol_universe is None:
        symbol_universe = symbols
    
    # Check available data
    sample_symbol = list(bars.keys())[0] if bars else None
    available_bars = len(bars[sample_symbol]) if sample_symbol else 0
    log.info(f"Fetched data for {len(bars)} symbols, {available_bars} bars per symbol")
    
    # Calculate required bars for WFO (before creating WFOConfig)
    timeframe_hours = 1.0  # Assuming 1h bars (could be extracted from config)
    
    # Use adaptive minimums based on available data
    # If we have limited data, reduce minimums; if we have plenty, use standard minimums
    effective_min_train_days = min(60, max(30, int(available_bars * 0.4 / 24)))  # At least 30 days, up to 60
    effective_min_oos_days = min(7, max(3, int(available_bars * 0.1 / 24)))  # At least 3 days, up to 7
    
    min_train_bars = int(effective_min_train_days * 24 / timeframe_hours)
    min_oos_bars = int(effective_min_oos_days * 24 / timeframe_hours)
    embargo_bars_required = int(embargo_days * 24 / timeframe_hours)
    required_bars = min_train_bars + embargo_bars_required + min_oos_bars
    
    log.info(
        f"Adaptive WFO minimums: train={effective_min_train_days}d, oos={effective_min_oos_days}d "
        f"(available: {available_bars} bars = {available_bars * timeframe_hours / 24:.1f} days)"
    )
    
    if available_bars < required_bars:
        available_days = available_bars * timeframe_hours / 24
        required_days = required_bars * timeframe_hours / 24
        error_msg = (
            f"WFO requires at least {required_days:.1f} days of data "
            f"({required_bars} bars at {timeframe_hours}h), but only {available_days:.1f} days "
            f"({available_bars} bars) are available.\n\n"
            f"Solutions:\n"
            f"1. Increase candles_limit in config.yaml (current: {base_cfg.exchange.candles_limit})\n"
            f"   Set to at least {required_bars} (recommended: {required_bars + 200})\n"
            f"2. Use a simpler optimizer that doesn't require WFO:\n"
            f"   - python -m src.optimizer_runner (grid search)\n"
            f"   - python -m src.optimizer_cli (staged grid search)\n"
            f"   - python -m src.optimizer (legacy simple grid)\n"
            f"3. Reduce WFO requirements (train_days, oos_days) if you have less data"
        )
        raise RuntimeError(error_msg)
    
    # Generate WFO segments
    # Adjust OOS window size if optimizer config prefers larger windows and data is available
    opt_cfg = base_cfg.optimizer
    effective_oos_days = oos_days
    if opt_cfg.prefer_larger_oos_windows and available_bars > 0:
        # Calculate how much data we have beyond minimum requirements
        min_required_bars = int((train_days + embargo_days + oos_days) * 24 / timeframe_hours)
        extra_bars = available_bars - min_required_bars
        if extra_bars > 0:
            # Use extra data to increase OOS window (up to max_oos_days_when_available)
            extra_oos_bars = min(extra_bars, int((opt_cfg.max_oos_days_when_available - oos_days) * 24 / timeframe_hours))
            if extra_oos_bars > 0:
                effective_oos_days = oos_days + (extra_oos_bars * timeframe_hours / 24.0)
                log.info(
                    f"Using larger OOS window: {effective_oos_days:.1f} days "
                    f"(requested: {oos_days}, max: {opt_cfg.max_oos_days_when_available})"
                )
    
    # Calculate effective minimums based on available data for WFOConfig
    effective_min_train_days_cfg = min(train_days, max(30, int(available_bars * 0.4 / 24)))
    effective_min_oos_days_cfg = min(int(effective_oos_days), max(3, int(available_bars * 0.1 / 24)))
    
    wfo_cfg = WFOConfig(
        train_days=train_days,
        oos_days=int(effective_oos_days),  # Convert to int for WFOConfig
        embargo_days=embargo_days,
        min_train_days=effective_min_train_days_cfg,  # Adaptive minimum
        min_oos_days=effective_min_oos_days_cfg,  # Adaptive minimum
        timeframe_hours=timeframe_hours,
    )
    
    segments = generate_wfo_segments(bars, wfo_cfg)
    
    if not segments:
        raise RuntimeError("No WFO segments generated")
    
    log.info(f"Generated {len(segments)} WFO segments")
    
    # Run baseline on current config
    log.info("Evaluating baseline (current live config)...")
    baseline_segment_results = []
    for seg in segments:
        try:
            # Calculate OOS sample size
            oos_sample_symbol = list(seg.oos_bars.keys())[0] if seg.oos_bars else None
            oos_bars_count = len(seg.oos_bars[oos_sample_symbol]) if oos_sample_symbol else 0
            oos_days_approx = oos_bars_count * wfo_cfg.timeframe_hours / 24.0
            
            # Evaluate on train data
            train_stats = run_backtest_with_params(
                base_cfg=live_cfg,
                param_overrides={},
                symbols=seg.symbols,
                prefetch_bars=seg.train_bars,
                return_curve=False,
            )
            
            # Evaluate on OOS data
            oos_stats = run_backtest_with_params(
                base_cfg=live_cfg,
                param_overrides={},
                symbols=seg.symbols,
                prefetch_bars=seg.oos_bars,
                return_curve=False,
            )
            
            # Extract trade count if available
            oos_trades = oos_stats.get("trades", 0) if oos_stats else 0
            
            baseline_segment_results.append({
                "segment_id": seg.segment_id,
                "train_metrics": train_stats,  # Added train metrics
                "oos_metrics": oos_stats,
                "oos_sample_size": {
                    "bars": oos_bars_count,
                    "days": oos_days_approx,
                    "trades": oos_trades,
                },
            })
        except Exception as e:
            log.warning(f"Baseline eval failed for segment {seg.segment_id}: {e}")
    
    baseline_aggregated = aggregate_segment_results(
        baseline_segment_results,
        metric_keys=["sharpe", "annualized", "max_drawdown", "calmar"],
    )
    
    # Extract OOS sample size info from aggregated results
    baseline_oos_bars = baseline_aggregated.get("oos_sample_bars_mean", 0.0)
    baseline_oos_days = baseline_aggregated.get("oos_sample_days_mean", 0.0)
    baseline_oos_trades = baseline_aggregated.get("oos_sample_trades_mean", 0.0)
    
    # Check if baseline OOS is too small
    opt_cfg = base_cfg.optimizer
    baseline_oos_too_small = (
        baseline_oos_bars < opt_cfg.oos_min_bars_for_deploy or
        baseline_oos_days < opt_cfg.oos_min_days_for_deploy or
        (opt_cfg.oos_min_trades_for_deploy > 0 and baseline_oos_trades < opt_cfg.oos_min_trades_for_deploy)
    )
    
    if baseline_oos_too_small and opt_cfg.warn_on_small_oos:
        log.warning(
            f"⚠️  Baseline OOS sample is too small for reliable metrics: "
            f"{baseline_oos_bars:.0f} bars (~{baseline_oos_days:.1f} days, {baseline_oos_trades:.0f} trades). "
            f"Minimum required: {opt_cfg.oos_min_bars_for_deploy} bars, {opt_cfg.oos_min_days_for_deploy} days, "
            f"{opt_cfg.oos_min_trades_for_deploy} trades. "
            f"Baseline metrics may be unreliable."
        )
    
    log.info(
        f"Baseline OOS: "
        f"Sharpe={baseline_aggregated.get('oos_sharpe_mean', 0.0):.2f}, "
        f"Annualized={baseline_aggregated.get('oos_annualized_mean', 0.0):.2%}, "
        f"Sample: {baseline_oos_bars:.0f} bars (~{baseline_oos_days:.1f} days, {baseline_oos_trades:.0f} trades)"
    )
    
    # Run BO on each segment
    bo_config = {
        "n_trials": bo_n_trials,
        "n_startup_trials": bo_n_startup,
        "seed": seed,
    }
    
    segment_best_params: List[Dict[str, Any]] = []
    segment_scores: List[float] = []
    
    for seg in segments:
        try:
            best_params, best_score, trial_history = run_wfo_bo_segment(
                seg, base_cfg, param_space, bo_config
            )
            segment_best_params.append(best_params)
            segment_scores.append(best_score)
        except Exception as e:
            log.error(f"BO failed for segment {seg.segment_id}: {e}")
            continue
    
    if not segment_best_params:
        raise RuntimeError("No valid BO results from any segment")
    
    # Select top K parameter sets
    top_k = min(3, len(segment_best_params))
    sorted_indices = sorted(
        range(len(segment_scores)),
        key=lambda i: segment_scores[i],
        reverse=True,
    )
    top_params_sets = [segment_best_params[i] for i in sorted_indices[:top_k]]
    
    log.info(f"Selected top {top_k} parameter sets from BO")
    
    # Evaluate top candidates on all OOS segments
    log.info("Evaluating top candidates on OOS segments...")
    candidate_results = []
    
    for cand_idx, cand_params in enumerate(top_params_sets):
        cand_segment_results = []
        
        for seg in segments:
            try:
                stats = run_backtest_with_params(
                    base_cfg=base_cfg,
                    param_overrides=cand_params,
                    symbols=seg.symbols,
                    prefetch_bars=seg.oos_bars,
                    return_curve=True,
                )
                
                if stats and "equity_curve" in stats:
                    equity_curve = stats.pop("equity_curve")
                    
                    # Calculate OOS sample size
                    oos_sample_symbol = list(seg.oos_bars.keys())[0] if seg.oos_bars else None
                    oos_bars_count = len(seg.oos_bars[oos_sample_symbol]) if oos_sample_symbol else 0
                    oos_days_approx = oos_bars_count * wfo_cfg.timeframe_hours / 24.0
                    oos_trades = stats.get("trades", 0) if stats else 0
                    
                    # Run Monte Carlo stress test
                    mc_cfg = MCConfig(
                        n_runs=mc_n_runs,
                        seed=seed + cand_idx if seed is not None else None,
                    )
                    mc_summary = run_monte_carlo_stress_test(
                        equity_curve,
                        mc_cfg,
                        method="bootstrap",
                    )
                    
                    cand_segment_results.append({
                        "segment_id": seg.segment_id,
                        "oos_metrics": stats,
                        "mc_summary": mc_summary,
                        "oos_sample_size": {
                            "bars": oos_bars_count,
                            "days": oos_days_approx,
                            "trades": oos_trades,
                        },
                    })
            except Exception as e:
                log.warning(f"Candidate eval failed for segment {seg.segment_id}: {e}")
        
        if cand_segment_results:
            aggregated = aggregate_segment_results(
                cand_segment_results,
                metric_keys=["sharpe", "annualized", "max_drawdown", "calmar"],
            )
            
            # Aggregate MC results
            mc_summaries = [r["mc_summary"] for r in cand_segment_results if r.get("mc_summary")]
            if mc_summaries:
                mc_aggregated = {
                    "mean_p95_dd": float(
                        sum(s.get("p95_max_drawdown", 0.0) for s in mc_summaries)
                        / len(mc_summaries)
                    ),
                    "mean_p99_dd": float(
                        sum(s.get("p99_max_drawdown", 0.0) for s in mc_summaries)
                        / len(mc_summaries)
                    ),
                    "worst_dd": float(
                        min(s.get("worst_max_drawdown", 0.0) for s in mc_summaries)
                    ),
                }
                aggregated.update(mc_aggregated)
            
            # Extract OOS sample size info
            cand_oos_bars = aggregated.get("oos_sample_bars_mean", 0.0)
            cand_oos_days = aggregated.get("oos_sample_days_mean", 0.0)
            cand_oos_trades = aggregated.get("oos_sample_trades_mean", 0.0)
            
            candidate_results.append({
                "candidate_id": cand_idx,
                "params": cand_params,
                "aggregated_metrics": aggregated,
                "segment_results": cand_segment_results,
                "oos_sample_size": {
                    "bars": cand_oos_bars,
                    "days": cand_oos_days,
                    "trades": cand_oos_trades,
                },
            })
    
    if not candidate_results:
        raise RuntimeError("No valid candidate evaluations")
    
    # Select best candidate
    best_candidate = None
    best_score = float("-inf")
    
    opt_cfg = base_cfg.optimizer
    
    for cand in candidate_results:
        metrics = cand["aggregated_metrics"]
        sharpe = metrics.get("oos_sharpe_mean", 0.0)
        annualized = metrics.get("oos_annualized_mean", 0.0)
        max_dd = abs(metrics.get("oos_max_drawdown_mean", 0.0))
        p99_dd = abs(metrics.get("mean_p99_dd", 0.0))
        
        # Get candidate OOS sample size
        cand_oos_size = cand.get("oos_sample_size", {})
        cand_oos_bars = cand_oos_size.get("bars", 0.0)
        cand_oos_days = cand_oos_size.get("days", 0.0)
        cand_oos_trades = cand_oos_size.get("trades", 0.0)
        
        # Check if candidate OOS is too small
        cand_oos_too_small = (
            cand_oos_bars < opt_cfg.oos_min_bars_for_deploy or
            cand_oos_days < opt_cfg.oos_min_days_for_deploy or
            (opt_cfg.oos_min_trades_for_deploy > 0 and cand_oos_trades < opt_cfg.oos_min_trades_for_deploy)
        )
        
        if cand_oos_too_small and opt_cfg.warn_on_small_oos:
            log.warning(
                f"Candidate {cand['candidate_id']}: OOS sample too small "
                f"({cand_oos_bars:.0f} bars, ~{cand_oos_days:.1f} days, {cand_oos_trades:.0f} trades). "
                f"Metrics may be unreliable."
            )
        
        # If both baseline and candidate OOS are too small, skip deployment comparison
        if opt_cfg.require_min_oos_for_deploy and (baseline_oos_too_small or cand_oos_too_small):
            if baseline_oos_too_small and opt_cfg.ignore_baseline_if_oos_too_small:
                log.info(
                    f"Candidate {cand['candidate_id']}: Skipping baseline comparison "
                    f"(baseline OOS too small: {baseline_oos_bars:.0f} bars, ~{baseline_oos_days:.1f} days). "
                    f"Will evaluate candidate on absolute metrics only."
                )
                # Evaluate candidate on absolute metrics only (no baseline comparison)
                # Still check safety thresholds
                if p99_dd > tail_dd_limit:
                    log.warning(f"Candidate {cand['candidate_id']}: Rejected (tail DD {p99_dd:.2%} > {tail_dd_limit:.2%})")
                    continue
                
                # If candidate OOS is also too small, reject it
                if cand_oos_too_small:
                    log.info(
                        f"Candidate {cand['candidate_id']}: Rejected (OOS sample too small: "
                        f"{cand_oos_bars:.0f} bars, ~{cand_oos_days:.1f} days). "
                        f"Require minimum {opt_cfg.oos_min_bars_for_deploy} bars, {opt_cfg.oos_min_days_for_deploy} days."
                    )
                    continue
                
                # Candidate OOS is acceptable, evaluate on absolute metrics
                # Use reasonable absolute thresholds (e.g., Sharpe > 0.5, positive annualized)
                if sharpe < 0.5:
                    log.info(f"Candidate {cand['candidate_id']}: Rejected (Sharpe {sharpe:.2f} < 0.5)")
                    continue
                
                if annualized < 0.0:
                    log.info(f"Candidate {cand['candidate_id']}: Rejected (negative annualized return {annualized:.2%})")
                    continue
                
                # Candidate passes absolute checks
                score = sharpe * 0.5 + annualized * 10.0 * 0.3 + (metrics.get("calmar", 0.0) * 0.2)
                if score > best_score:
                    best_score = score
                    best_candidate = cand
                continue
            else:
                # Baseline too small but we're not ignoring it, or candidate too small
                log.info(
                    f"Candidate {cand['candidate_id']}: Rejected (OOS sample size requirements not met). "
                    f"Baseline: {baseline_oos_bars:.0f} bars, Candidate: {cand_oos_bars:.0f} bars."
                )
                continue
        
        # Safety checks
        if p99_dd > tail_dd_limit:
            log.warning(f"Candidate {cand['candidate_id']}: Rejected (tail DD {p99_dd:.2%} > {tail_dd_limit:.2%})")
            continue
        
        # Only compare to baseline if both have sufficient OOS sample size
        baseline_sharpe = baseline_aggregated.get("oos_sharpe_mean", 0.0)
        baseline_annualized = baseline_aggregated.get("oos_annualized_mean", 0.0)
        baseline_dd = abs(baseline_aggregated.get("oos_max_drawdown_mean", 0.0))
        
        # Improvement checks (only if baseline is reliable)
        sharpe_improve = sharpe - baseline_sharpe
        annualized_improve = annualized - baseline_annualized
        dd_increase = max_dd - baseline_dd
        
        if sharpe_improve < min_improve_sharpe:
            log.info(
                f"Candidate {cand['candidate_id']}: Sharpe improvement "
                f"{sharpe_improve:.4f} < {min_improve_sharpe:.4f} "
                f"(candidate: {sharpe:.2f}, baseline: {baseline_sharpe:.2f})"
            )
            continue
        
        if annualized_improve < min_improve_annualized:
            log.info(
                f"Candidate {cand['candidate_id']}: Annualized improvement "
                f"{annualized_improve:.2%} < {min_improve_annualized:.2%} "
                f"(candidate: {annualized:.2%}, baseline: {baseline_annualized:.2%})"
            )
            continue
        
        if dd_increase > max_dd_increase:
            log.info(
                f"Candidate {cand['candidate_id']}: DD increase "
                f"{dd_increase:.2%} > {max_dd_increase:.2%}"
            )
            continue
        
        # Score candidate
        calmar = metrics.get("calmar", 0.0)
        score = sharpe * 0.5 + annualized * 10.0 * 0.3 + calmar * 0.2
        
        if score > best_score:
            best_score = score
            best_candidate = cand
    
    # Prepare result
    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "baseline_metrics": baseline_aggregated,
        "candidates_evaluated": len(candidate_results),
        "best_candidate": None,
        "deployed": False,
    }
    
    if best_candidate:
        log.info(
            f"Best candidate: {best_candidate['candidate_id']}, "
            f"OOS Sharpe={best_candidate['aggregated_metrics'].get('oos_sharpe_mean', 0.0):.2f}"
        )
        
        # Build new config
        best_params = best_candidate["params"]
        new_config_dict = base_cfg.model_dump()
        
        # Apply parameter overrides
        for path, value in best_params.items():
            _deep_set(new_config_dict, path, value)
        
        # Save versioned config
        metadata = {
            "baseline_metrics": baseline_aggregated,
            "candidate_metrics": best_candidate["aggregated_metrics"],
            "params": best_params,
            "wfo_segments": len(segments),
            "bo_trials_per_segment": bo_n_trials,
            "mc_runs": mc_n_runs,
        }
        
        # Store metadata in result for notification
        result["wfo_segments"] = len(segments)
        result["bo_trials_per_segment"] = bo_n_trials
        result["mc_runs"] = mc_n_runs
        
        config_path = config_manager.save_versioned_config(
            new_config_dict,
            metadata,
        )
        
        result["best_candidate"] = {
            "params": best_params,
            "metrics": best_candidate["aggregated_metrics"],
            "config_path": str(config_path),
        }
        
        # Deploy if requested
        if deploy:
            if config_manager.validate_config(config_path):
                if config_manager.deploy_config(config_path, create_backup=True):
                    result["deployed"] = True
                    log.info("Deployed new config to live location")
                else:
                    log.error("Failed to deploy config")
            else:
                log.error("Config validation failed, not deploying")
    else:
        log.info("No candidate passed all checks; keeping existing config")
    
    # Cleanup old versions
    config_manager.cleanup_old_versions()
    
    # Add to rollout queue if candidate found (instead of direct deployment)
    if best_candidate and not deploy:
        try:
            from ..rollout.integration import add_optimizer_candidate
            candidate_id = add_optimizer_candidate(result, auto_start_staging=False)
            if candidate_id:
                log.info(f"Added candidate {candidate_id} to rollout queue (staging will be handled by supervisor)")
            else:
                log.warning("Failed to add candidate to rollout queue")
        except Exception as e:
            log.warning(f"Failed to add candidate to rollout queue: {e}")
            # Don't fail the optimizer if rollout integration fails
    
    # Send Discord notification
    try:
        from ..notifications.optimizer_notifications import send_optimizer_notification
        send_optimizer_notification(result, live_cfg, run_start_time)
    except Exception as e:
        log.warning(f"Failed to send Discord notification: {e}")
        # Don't fail the optimizer if notification fails
    
    return result


def _deep_set(d: Dict[str, Any], path: str, value: Any) -> None:
    """Set a nested dict value using dot notation."""
    keys = path.split(".")
    cur = d
    for k in keys[:-1]:
        # Handle array indices like "lookbacks[0]"
        if "[" in k:
            k, idx_str = k.split("[")
            idx = int(idx_str.rstrip("]"))
            if k not in cur:
                cur[k] = []
            while len(cur[k]) <= idx:
                cur[k].append(0)
            cur = cur[k][idx]
        else:
            if k not in cur or not isinstance(cur[k], dict):
                cur[k] = {}
            cur = cur[k]
    
    final_key = keys[-1]
    if "[" in final_key:
        final_key, idx_str = final_key.split("[")
        idx = int(idx_str.rstrip("]"))
        if final_key not in cur:
            cur[final_key] = []
        while len(cur[final_key]) <= idx:
            cur[final_key].append(0)
        cur[final_key][idx] = value
    else:
        cur[final_key] = value


def main():
    """CLI entrypoint for full-cycle optimizer."""
    parser = argparse.ArgumentParser(
        description="Full-cycle automated optimizer (WFO + BO + MC)"
    )
    parser.add_argument(
        "--base-config",
        default="config/config.yaml",
        help="Path to base config",
    )
    parser.add_argument(
        "--live-config",
        default="config/config.yaml",
        help="Path to live config (for comparison)",
    )
    parser.add_argument(
        "--symbol-universe",
        default=None,
        help="Comma-separated symbol list (if None, fetched from exchange)",
    )
    parser.add_argument(
        "--train-days",
        type=int,
        default=120,
        help="Training window size (days)",
    )
    parser.add_argument(
        "--oos-days",
        type=int,
        default=30,
        help="Out-of-sample window size (days)",
    )
    parser.add_argument(
        "--embargo-days",
        type=int,
        default=2,
        help="Embargo between train and OOS (days)",
    )
    parser.add_argument(
        "--bo-evals",
        type=int,
        default=100,
        help="Number of BO trials per segment",
    )
    parser.add_argument(
        "--bo-startup",
        type=int,
        default=10,
        help="Random trials before BO",
    )
    parser.add_argument(
        "--mc-runs",
        type=int,
        default=1000,
        help="Number of MC runs",
    )
    parser.add_argument(
        "--min-improve-sharpe",
        type=float,
        default=0.05,
        help="Minimum Sharpe improvement to deploy",
    )
    parser.add_argument(
        "--min-improve-ann",
        type=float,
        default=0.03,
        help="Minimum annualized return improvement",
    )
    parser.add_argument(
        "--max-dd-increase",
        type=float,
        default=0.05,
        help="Maximum allowed drawdown increase",
    )
    parser.add_argument(
        "--tail-dd-limit",
        type=float,
        default=0.70,
        help="Catastrophic DD threshold (reject if exceeded)",
    )
    parser.add_argument(
        "--deploy",
        action="store_true",
        help="Deploy new config if it passes checks",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Random seed",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output JSON file path (default: stdout)",
    )
    
    args = parser.parse_args()
    
    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    
    # Parse symbol universe
    symbol_universe = None
    if args.symbol_universe:
        symbol_universe = [s.strip() for s in args.symbol_universe.split(",") if s.strip()]
    
    try:
        result = run_full_cycle(
            base_config_path=args.base_config,
            live_config_path=args.live_config,
            symbol_universe=symbol_universe,
            train_days=args.train_days,
            oos_days=args.oos_days,
            embargo_days=args.embargo_days,
            bo_n_trials=args.bo_evals,
            bo_n_startup=args.bo_startup,
            mc_n_runs=args.mc_runs,
            min_improve_sharpe=args.min_improve_sharpe,
            min_improve_annualized=args.min_improve_ann,
            max_dd_increase=args.max_dd_increase,
            tail_dd_limit=args.tail_dd_limit,
            deploy=args.deploy,
            seed=args.seed,
        )
        
        # Output results
        output_json = json.dumps(result, indent=2)
        
        if args.output:
            with open(args.output, "w") as f:
                f.write(output_json)
            log.info(f"Results saved to {args.output}")
        else:
            print(output_json)
        
        # Exit code: 0 if successful (even if no deploy), non-zero on failure
        return 0
    except Exception as e:
        log.error(f"Full cycle optimizer failed: {e}", exc_info=True)
        return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())

