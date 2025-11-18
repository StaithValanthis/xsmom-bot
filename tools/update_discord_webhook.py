#!/usr/bin/env python3
"""
Helper script to update Discord webhook URL in config.yaml.

Usage:
    python tools/update_discord_webhook.py --config config/config.yaml --webhook-url "https://..."
    python tools/update_discord_webhook.py --config config/config.yaml  # Prompts for URL
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
import yaml

def update_webhook_url(config_path: str, webhook_url: str) -> bool:
    """
    Update Discord webhook URL in config.yaml.
    
    Args:
        config_path: Path to config.yaml
        webhook_url: Discord webhook URL to set
    
    Returns:
        True if successful, False otherwise
    """
    config_file = Path(config_path)
    if not config_file.exists():
        print(f"Error: Config file not found: {config_path}", file=sys.stderr)
        return False
    
    try:
        # Read existing config
        with open(config_file, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f) or {}
        
        # Ensure notifications.discord structure exists
        if 'notifications' not in config:
            config['notifications'] = {}
        if 'discord' not in config['notifications']:
            config['notifications']['discord'] = {}
        
        # Set webhook_url
        config['notifications']['discord']['webhook_url'] = webhook_url
        
        # Enable notifications if not already set
        if 'enabled' not in config['notifications']['discord']:
            config['notifications']['discord']['enabled'] = True
        
        # Write back
        with open(config_file, 'w', encoding='utf-8') as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
        
        print(f"✓ Updated {config_path} with Discord webhook URL")
        print(f"  webhook_url: {webhook_url[:50]}..." if len(webhook_url) > 50 else f"  webhook_url: {webhook_url}")
        return True
        
    except Exception as e:
        print(f"Error updating config: {e}", file=sys.stderr)
        return False

def main():
    parser = argparse.ArgumentParser(description="Update Discord webhook URL in config.yaml")
    parser.add_argument("--config", type=str, default="config/config.yaml", help="Path to config.yaml")
    parser.add_argument("--webhook-url", type=str, help="Discord webhook URL (if not provided, will prompt)")
    
    args = parser.parse_args()
    
    webhook_url = args.webhook_url
    if not webhook_url:
        webhook_url = input("Enter Discord Webhook URL: ").strip()
        if not webhook_url:
            print("Error: Webhook URL cannot be empty", file=sys.stderr)
            return 1
    
    if update_webhook_url(args.config, webhook_url):
        return 0
    else:
        return 1

if __name__ == "__main__":
    sys.exit(main())

