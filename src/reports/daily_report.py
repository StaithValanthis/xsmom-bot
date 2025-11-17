"""
Daily performance report generator.

Aggregates daily PnL, equity, trades, and sends Discord notification.
"""
from __future__ import annotations

import argparse
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Any, Optional, List
import pandas as pd

from ..config import load_config, AppConfig
from ..exchange import ExchangeWrapper
from ..utils import read_json, utcnow
from ..notifications.discord_notifier import DiscordNotifier

log = logging.getLogger("reports.daily")


def compute_daily_metrics(
    state: Dict[str, Any],
    current_equity: float,
    day_start_equity: float,
    day_high_equity: float,
    sym_stats: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Compute daily performance metrics.
    
    Args:
        state: State dict from state file
        current_equity: Current equity (USDT)
        day_start_equity: Equity at start of day (USDT)
        day_high_equity: Highest equity during day (USDT)
        sym_stats: Optional per-symbol stats dict
    
    Returns:
        Dict of daily metrics
    """
    metrics: Dict[str, Any] = {}
    
    # Daily PnL (absolute and %)
    daily_pnl_usdt = current_equity - day_start_equity if day_start_equity > 0 else 0.0
    daily_pnl_pct = (daily_pnl_usdt / day_start_equity) if day_start_equity > 0 else 0.0
    
    metrics["daily_pnl_usdt"] = daily_pnl_usdt
    metrics["daily_pnl_pct"] = daily_pnl_pct
    metrics["day_start_equity"] = day_start_equity
    metrics["current_equity"] = current_equity
    metrics["day_high_equity"] = day_high_equity
    
    # Intraday drawdown
    intraday_dd = (current_equity / day_high_equity - 1.0) if day_high_equity > 0 else 0.0
    metrics["intraday_dd"] = intraday_dd
    
    # Trade stats (from sym_stats)
    if sym_stats:
        total_trades = 0
        wins = 0
        losses = 0
        total_pnl = 0.0
        largest_win = 0.0
        largest_loss = 0.0
        
        for sym, stats in sym_stats.items():
            n = stats.get("n", 0)
            sym_wins = stats.get("wins", 0)
            sym_losses = stats.get("losses", 0)
            sym_pnl = stats.get("ema_pnl", 0.0) or 0.0
            
            total_trades += n
            wins += sym_wins
            losses += sym_losses
            total_pnl += sym_pnl
            
            # Track largest win/loss (rough estimate from EMA)
            if sym_pnl > 0:
                largest_win = max(largest_win, sym_pnl)
            else:
                largest_loss = min(largest_loss, sym_pnl)
        
        metrics["total_trades"] = total_trades
        metrics["wins"] = wins
        metrics["losses"] = losses
        win_rate = (wins / total_trades * 100.0) if total_trades > 0 else 0.0
        metrics["win_rate_pct"] = win_rate
        metrics["largest_win_usdt"] = largest_win
        metrics["largest_loss_usdt"] = abs(largest_loss)
    else:
        metrics["total_trades"] = 0
        metrics["win_rate_pct"] = 0.0
    
    return metrics


def compute_cumulative_metrics(
    state: Dict[str, Any],
    current_equity: float,
) -> Dict[str, Any]:
    """
    Compute cumulative performance metrics.
    
    Args:
        state: State dict from state file
        current_equity: Current equity (USDT)
    
    Returns:
        Dict of cumulative metrics
    """
    metrics: Dict[str, Any] = {}
    
    # Starting equity (first time equity was recorded, or current if not set)
    start_equity = state.get("start_equity") or current_equity
    if not state.get("start_equity") and current_equity > 0:
        # Set starting equity if not recorded
        start_equity = current_equity
        state["start_equity"] = start_equity
    
    metrics["start_equity"] = start_equity
    metrics["current_equity"] = current_equity
    
    # Total PnL
    total_pnl_usdt = current_equity - start_equity
    total_pnl_pct = (total_pnl_usdt / start_equity) if start_equity > 0 else 0.0
    
    metrics["total_pnl_usdt"] = total_pnl_usdt
    metrics["total_pnl_pct"] = total_pnl_pct
    
    # Max drawdown (tracked in state or computed from equity history)
    max_dd = state.get("max_drawdown_pct", 0.0)
    peak_equity = state.get("peak_equity", start_equity)
    
    if current_equity > peak_equity:
        peak_equity = current_equity
        state["peak_equity"] = peak_equity
    
    current_dd = (current_equity / peak_equity - 1.0) if peak_equity > 0 else 0.0
    if current_dd < max_dd:
        max_dd = current_dd
        state["max_drawdown_pct"] = max_dd
    
    metrics["max_drawdown_pct"] = max_dd
    metrics["peak_equity"] = peak_equity
    
    return metrics


def format_daily_report_embed(
    daily_metrics: Dict[str, Any],
    cumulative_metrics: Dict[str, Any],
    report_date: datetime,
) -> Dict[str, Any]:
    """
    Format daily report as Discord embed.
    
    Args:
        daily_metrics: Daily metrics dict
        cumulative_metrics: Cumulative metrics dict
        report_date: Report date (UTC)
    
    Returns:
        Dict with title, description, fields, color for Discord embed
    """
    date_str = report_date.strftime("%Y-%m-%d")
    title = f"XSMOM Daily Report – {date_str}"
    
    # Build description
    daily_pnl_pct = daily_metrics.get("daily_pnl_pct", 0.0)
    trades = daily_metrics.get("total_trades", 0)
    win_rate = daily_metrics.get("win_rate_pct", 0.0)
    
    description = f"Today: {daily_pnl_pct:+.2%} | Trades: {trades} | Win rate: {win_rate:.1f}%"
    
    # Determine color
    if daily_pnl_pct > 0.02:  # > 2%
        color = DiscordNotifier.COLOR_GREEN
    elif daily_pnl_pct > 0:  # Positive
        color = DiscordNotifier.COLOR_BLUE
    elif daily_pnl_pct > -0.02:  # > -2%
        color = DiscordNotifier.COLOR_ORANGE
    else:  # < -2%
        color = DiscordNotifier.COLOR_RED
    
    # Build fields
    fields: List[Dict[str, Any]] = []
    
    # Today's metrics
    today_fields = []
    daily_pnl_usdt = daily_metrics.get("daily_pnl_usdt", 0.0)
    today_fields.append(f"PnL: ${daily_pnl_usdt:+.2f} ({daily_pnl_pct:+.2%})")
    
    if trades > 0:
        today_fields.append(f"Trades: {trades}")
        today_fields.append(f"Win rate: {win_rate:.1f}%")
        
        largest_win = daily_metrics.get("largest_win_usdt", 0.0)
        largest_loss = daily_metrics.get("largest_loss_usdt", 0.0)
        if largest_win > 0:
            today_fields.append(f"Largest win: ${largest_win:.2f}")
        if largest_loss > 0:
            today_fields.append(f"Largest loss: ${-largest_loss:.2f}")
    
    intraday_dd = daily_metrics.get("intraday_dd", 0.0)
    if intraday_dd < 0:
        today_fields.append(f"Max intraday DD: {intraday_dd:.2%}")
    
    if today_fields:
        fields.append({
            "name": "Today",
            "value": "\n".join(today_fields),
            "inline": True,
        })
    
    # Cumulative metrics
    cumul_fields = []
    total_pnl_usdt = cumulative_metrics.get("total_pnl_usdt", 0.0)
    total_pnl_pct = cumulative_metrics.get("total_pnl_pct", 0.0)
    cumul_fields.append(f"Total PnL: ${total_pnl_usdt:+.2f} ({total_pnl_pct:+.2%})")
    
    current_equity = cumulative_metrics.get("current_equity", 0.0)
    start_equity = cumulative_metrics.get("start_equity", 0.0)
    if start_equity > 0:
        cumul_fields.append(f"Equity: ${current_equity:.2f} (start: ${start_equity:.2f})")
    
    max_dd = cumulative_metrics.get("max_drawdown_pct", 0.0)
    if max_dd < 0:
        cumul_fields.append(f"Max DD: {max_dd:.2%}")
    
    if cumul_fields:
        fields.append({
            "name": "Since Start",
            "value": "\n".join(cumul_fields),
            "inline": True,
        })
    
    # Equity info
    equity_fields = []
    day_start_equity = daily_metrics.get("day_start_equity", 0.0)
    if day_start_equity > 0:
        equity_fields.append(f"Day start: ${day_start_equity:.2f}")
    if current_equity > 0:
        equity_fields.append(f"Current: ${current_equity:.2f}")
    day_high_equity = daily_metrics.get("day_high_equity", 0.0)
    if day_high_equity > 0:
        equity_fields.append(f"Day high: ${day_high_equity:.2f}")
    
    if equity_fields:
        fields.append({
            "name": "Equity",
            "value": "\n".join(equity_fields),
            "inline": False,
        })
    
    return {
        "title": title,
        "description": description,
        "fields": fields,
        "color": color,
        "timestamp": report_date.isoformat(),
    }


def generate_daily_report(
    cfg: AppConfig,
    report_date: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Generate daily performance report.
    
    Args:
        cfg: Config object
        report_date: Optional report date (defaults to today UTC)
    
    Returns:
        Dict with daily and cumulative metrics
    """
    if report_date is None:
        report_date = utcnow()
    
    # Read state file
    state = read_json(cfg.paths.state_path, default={}) or {}
    
    # Get current equity from exchange
    ex = ExchangeWrapper(cfg.exchange)
    try:
        current_equity = ex.get_equity_usdt()
    except Exception as e:
        log.warning(f"Failed to fetch equity: {e}")
        current_equity = state.get("day_start_equity", 0.0) or 0.0
    finally:
        ex.close()
    
    # Get daily equity tracking from state
    day_start_equity = float(state.get("day_start_equity", 0.0) or 0.0)
    day_high_equity = float(state.get("day_high_equity", current_equity) or current_equity)
    
    # Get symbol stats
    sym_stats = state.get("sym_stats", {})
    
    # Compute metrics
    daily_metrics = compute_daily_metrics(
        state=state,
        current_equity=current_equity,
        day_start_equity=day_start_equity,
        day_high_equity=day_high_equity,
        sym_stats=sym_stats,
    )
    
    cumulative_metrics = compute_cumulative_metrics(
        state=state,
        current_equity=current_equity,
    )
    
    # Update state with peak equity / max DD
    if "peak_equity" not in state or current_equity > state.get("peak_equity", 0.0):
        state["peak_equity"] = current_equity
    if "max_drawdown_pct" not in state or cumulative_metrics["max_drawdown_pct"] < state.get("max_drawdown_pct", 0.0):
        state["max_drawdown_pct"] = cumulative_metrics["max_drawdown_pct"]
    
    # Save updated state (if changed)
    from ..utils import write_json
    write_json(cfg.paths.state_path, state)
    
    return {
        "report_date": report_date.isoformat(),
        "daily_metrics": daily_metrics,
        "cumulative_metrics": cumulative_metrics,
    }


def send_daily_report_notification(
    report: Dict[str, Any],
    cfg: AppConfig,
) -> bool:
    """
    Send Discord notification for daily report.
    
    Args:
        report: Report dict from generate_daily_report
        cfg: Config object
    
    Returns:
        True if sent successfully, False otherwise
    """
    if not cfg.notifications.discord.enabled:
        log.debug("Discord notifications disabled")
        return False
    
    if not cfg.notifications.discord.send_daily_report:
        log.debug("Daily report notifications disabled")
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
        
        # Parse report date
        report_date_str = report.get("report_date", "")
        try:
            report_date = datetime.fromisoformat(report_date_str.replace("Z", "+00:00"))
        except Exception:
            report_date = utcnow()
        
        # Format embed
        embed_data = format_daily_report_embed(
            daily_metrics=report.get("daily_metrics", {}),
            cumulative_metrics=report.get("cumulative_metrics", {}),
            report_date=report_date,
        )
        
        # Send embed
        success = notifier.send_embed(
            title=embed_data["title"],
            description=embed_data.get("description", ""),
            fields=embed_data.get("fields", []),
            color=embed_data.get("color"),
            timestamp=embed_data.get("timestamp"),
        )
        
        if success:
            log.info("Discord daily report notification sent successfully")
        else:
            log.warning("Failed to send Discord daily report notification")
        
        return success
    except Exception as e:
        log.error(f"Failed to send Discord daily report notification: {e}", exc_info=True)
        return False


def main():
    """CLI entrypoint for daily report."""
    parser = argparse.ArgumentParser(
        description="Generate daily performance report"
    )
    parser.add_argument(
        "--config",
        default="config/config.yaml",
        help="Path to config file",
    )
    parser.add_argument(
        "--date",
        default=None,
        help="Report date (YYYY-MM-DD, defaults to today UTC)",
    )
    parser.add_argument(
        "--no-notify",
        action="store_true",
        help="Don't send Discord notification",
    )
    
    args = parser.parse_args()
    
    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    
    # Parse date
    report_date = None
    if args.date:
        try:
            report_date = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            log.error(f"Invalid date format: {args.date}, expected YYYY-MM-DD")
            return 1
    
    try:
        # Load config
        cfg = load_config(args.config)
        
        # Generate report
        report = generate_daily_report(cfg, report_date=report_date)
        
        # Print summary
        daily = report["daily_metrics"]
        cumul = report["cumulative_metrics"]
        
        print(f"\n=== Daily Report ({report['report_date']}) ===")
        print(f"Today PnL: ${daily.get('daily_pnl_usdt', 0):+.2f} ({daily.get('daily_pnl_pct', 0):+.2%})")
        print(f"Trades: {daily.get('total_trades', 0)}")
        print(f"Win rate: {daily.get('win_rate_pct', 0):.1f}%")
        print(f"Total PnL: ${cumul.get('total_pnl_usdt', 0):+.2f} ({cumul.get('total_pnl_pct', 0):+.2%})")
        print(f"Max DD: {cumul.get('max_drawdown_pct', 0):.2%}")
        print()
        
        # Send notification unless disabled
        if not args.no_notify:
            send_daily_report_notification(report, cfg)
        
        return 0
    except Exception as e:
        log.error(f"Daily report failed: {e}", exc_info=True)
        return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())

