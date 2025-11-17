"""
Discord notification client using webhooks.

Sends messages and embeds to Discord channels via webhook URLs.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Dict, Any, List, Optional
import requests

log = logging.getLogger("notifications.discord")


class DiscordNotifier:
    """Discord notification client via webhooks."""
    
    # Discord embed colors
    COLOR_GREEN = 0x00FF00    # Success (deployed, positive PnL)
    COLOR_ORANGE = 0xFFA500   # Warning (no change, moderate PnL)
    COLOR_RED = 0xFF0000      # Error (failed, negative PnL)
    COLOR_BLUE = 0x0099FF     # Info (neutral, reports)
    
    def __init__(
        self,
        webhook_url: Optional[str] = None,
        enabled: bool = True,
    ):
        """
        Initialize Discord notifier.
        
        Args:
            webhook_url: Optional webhook URL (if None, reads from env/config)
            enabled: If False, all methods are no-ops
        """
        self.enabled = enabled
        
        if not enabled:
            log.debug("Discord notifier disabled")
            self.webhook_url = None
            return
        
        # Resolve webhook URL: explicit > env var > config
        if webhook_url:
            self.webhook_url = webhook_url
        else:
            # Try environment variable first
            self.webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
            
            # If still None, will need to be set from config (caller responsibility)
            if not self.webhook_url:
                log.debug("Discord webhook URL not found in environment")
        
        if not self.webhook_url:
            log.warning(
                "Discord notifier initialized but no webhook URL available. "
                "Set DISCORD_WEBHOOK_URL env var or pass webhook_url to __init__"
            )
    
    def set_webhook_from_config(self, webhook_url: Optional[str]) -> None:
        """
        Set webhook URL from config (fallback if env var not set).
        
        Args:
            webhook_url: Webhook URL from config
        """
        if not self.webhook_url and webhook_url:
            self.webhook_url = webhook_url
            log.debug("Set Discord webhook URL from config")
    
    def _should_send(self) -> bool:
        """Check if notifications should be sent."""
        if not self.enabled:
            return False
        
        if not self.webhook_url:
            log.debug("Discord webhook URL not available, skipping send")
            return False
        
        return True
    
    def send_message(self, content: str) -> bool:
        """
        Send a simple text message to Discord.
        
        Args:
            content: Message content (max 2000 chars)
        
        Returns:
            True if successful, False otherwise
        """
        if not self._should_send():
            return False
        
        if len(content) > 2000:
            content = content[:1997] + "..."
        
        payload = {"content": content}
        
        try:
            response = requests.post(
                self.webhook_url,
                json=payload,
                timeout=10,
            )
            response.raise_for_status()
            log.debug("Discord message sent successfully")
            return True
        except requests.exceptions.HTTPError as e:
            # Handle rate limiting
            if e.response.status_code == 429:
                retry_after = e.response.headers.get("Retry-After", "5")
                try:
                    wait_seconds = int(retry_after)
                except ValueError:
                    wait_seconds = 5
                
                log.warning(f"Discord rate limited, retrying after {wait_seconds}s")
                time.sleep(wait_seconds)
                
                try:
                    response = requests.post(
                        self.webhook_url,
                        json=payload,
                        timeout=10,
                    )
                    response.raise_for_status()
                    log.debug("Discord message sent after retry")
                    return True
                except Exception as retry_error:
                    log.error(f"Discord message failed after retry: {retry_error}")
                    return False
            else:
                log.error(f"Discord HTTP error: {e.response.status_code} - {e}")
                return False
        except requests.exceptions.RequestException as e:
            log.error(f"Discord request failed: {e}")
            return False
        except Exception as e:
            log.error(f"Discord send failed: {e}")
            return False
    
    def send_embed(
        self,
        title: str,
        description: str = "",
        fields: Optional[List[Dict[str, Any]]] = None,
        color: Optional[int] = None,
        footer: Optional[str] = None,
        timestamp: Optional[str] = None,
    ) -> bool:
        """
        Send a Discord embed message.
        
        Args:
            title: Embed title
            description: Embed description (max 4096 chars)
            fields: List of field dicts with keys: name, value, inline (optional)
            color: Embed color (integer, 0xRRGGBB format)
            footer: Optional footer text
            timestamp: Optional ISO timestamp string
        
        Returns:
            True if successful, False otherwise
        """
        if not self._should_send():
            return False
        
        embed = {
            "title": title[:256],  # Discord limit
            "description": description[:4096] if description else "",
            "color": color if color is not None else self.COLOR_BLUE,
        }
        
        if fields:
            embed["fields"] = []
            for field in fields:
                embed["fields"].append({
                    "name": str(field.get("name", ""))[:256],
                    "value": str(field.get("value", ""))[:1024],
                    "inline": bool(field.get("inline", False)),
                })
        
        if footer:
            embed["footer"] = {"text": footer[:2048]}
        
        if timestamp:
            embed["timestamp"] = timestamp
        
        payload = {"embeds": [embed]}
        
        try:
            response = requests.post(
                self.webhook_url,
                json=payload,
                timeout=10,
            )
            response.raise_for_status()
            log.debug("Discord embed sent successfully")
            return True
        except requests.exceptions.HTTPError as e:
            # Handle rate limiting
            if e.response.status_code == 429:
                retry_after = e.response.headers.get("Retry-After", "5")
                try:
                    wait_seconds = int(retry_after)
                except ValueError:
                    wait_seconds = 5
                
                log.warning(f"Discord rate limited, retrying after {wait_seconds}s")
                time.sleep(wait_seconds)
                
                try:
                    response = requests.post(
                        self.webhook_url,
                        json=payload,
                        timeout=10,
                    )
                    response.raise_for_status()
                    log.debug("Discord embed sent after retry")
                    return True
                except Exception as retry_error:
                    log.error(f"Discord embed failed after retry: {retry_error}")
                    return False
            else:
                log.error(f"Discord HTTP error: {e.response.status_code} - {e}")
                return False
        except requests.exceptions.RequestException as e:
            log.error(f"Discord request failed: {e}")
            return False
        except Exception as e:
            log.error(f"Discord send failed: {e}")
            return False

