"""
Discord notifications for optimizer results.
"""
from __future__ import annotations

import logging
from typing import Dict, Any, List, Optional
from datetime import datetime

from .discord_notifier import DiscordNotifier
from ..config import AppConfig

log = logging.getLogger("notifications.optimizer")


def format_optimizer_result_embed(
    result: Dict[str, Any],
    cfg: AppConfig,
    run_start_time: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Format optimizer result as Discord embed.
    
    Args:
        result: Optimization result dict from run_full_cycle
        cfg: Config object (for notification settings)
        run_start_time: Optional run start time
    
    Returns:
        Dict with title, description, fields, color for Discord embed
    """
    deployed = result.get("deployed", False)
    best_candidate = result.get("best_candidate")
    baseline_metrics = result.get("baseline_metrics", {})
    
    # Determine status and color
    if best_candidate and deployed:
        status_emoji = "✅"
        status_text = "Deployed New Config"
        color = DiscordNotifier.COLOR_GREEN
    elif best_candidate:
        status_emoji = "⚠️"
        status_text = "No Deployment (Did Not Beat Baseline Safely)"
        color = DiscordNotifier.COLOR_ORANGE
    else:
        status_emoji = "❌"
        status_text = "No Valid Candidate"
        color = DiscordNotifier.COLOR_RED
    
    title = f"XSMOM Optimizer Run – {status_emoji} {status_text}"
    
    # Build description
    description_parts = []
    if run_start_time:
        duration = (datetime.utcnow() - run_start_time).total_seconds() / 3600
        description_parts.append(f"Duration: {duration:.1f}h")
    
    timestamp_str = result.get("timestamp", "")
    if timestamp_str:
        try:
            dt = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
            description_parts.append(f"Completed: {dt.strftime('%Y-%m-%d %H:%M UTC')}")
        except Exception:
            pass
    
    description = " | ".join(description_parts) if description_parts else ""
    
    # Build fields
    fields: List[Dict[str, Any]] = []
    
    # Run info
    run_info = []
    segments = result.get("wfo_segments", 0)
    bo_trials = result.get("bo_trials_per_segment", 0)
    mc_runs = result.get("mc_runs", 0)
    
    if segments:
        run_info.append(f"WFO segments: {segments}")
    if bo_trials:
        run_info.append(f"BO trials/segment: {bo_trials}")
    if mc_runs:
        run_info.append(f"MC runs: {mc_runs}")
    
    candidates_eval = result.get("candidates_evaluated", 0)
    if candidates_eval:
        run_info.append(f"Candidates: {candidates_eval}")
    
    if run_info:
        fields.append({
            "name": "Run Info",
            "value": "\n".join(run_info),
            "inline": False,
        })
    
    # Baseline metrics
    baseline_fields = []
    baseline_sharpe = baseline_metrics.get("oos_sharpe_mean", 0.0)
    baseline_ann = baseline_metrics.get("oos_annualized_mean", 0.0)
    baseline_dd = baseline_metrics.get("oos_max_drawdown_mean", 0.0)
    baseline_calmar = baseline_metrics.get("oos_calmar_mean", 0.0)
    
    if baseline_sharpe is not None:
        baseline_fields.append(f"Sharpe: {baseline_sharpe:.2f}")
    if baseline_ann is not None:
        baseline_fields.append(f"CAGR: {baseline_ann:.2%}")
    if baseline_dd is not None:
        baseline_fields.append(f"Max DD: {baseline_dd:.2%}")
    if baseline_calmar is not None:
        baseline_fields.append(f"Calmar: {baseline_calmar:.2f}")
    
    if baseline_fields:
        fields.append({
            "name": "Baseline (Live)",
            "value": "\n".join(baseline_fields),
            "inline": True,
        })
    
    # Candidate metrics (if available)
    if best_candidate:
        cand_metrics = best_candidate.get("metrics", {})
        cand_fields = []
        
        cand_sharpe = cand_metrics.get("oos_sharpe_mean", 0.0)
        cand_ann = cand_metrics.get("oos_annualized_mean", 0.0)
        cand_dd = cand_metrics.get("oos_max_drawdown_mean", 0.0)
        cand_calmar = cand_metrics.get("oos_calmar_mean", 0.0)
        p99_dd = cand_metrics.get("mean_p99_dd", 0.0)
        
        if cand_sharpe is not None:
            sharpe_improve = cand_sharpe - baseline_sharpe
            cand_fields.append(f"Sharpe: {cand_sharpe:.2f} ({sharpe_improve:+.2f})")
        if cand_ann is not None:
            ann_improve = cand_ann - baseline_ann
            cand_fields.append(f"CAGR: {cand_ann:.2%} ({ann_improve:+.2%})")
        if cand_dd is not None:
            cand_fields.append(f"Max DD: {cand_dd:.2%}")
        if p99_dd is not None:
            cand_fields.append(f"MC 99% DD: {abs(p99_dd):.2%}")
        if cand_calmar is not None:
            cand_fields.append(f"Calmar: {cand_calmar:.2f}")
        
        if cand_fields:
            fields.append({
                "name": "Candidate",
                "value": "\n".join(cand_fields),
                "inline": True,
            })
        
        # Parameter highlights
        params = best_candidate.get("params", {})
        if params:
            param_highlights = []
            
            # Show most important/changed parameters
            key_params = [
                "strategy.signal_power",
                "strategy.gross_leverage",
                "strategy.k_min",
                "strategy.k_max",
                "strategy.max_weight_per_asset",
                "risk.atr_mult_sl",
                "risk.trail_atr_mult",
                "strategy.portfolio_vol_target.target_ann_vol",
            ]
            
            for key in key_params:
                if key in params:
                    value = params[key]
                    # Format nicely
                    if isinstance(value, float):
                        if key.endswith("_vol") or "target" in key:
                            param_highlights.append(f"{key.split('.')[-1]}: {value:.2%}")
                        else:
                            param_highlights.append(f"{key.split('.')[-1]}: {value:.2f}")
                    else:
                        param_highlights.append(f"{key.split('.')[-1]}: {value}")
            
            if param_highlights:
                fields.append({
                    "name": "Parameter Highlights",
                    "value": "\n".join(param_highlights[:8]),  # Limit to 8 params
                    "inline": False,
                })
    
    # Decision
    decision_text = []
    if deployed and best_candidate:
        config_path = best_candidate.get("config_path", "")
        if config_path:
            # Extract timestamp from path
            import re
            match = re.search(r"config_(\d{8}_\d{6})", config_path)
            if match:
                ts_str = match.group(1)
                decision_text.append(f"✅ Deployed: `config_{ts_str}.yaml`")
            else:
                decision_text.append(f"✅ Deployed: `{config_path}`")
    elif best_candidate:
        decision_text.append("⚠️ No deployment (did not beat baseline safely)")
    else:
        decision_text.append("❌ No valid candidate found")
    
    if decision_text:
        fields.append({
            "name": "Decision",
            "value": "\n".join(decision_text),
            "inline": False,
        })
    
    return {
        "title": title,
        "description": description,
        "fields": fields,
        "color": color,
        "timestamp": timestamp_str,
    }


def send_optimizer_notification(
    result: Dict[str, Any],
    cfg: AppConfig,
    run_start_time: Optional[datetime] = None,
) -> bool:
    """
    Send Discord notification for optimizer results.
    
    Args:
        result: Optimization result dict from run_full_cycle
        cfg: Config object (for notification settings)
        run_start_time: Optional run start time
    
    Returns:
        True if sent successfully, False otherwise
    """
    if not cfg.notifications.discord.enabled:
        log.debug("Discord notifications disabled")
        return False
    
    if not cfg.notifications.discord.send_optimizer_results:
        log.debug("Optimizer results notifications disabled")
        return False
    
    try:
        notifier = DiscordNotifier(
            enabled=cfg.notifications.discord.enabled,
        )
        
        # Set webhook from config if env var not set
        if not notifier.webhook_url:
            notifier.set_webhook_from_config(
                cfg.notifications.discord.webhook_url
            )
        
        if not notifier.webhook_url:
            log.warning("Discord webhook URL not available, skipping notification")
            return False
        
        # Format embed
        embed_data = format_optimizer_result_embed(result, cfg, run_start_time)
        
        # Send embed
        success = notifier.send_embed(
            title=embed_data["title"],
            description=embed_data.get("description", ""),
            fields=embed_data.get("fields", []),
            color=embed_data.get("color"),
            timestamp=embed_data.get("timestamp"),
        )
        
        if success:
            log.info("Discord optimizer notification sent successfully")
        else:
            log.warning("Failed to send Discord optimizer notification")
        
        return success
    except Exception as e:
        log.error(f"Failed to send Discord optimizer notification: {e}", exc_info=True)
        return False

