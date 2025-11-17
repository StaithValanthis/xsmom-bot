"""
Knowledge Base (KB) update tool.

Automatically generates/updates documentation from codebase:
- Module map
- Config parameter reference
- Architecture snapshots
"""
from __future__ import annotations

import ast
import logging
import os
import re
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any
import yaml

log = logging.getLogger("update_kb")

# Color constants for markdown output
HEADER = "#"
SUBSECTION = "##"
SUBSUBSECTION = "###"


def scan_module_tree(src_dir: Path) -> Dict[str, Any]:
    """
    Scan src/ directory to build module tree with descriptions.
    
    Returns:
        Dict with module structure and one-liner descriptions
    """
    modules: Dict[str, Any] = {
        "core": [],
        "optimizer": [],
        "notifications": [],
        "reports": [],
        "legacy": [],
    }
    
    # Module descriptions (manually curated + auto-inferred)
    module_descriptions = {
        # Core
        "main.py": "Entry point: CLI for live/backtest modes",
        "config.py": "Pydantic config schema: type-safe configuration management",
        "live.py": "Live trading loop: orchestrates strategy execution, order management, risk checks",
        "backtester.py": "Cost-aware backtesting engine: simulates strategy with realistic costs",
        "signals.py": "Signal generation: momentum, regime filters, ADX, meta-labeler",
        "sizing.py": "Position sizing: inverse-volatility, Kelly scaling, caps, vol targeting",
        "risk.py": "Risk management: kill-switch, drawdown tracking, daily loss limits",
        "exchange.py": "CCXT wrapper: unified interface for Bybit USDT-perp exchange",
        "regime_router.py": "Regime switching: dynamically chooses XSMOM vs TSMOM based on market conditions",
        "carry.py": "Carry trading: funding/basis trades with delta-neutral hedging",
        "anti_churn.py": "Trade throttling: prevents overtrading via cooldowns and streak tracking",
        "utils.py": "Utilities: JSON I/O, logging setup, health checks",
        
        # Optimizer
        "optimizer/full_cycle.py": "Full-cycle optimizer: WFO + Bayesian + Monte Carlo orchestrator",
        "optimizer/walk_forward.py": "Walk-forward optimization: purged segments with embargo",
        "optimizer/bo_runner.py": "Bayesian optimization: Optuna TPE sampler for parameter search",
        "optimizer/monte_carlo.py": "Monte Carlo stress testing: bootstrap and cost perturbation",
        "optimizer/backtest_runner.py": "Backtest runner: clean entrypoint for optimizer with param overrides",
        "optimizer/config_manager.py": "Config manager: versioning, deployment, rollback",
        "optimizer/rollback_cli.py": "Rollback CLI: restore previous config versions",
        
        # Notifications
        "notifications/discord_notifier.py": "Discord webhook client: embeds, rate limiting, error handling",
        "notifications/optimizer_notifications.py": "Optimizer notifications: formats and sends optimizer results",
        
        # Reports
        "reports/daily_report.py": "Daily performance reports: PnL aggregation, Discord notifications",
        
        # Legacy/Other
        "optimizer_runner.py": "Legacy optimizer: Phase 1/2 grid search with PnL heuristics",
        "optimizer_cli.py": "Grid-based CLI optimizer: uses optimizer.grid.yaml",
        "optimizer.py": "Legacy simple grid optimizer",
        "optimizer_bayes.py": "Experimental Bayesian optimizer wrapper",
        "optimizer_purged_wf.py": "Purged walk-forward optimizer (basic implementation)",
        "backtest_cli.py": "Backtest CLI: command-line interface for running backtests",
        "meta_label_trainer.py": "Meta-labeler trainer: ML-based signal filtering (not integrated)",
        "auto_opt.py": "Auto-optimization helper (legacy)",
        "optimize_timeframe.py": "Timeframe optimization helper (legacy)",
    }
    
    # Scan src/
    for py_file in sorted(src_dir.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        
        desc = module_descriptions.get(py_file.name, "Core module")
        modules["core"].append({
            "name": py_file.name,
            "path": f"src/{py_file.name}",
            "description": desc,
        })
    
    # Scan subdirectories
    for subdir in ["optimizer", "notifications", "reports"]:
        subdir_path = src_dir / subdir
        if subdir_path.exists():
            for py_file in sorted(subdir_path.glob("*.py")):
                if py_file.name.startswith("_"):
                    continue
                
                key = subdir
                if subdir == "optimizer":
                    key = "optimizer"
                elif subdir == "notifications":
                    key = "notifications"
                elif subdir == "reports":
                    key = "reports"
                
                rel_path = f"src/{subdir}/{py_file.name}"
                desc = module_descriptions.get(rel_path, f"{subdir.title()} module")
                
                modules[key].append({
                    "name": py_file.name,
                    "path": rel_path,
                    "description": desc,
                })
    
    # Legacy modules
    legacy_patterns = [
        "optimizer_runner.py",
        "optimizer_cli.py",
        "optimizer.py",
        "optimizer_bayes.py",
        "optimizer_purged_wf.py",
        "auto_opt.py",
        "optimize_timeframe.py",
        "meta_label_trainer.py",
    ]
    
    for py_file in src_dir.glob("*.py"):
        if py_file.name in legacy_patterns:
            desc = module_descriptions.get(py_file.name, "Legacy module")
            modules["legacy"].append({
                "name": py_file.name,
                "path": f"src/{py_file.name}",
                "description": desc,
            })
    
    return modules


def extract_config_parameters(config_path: Path) -> List[Dict[str, Any]]:
    """
    Parse config.yaml to extract parameter information.
    
    Returns:
        List of parameter dicts with path, type, default, description
    """
    if not config_path.exists():
        log.warning(f"Config file not found: {config_path}")
        return []
    
    with open(config_path, "r") as f:
        config_data = yaml.safe_load(f) or {}
    
    parameters: List[Dict[str, Any]] = []
    
    # Recursively extract parameters with their paths
    def _extract_params(obj: Any, path: str = "", level: int = 0) -> None:
        if level > 10:  # Safety limit
            return
        
        if isinstance(obj, dict):
            for key, value in obj.items():
                current_path = f"{path}.{key}" if path else key
                
                # Determine type
                if isinstance(value, dict):
                    # Nested dict - recurse
                    _extract_params(value, current_path, level + 1)
                elif isinstance(value, list):
                    # List - record type
                    list_types = set(type(v).__name__ for v in value[:5] if value)
                    param_type = f"list[{', '.join(list_types) or 'any'}]" if list_types else "list"
                    parameters.append({
                        "path": current_path,
                        "type": param_type,
                        "default": str(value)[:100],  # Truncate long lists
                        "description": "",  # Can be enhanced with comments parsing
                    })
                else:
                    # Leaf value
                    param_type = type(value).__name__
                    parameters.append({
                        "path": current_path,
                        "type": param_type,
                        "default": value,
                        "description": "",  # Can be enhanced with comments parsing
                    })
    
    _extract_params(config_data)
    
    return parameters


def generate_module_map(modules: Dict[str, Any], output_path: Path) -> None:
    """
    Generate module_map.md from module tree.
    
    Args:
        modules: Module tree dict from scan_module_tree
        output_path: Path to write module_map.md
    """
    lines = [
        "# Module Map",
        "",
        "> **Auto-generated** by `tools/update_kb.py`",
        "",
        "This document maps all modules in `src/` with their responsibilities.",
        "",
        "---",
        "",
        "## Core Modules",
        "",
    ]
    
    for mod in modules["core"]:
        lines.append(f"### `{mod['path']}`")
        lines.append(f"")
        lines.append(f"{mod['description']}")
        lines.append("")
    
    if modules["optimizer"]:
        lines.extend([
            "",
            "## Optimizer Modules",
            "",
        ])
        for mod in modules["optimizer"]:
            lines.append(f"### `{mod['path']}`")
            lines.append(f"")
            lines.append(f"{mod['description']}")
            lines.append("")
    
    if modules["notifications"]:
        lines.extend([
            "",
            "## Notification Modules",
            "",
        ])
        for mod in modules["notifications"]:
            lines.append(f"### `{mod['path']}`")
            lines.append(f"")
            lines.append(f"{mod['description']}")
            lines.append("")
    
    if modules["reports"]:
        lines.extend([
            "",
            "## Report Modules",
            "",
        ])
        for mod in modules["reports"]:
            lines.append(f"### `{mod['path']}`")
            lines.append(f"")
            lines.append(f"{mod['description']}")
            lines.append("")
    
    if modules["legacy"]:
        lines.extend([
            "",
            "## Legacy Modules",
            "",
            "> **Note:** These modules are legacy or experimental. Consider migrating to newer alternatives.",
            "",
        ])
        for mod in modules["legacy"]:
            lines.append(f"### `{mod['path']}`")
            lines.append(f"")
            lines.append(f"{mod['description']}")
            lines.append("")
    
    output_path.write_text("\n".join(lines))
    log.info(f"Generated module map: {output_path}")


def generate_config_reference(
    parameters: List[Dict[str, Any]],
    output_path: Path,
    config_example_path: Optional[Path] = None,
) -> None:
    """
    Generate config_reference.md from parameters.
    
    Args:
        parameters: List of parameter dicts from extract_config_parameters
        output_path: Path to write config_reference.md
        config_example_path: Optional path to config.yaml.example for additional context
    """
    lines = [
        "# Config Parameter Reference",
        "",
        "> **Partially auto-generated** by `tools/update_kb.py`",
        "",
        "This document lists all configuration parameters with their types, defaults, and descriptions.",
        "",
        "**Legend:**",
        "- ⚙️ = Optimizable (good for optimizer)",
        "- 🔒 = Safety limit (optimize with caution or not at all)",
        "- ⚠️ = High overfitting risk (keep simple)",
        "- ❌ = Dead/unused parameter",
        "",
        "---",
        "",
    ]
    
    # Group parameters by section
    sections: Dict[str, List[Dict[str, Any]]] = {}
    
    for param in parameters:
        section = param["path"].split(".")[0]
        if section not in sections:
            sections[section] = []
        sections[section].append(param)
    
    # Known parameter descriptions (can be enhanced)
    param_descriptions = {
        # Exchange
        "exchange.id": "Exchange identifier (e.g., 'bybit')",
        "exchange.account_type": "Account type: 'swap' for futures",
        "exchange.quote": "Quote currency (e.g., 'USDT')",
        "exchange.max_symbols": "Maximum symbols in trading universe",
        "exchange.min_usd_volume_24h": "Minimum 24h volume filter (USD)",
        "exchange.timeframe": "OHLCV bar timeframe (e.g., '1h')",
        
        # Strategy
        "strategy.signal_power": "Nonlinear z-score amplification exponent",
        "strategy.lookbacks": "Momentum lookback periods (hours/bars)",
        "strategy.lookback_weights": "Weights for each lookback period",
        "strategy.k_min": "Minimum top-K selection (long/short pairs)",
        "strategy.k_max": "Maximum top-K selection",
        "strategy.gross_leverage": "Portfolio gross leverage cap",
        "strategy.max_weight_per_asset": "Per-asset weight cap (fraction of portfolio)",
        "strategy.entry_zscore_min": "Minimum entry z-score threshold",
        
        # Risk
        "risk.max_daily_loss_pct": "Daily loss kill-switch threshold (%)",
        "risk.atr_mult_sl": "Stop loss ATR multiplier",
        "risk.trail_atr_mult": "Trailing stop ATR multiplier",
        
        # Execution
        "execution.rebalance_minute": "Minute of hour to rebalance (0-59)",
        "execution.poll_seconds": "Poll interval for main loop",
        "execution.min_notional_per_order_usdt": "Minimum order notional (USDT)",
    }
    
    # Sort sections
    section_order = [
        "exchange",
        "strategy",
        "risk",
        "execution",
        "liquidity",
        "costs",
        "paths",
        "logging",
        "notifications",
    ]
    
    for section in section_order:
        if section not in sections:
            continue
        
        lines.append(f"## {section.title()}")
        lines.append("")
        lines.append("| Parameter | Type | Default | Description |")
        lines.append("|-----------|------|---------|-------------|")
        
        for param in sorted(sections[section], key=lambda x: x["path"]):
            path = param["path"]
            param_type = param["type"]
            default = param["default"]
            description = param_descriptions.get(path, "")
            
            # Format default for markdown
            if isinstance(default, str) and len(default) > 50:
                default = default[:47] + "..."
            default_str = f"`{default}`" if default != "" else "-"
            
            lines.append(f"| `{path}` | {param_type} | {default_str} | {description} |")
        
        lines.append("")
    
    # Add hand-written sections
    lines.extend([
        "---",
        "",
        "## Parameter Importance & Optimization",
        "",
        "### Core Optimizable Parameters (~18)",
        "",
        "These parameters are recommended for optimization:",
        "",
        "1. **Signals (6 params)**: `signal_power`, `lookbacks[0-2]`, `k_min`, `k_max`",
        "2. **Filters (3 params)**: `regime_filter.ema_len`, `regime_filter.slope_min_bps_per_day`, `entry_zscore_min`",
        "3. **Risk (5 params)**: `atr_mult_sl`, `trail_atr_mult`, `gross_leverage`, `max_weight_per_asset`, `portfolio_vol_target.target_ann_vol`",
        "4. **Enable/Disable (4 params)**: `regime_filter.enabled`, `adx_filter.enabled`, `vol_target_enabled`, `diversify_enabled`",
        "",
        "### Safety Limits (Do NOT Optimize Heavily)",
        "",
        "- `risk.max_daily_loss_pct` - Absolute safety limit",
        "- `risk.max_portfolio_drawdown_pct` - Catastrophic stop",
        "",
        "For detailed parameter analysis, see `docs/kb/parameter_review.md`.",
        "",
    ])
    
    output_path.write_text("\n".join(lines))
    log.info(f"Generated config reference: {output_path}")


def update_kb_timestamp(docs_dir: Path) -> None:
    """Update timestamp in KB knowledge_base.md."""
    kb_path = docs_dir / "kb" / "knowledge_base.md"
    if not kb_path.exists():
        return
    
    content = kb_path.read_text()
    
    # Find and update "Last updated" line
    timestamp_pattern = r"(Last updated: )\d{4}-\d{2}-\d{2}"
    from datetime import datetime
    new_timestamp = datetime.utcnow().strftime("%Y-%m-%d")
    
    if re.search(timestamp_pattern, content):
        content = re.sub(timestamp_pattern, rf"\1{new_timestamp}", content)
    else:
        # Add timestamp if not present
        content = content.replace(
            "## Knowledge Base",
            f"## Knowledge Base\n\n**Last updated:** {new_timestamp}",
            1
        )
    
    kb_path.write_text(content)
    log.info(f"Updated KB timestamp: {new_timestamp}")


def main():
    """Main entrypoint for KB update tool."""
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Update Knowledge Base documentation from codebase"
    )
    parser.add_argument(
        "--repo-root",
        type=str,
        default=".",
        help="Repository root directory",
    )
    parser.add_argument(
        "--skip-module-map",
        action="store_true",
        help="Skip module map generation",
    )
    parser.add_argument(
        "--skip-config-ref",
        action="store_true",
        help="Skip config reference generation",
    )
    
    args = parser.parse_args()
    
    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    
    repo_root = Path(args.repo_root).resolve()
    src_dir = repo_root / "src"
    docs_dir = repo_root / "docs"
    config_path = repo_root / "config" / "config.yaml.example"
    
    log.info(f"Updating KB from {repo_root}")
    
    if not src_dir.exists():
        log.error(f"src/ directory not found: {src_dir}")
        return 1
    
    # Ensure docs structure exists
    (docs_dir / "architecture").mkdir(parents=True, exist_ok=True)
    (docs_dir / "reference").mkdir(parents=True, exist_ok=True)
    (docs_dir / "kb" / "autogenerated").mkdir(parents=True, exist_ok=True)
    
    try:
        # Generate module map
        if not args.skip_module_map:
            log.info("Scanning module tree...")
            modules = scan_module_tree(src_dir)
            
            module_map_path = docs_dir / "architecture" / "module_map.md"
            generate_module_map(modules, module_map_path)
            
            # Also save to autogenerated
            (docs_dir / "kb" / "autogenerated" / "module_map.md").write_text(
                module_map_path.read_text()
            )
        
        # Generate config reference
        if not args.skip_config_ref:
            log.info("Extracting config parameters...")
            parameters = extract_config_parameters(config_path)
            
            config_ref_path = docs_dir / "reference" / "config_reference.md"
            generate_config_reference(
                parameters,
                config_ref_path,
                config_example_path=config_path,
            )
        
        # Update KB timestamp
        update_kb_timestamp(docs_dir)
        
        log.info("KB update complete!")
        return 0
    
    except Exception as e:
        log.error(f"KB update failed: {e}", exc_info=True)
        return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())

