"""
Rate limiting middleware for the Customer Support API.

Implements sliding window rate limiting to prevent abuse:
- Per-user limits for authenticated users
- Per-IP limits for anonymous/unauthenticated requests
- Different limits for different endpoint types
"""

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from threading import Lock
from typing import Dict, Optional, Tuple

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)


# =============================================================================
# CONFIGURATION
# =============================================================================


@dataclass
class RateLimitConfig:
    """Configuration for rate limit rules."""

    requests_per_minute: int = 30  # Default: 30 requests per minute
    requests_per_hour: int = 500  # Default: 500 requests per hour
    burst_limit: int = 10  # Max burst in short period


# Pre-defined configs for different endpoint types
RATE_LIMITS = {
    "default": RateLimitConfig(requests_per_minute=30, requests_per_hour=500, burst_limit=10),
    "chat": RateLimitConfig(requests_per_minute=20, requests_per_hour=300, burst_limit=5),
    "auth": RateLimitConfig(requests_per_minute=10, requests_per_hour=50, burst_limit=3),
    "sessions": RateLimitConfig(requests_per_minute=60, requests_per_hour=1000, burst_limit=20),
}


# =============================================================================
# SLIDING WINDOW RATE LIMITER
# =============================================================================


@dataclass
class RequestWindow:
    """Tracks requests in a sliding window."""

    timestamps: list = field(default_factory=list)
    lock: Lock = field(default_factory=Lock)


class SlidingWindowRateLimiter:
    """
    Sliding window rate limiter with per-user and per-IP tracking.

    Uses in-memory storage (suitable for single-instance deployments).
    For multi-instance deployments, replace with Redis-based implementation.
    """

    def __init__(self):
        # User-based tracking: {user_id: {endpoint_type: RequestWindow}}
        self._user_windows: Dict[str, Dict[str, RequestWindow]] = defaultdict(lambda: defaultdict(RequestWindow))
        # IP-based tracking: {ip_address: {endpoint_type: RequestWindow}}
        self._ip_windows: Dict[str, Dict[str, RequestWindow]] = defaultdict(lambda: defaultdict(RequestWindow))
        self._global_lock = Lock()

    def _get_window(self, identifier: str, endpoint_type: str, is_user: bool = True) -> RequestWindow:
        """Get or create a request window for the identifier."""
        windows = self._user_windows if is_user else self._ip_windows
        return windows[identifier][endpoint_type]

    def _clean_old_requests(
        self,
        window: RequestWindow,
        current_time: float,
        max_age_seconds: int = 3600,  # 1 hour
    ) -> None:
        """Remove requests older than max_age_seconds."""
        cutoff = current_time - max_age_seconds
        window.timestamps = [t for t in window.timestamps if t > cutoff]

    def _count_requests_in_period(self, window: RequestWindow, current_time: float, period_seconds: int) -> int:
        """Count requests within the specified period."""
        cutoff = current_time - period_seconds
        return sum(1 for t in window.timestamps if t > cutoff)

    def check_rate_limit(
        self, identifier: str, endpoint_type: str = "default", is_user: bool = True
    ) -> Tuple[bool, Optional[str], Optional[int]]:
        """
        Check if request is allowed under rate limits.

        Args:
            identifier: User ID or IP address
            endpoint_type: Type of endpoint (chat, auth, sessions, default)
            is_user: True if identifier is user_id, False if IP address

        Returns:
            Tuple of (allowed, error_message, retry_after_seconds)
        """
        config = RATE_LIMITS.get(endpoint_type, RATE_LIMITS["default"])
        current_time = time.time()
        window = self._get_window(identifier, endpoint_type, is_user)

        with window.lock:
            # Clean old requests
            self._clean_old_requests(window, current_time)

            # Check minute limit
            requests_last_minute = self._count_requests_in_period(window, current_time, 60)
            if requests_last_minute >= config.requests_per_minute:
                retry_after = 60 - int(current_time - min(t for t in window.timestamps if t > current_time - 60))
                logger.warning(
                    f"Rate limit exceeded (minute): {identifier} - "
                    f"{requests_last_minute}/{config.requests_per_minute}"
                )
                return False, "Rate limit exceeded. Too many requests per minute.", retry_after

            # Check hour limit
            requests_last_hour = self._count_requests_in_period(window, current_time, 3600)
            if requests_last_hour >= config.requests_per_hour:
                retry_after = 3600 - int(current_time - min(t for t in window.timestamps if t > current_time - 3600))
                logger.warning(
                    f"Rate limit exceeded (hour): {identifier} - " f"{requests_last_hour}/{config.requests_per_hour}"
                )
                return False, "Rate limit exceeded. Too many requests per hour.", min(retry_after, 300)

            # Check burst limit (last 10 seconds)
            requests_last_10s = self._count_requests_in_period(window, current_time, 10)
            if requests_last_10s >= config.burst_limit:
                logger.warning(f"Burst limit exceeded: {identifier} - " f"{requests_last_10s}/{config.burst_limit}")
                return False, "Too many requests. Please slow down.", 10

            # Request allowed - record it
            window.timestamps.append(current_time)

            return True, None, None

    def get_remaining_requests(
        self, identifier: str, endpoint_type: str = "default", is_user: bool = True
    ) -> Dict[str, int]:
        """
        Get remaining requests for the identifier.

        Returns dict with remaining requests for minute and hour windows.
        """
        config = RATE_LIMITS.get(endpoint_type, RATE_LIMITS["default"])
        current_time = time.time()
        window = self._get_window(identifier, endpoint_type, is_user)

        with window.lock:
            requests_last_minute = self._count_requests_in_period(window, current_time, 60)
            requests_last_hour = self._count_requests_in_period(window, current_time, 3600)

        return {
            "remaining_minute": max(0, config.requests_per_minute - requests_last_minute),
            "remaining_hour": max(0, config.requests_per_hour - requests_last_hour),
            "limit_minute": config.requests_per_minute,
            "limit_hour": config.requests_per_hour,
        }

    def reset(self, identifier: str = None) -> None:
        """Reset rate limits. If identifier provided, reset only for that identifier."""
        with self._global_lock:
            if identifier:
                self._user_windows.pop(identifier, None)
                self._ip_windows.pop(identifier, None)
            else:
                self._user_windows.clear()
                self._ip_windows.clear()


