"""
CLI for rolling back to previous config versions.
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path
from .config_manager import ConfigManager

log = logging.getLogger("optimizer.rollback")


def main():
    """CLI entrypoint for config rollback."""
    parser = argparse.ArgumentParser(
        description="Rollback to a previous config version"
    )
    parser.add_argument(
        "--live-config",
        default="config/config.yaml",
        help="Path to live config file",
    )
    parser.add_argument(
        "--to",
        required=True,
        help="Timestamp string (YYYYMMDD_HHMMSS) or 'latest'",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Don't backup current live config before rollback",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available versions and exit",
    )
    
    args = parser.parse_args()
    
    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    
    config_manager = ConfigManager(
        live_config_path=args.live_config,
        optimized_dir="config/optimized",
    )
    
    if args.list:
        versions = config_manager.list_versions()
        print(f"\nAvailable config versions ({len(versions)}):")
        print("-" * 80)
        for v in versions[:20]:  # Show latest 20
            ts = v["timestamp"]
            metadata = v.get("metadata", {})
            metrics = metadata.get("candidate_metrics", {})
            sharpe = metrics.get("oos_sharpe_mean", "N/A")
            ann = metrics.get("oos_annualized_mean", "N/A")
            print(f"{ts} | Sharpe={sharpe}, Annualized={ann}")
        print()
        return 0
    
    try:
        success = config_manager.rollback_to_version(
            args.to,
            create_backup=not args.no_backup,
        )
        
        if success:
            print(f"Successfully rolled back to version: {args.to}")
            return 0
        else:
            print(f"Failed to rollback to version: {args.to}")
            return 1
    except Exception as e:
        log.error(f"Rollback failed: {e}", exc_info=True)
        return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())

