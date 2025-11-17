"""
Risk controller module for MAKE MONEY hardening.

Centralizes risk checks: circuit breaker, margin protection, position reconciliation.
"""
from __future__ import annotations
import logging
import time
from typing import Dict, Optional, Tuple, List
from collections import deque
from datetime import datetime, timedelta

log = logging.getLogger("risk_controller")


class APICircuitBreaker:
    """Circuit breaker for API failures."""
    
    def __init__(
        self,
        max_errors: int = 5,
        window_seconds: int = 300,
        cooldown_seconds: int = 600,
    ):
        self.max_errors = max_errors
        self.window_seconds = window_seconds
        self.cooldown_seconds = cooldown_seconds
        self.error_timestamps: deque = deque(maxlen=max_errors * 2)  # Keep extra for safety
        self.tripped_at: Optional[float] = None
        self.tripped_count = 0
    
    def record_error(self) -> None:
        """Record an API error."""
        now = time.time()
        self.error_timestamps.append(now)
    
    def record_success(self) -> None:
        """Record a successful API call (helps recovery)."""
        # Don't clear errors, but don't add new ones
        pass
    
    def is_tripped(self) -> bool:
        """Check if circuit breaker is tripped."""
        now = time.time()
        
        # If tripped, check if cooldown period has passed
        if self.tripped_at is not None:
            if now - self.tripped_at >= self.cooldown_seconds:
                # Cooldown expired, reset
                log.info(f"Circuit breaker cooldown expired. Resetting.")
                self.tripped_at = None
                self.tripped_count = 0
                self.error_timestamps.clear()
                return False
            return True
        
        # Check if error rate exceeds threshold
        if len(self.error_timestamps) < self.max_errors:
            return False
        
        # Count errors in window
        cutoff = now - self.window_seconds
        recent_errors = [ts for ts in self.error_timestamps if ts >= cutoff]
        
        if len(recent_errors) >= self.max_errors:
            # Trip the circuit breaker
            if self.tripped_at is None:
                self.tripped_at = now
                self.tripped_count += 1
                log.error(
                    f"API CIRCUIT BREAKER TRIPPED: {len(recent_errors)} errors in {self.window_seconds}s. "
                    f"Trading paused for {self.cooldown_seconds}s."
                )
            return True
        
        return False
    
    def get_status(self) -> Dict[str, any]:
        """Get current circuit breaker status."""
        now = time.time()
        cutoff = now - self.window_seconds
        recent_errors = [ts for ts in self.error_timestamps if ts >= cutoff]
        
        return {
            "tripped": self.is_tripped(),
            "recent_errors": len(recent_errors),
            "max_errors": self.max_errors,
            "window_seconds": self.window_seconds,
            "tripped_at": self.tripped_at,
            "cooldown_remaining": max(0, self.cooldown_seconds - (now - self.tripped_at)) if self.tripped_at else 0,
        }


def check_margin_ratio(
    equity: float,
    used_margin: float,
    soft_limit_pct: float = 0.0,
    hard_limit_pct: float = 0.0,
) -> Tuple[bool, bool, float]:
    """
    Check margin ratio against limits.
    
    Args:
        equity: Total equity
        used_margin: Used margin
        soft_limit_pct: Soft limit (pause new trades), 0.0 = disabled
        hard_limit_pct: Hard limit (close positions), 0.0 = disabled
    
    Returns:
        Tuple of (soft_limit_exceeded, hard_limit_exceeded, margin_usage_pct)
    """
    if equity <= 0:
        return False, False, 0.0
    
    margin_usage_pct = (used_margin / equity) * 100.0
    
    soft_exceeded = soft_limit_pct > 0.0 and margin_usage_pct >= soft_limit_pct
    hard_exceeded = hard_limit_pct > 0.0 and margin_usage_pct >= hard_limit_pct
    
    return soft_exceeded, hard_exceeded, margin_usage_pct