# Global rate limiter instance
rate_limiter = SlidingWindowRateLimiter()


# =============================================================================
# FASTAPI DEPENDENCIES
# =============================================================================


def get_client_ip(request: Request) -> str:
    """
    Extract client IP from request, handling proxies.

    Note: In production behind a load balancer, configure trusted proxy headers.
    """
    # Check for forwarded headers (when behind proxy/load balancer)
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        # Take the first IP (original client)
        return forwarded_for.split(",")[0].strip()

    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()

    # Fallback to direct connection IP
    if request.client:
        return request.client.host

    return "unknown"


class RateLimitDependency:
    """
    FastAPI dependency for rate limiting.

    Usage:
        @app.post("/api/chat")
        async def chat(rate_check: bool = Depends(RateLimitDependency("chat"))):
            ...
    """

    def __init__(self, endpoint_type: str = "default"):
        self.endpoint_type = endpoint_type

    async def __call__(
        self,
        request: Request,
    ) -> bool:
        """
        Check rate limit for the request.

        Raises HTTPException(429) if rate limit exceeded.
        """
        # Try to get user_id from request state (set by auth middleware)
        user_id = getattr(request.state, "user_id", None)

        # Determine identifier and type
        if user_id:
            identifier = user_id
            is_user = True
        else:
            identifier = get_client_ip(request)
            is_user = False

        # Check rate limit
        allowed, error_message, retry_after = rate_limiter.check_rate_limit(
            identifier=identifier, endpoint_type=self.endpoint_type, is_user=is_user
        )

        if not allowed:
            headers = {}
            if retry_after:
                headers["Retry-After"] = str(retry_after)

            # Add rate limit headers
            remaining = rate_limiter.get_remaining_requests(identifier, self.endpoint_type, is_user)
            headers["X-RateLimit-Limit"] = str(remaining["limit_minute"])
            headers["X-RateLimit-Remaining"] = str(remaining["remaining_minute"])

            raise HTTPException(status_code=429, detail=error_message, headers=headers)

        return True


# Convenience functions for common endpoint types
def rate_limit_chat():
    """Rate limit dependency for chat endpoint."""
    return RateLimitDependency("chat")


def rate_limit_auth():
    """Rate limit dependency for auth endpoints."""
    return RateLimitDependency("auth")


def rate_limit_sessions():
    """Rate limit dependency for session endpoints."""
    return RateLimitDependency("sessions")


def rate_limit_default():
    """Default rate limit dependency."""
    return RateLimitDependency("default")
