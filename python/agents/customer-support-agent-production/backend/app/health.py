"""
Health check module for production monitoring.

Provides comprehensive health checks for:
- Database (Firestore) connectivity
- Agent Engine availability
- Overall system health status

Returns appropriate status codes for load balancer routing:
- 200: Healthy - all systems operational
- 503: Degraded - some systems unavailable
"""

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional

from .logging_config import get_logger

logger = get_logger(__name__)


class HealthStatus(str, Enum):
    """Health status values."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


@dataclass
class ComponentHealth:
    """Health status for a single component."""

    name: str
    status: HealthStatus
    latency_ms: Optional[float] = None
    message: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "status": self.status.value,
        }
        if self.latency_ms is not None:
            result["latency_ms"] = round(self.latency_ms, 2)
        if self.message:
            result["message"] = self.message
        if self.details:
            result["details"] = self.details
        return result


@dataclass
class HealthCheckResult:
    """Overall health check result."""

    status: HealthStatus
    components: Dict[str, ComponentHealth]
    timestamp: str
    version: str = "2.0.0"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status.value,
            "timestamp": self.timestamp,
            "version": self.version,
            "components": {name: comp.to_dict() for name, comp in self.components.items()},
        }


class HealthChecker:
    """
    Performs health checks on system components.

    Usage:
        checker = HealthChecker(db=database, agent_client=agent)
        result = await checker.check_all()
    """

    def __init__(self, db=None, agent_client=None, timeout_seconds: float = 5.0):
        self.db = db
        self.agent_client = agent_client
        self.timeout_seconds = timeout_seconds

    async def check_database(self) -> ComponentHealth:
        """Check Firestore database connectivity."""
        start = time.time()
        try:
            if not self.db:
                return ComponentHealth(
                    name="database", status=HealthStatus.UNHEALTHY, message="Database client not configured"
                )

            # Try a lightweight read operation
            # Check if we can access a collection (doesn't need to have data)
            await asyncio.wait_for(asyncio.to_thread(self._check_db_sync), timeout=self.timeout_seconds)

            latency = (time.time() - start) * 1000
            return ComponentHealth(
                name="database", status=HealthStatus.HEALTHY, latency_ms=latency, details={"type": "firestore"}
            )

        except asyncio.TimeoutError:
            latency = (time.time() - start) * 1000
            logger.warning("Database health check timed out", latency_ms=latency)
            return ComponentHealth(
                name="database", status=HealthStatus.UNHEALTHY, latency_ms=latency, message="Connection timed out"
            )
        except Exception as e:
            latency = (time.time() - start) * 1000
            logger.error("Database health check failed", error=str(e))
            return ComponentHealth(name="database", status=HealthStatus.UNHEALTHY, latency_ms=latency, message=str(e))

    def _check_db_sync(self) -> bool:
        """Synchronous database check (run in thread)."""
        # Try to list collections or get a document
        # This is a lightweight operation that verifies connectivity
        try:
            # Attempt to access a known collection
            list(self.db.db.collection("users").limit(1).stream())
            return True
        except Exception:
            # Fallback: just try to get any collection
            list(self.db.db.collections())
            return True

    async def check_agent_engine(self) -> ComponentHealth:
        """Check Agent Engine availability."""
        start = time.time()
        try:
            if not self.agent_client:
                return ComponentHealth(
                    name="agent_engine", status=HealthStatus.DEGRADED, message="Agent client not configured"
                )

            # Check if agent client is initialized
            if hasattr(self.agent_client, "agent_engine_app") and self.agent_client.agent_engine_app:
                latency = (time.time() - start) * 1000
                return ComponentHealth(
                    name="agent_engine",
                    status=HealthStatus.HEALTHY,
                    latency_ms=latency,
                    details={"resource_name": getattr(self.agent_client, "resource_name", "unknown")},
                )
            else:
                return ComponentHealth(
                    name="agent_engine", status=HealthStatus.DEGRADED, message="Agent engine not initialized"
                )

        except Exception as e:
            latency = (time.time() - start) * 1000
            logger.error("Agent engine health check failed", error=str(e))
            return ComponentHealth(
                name="agent_engine", status=HealthStatus.UNHEALTHY, latency_ms=latency, message=str(e)
            )

    async def check_all(self) -> HealthCheckResult:
        """
        Run all health checks concurrently.

        Returns:
            HealthCheckResult with overall status and component details
        """
        # Run checks concurrently
        db_check, agent_check = await asyncio.gather(
            self.check_database(), self.check_agent_engine(), return_exceptions=True
        )

        # Handle any exceptions from gather
        components = {}

        if isinstance(db_check, Exception):
            components["database"] = ComponentHealth(
                name="database", status=HealthStatus.UNHEALTHY, message=str(db_check)
            )
        else:
            components["database"] = db_check

        if isinstance(agent_check, Exception):
            components["agent_engine"] = ComponentHealth(
                name="agent_engine", status=HealthStatus.UNHEALTHY, message=str(agent_check)
            )
        else:
            components["agent_engine"] = agent_check

        # Determine overall status
        statuses = [c.status for c in components.values()]

        if all(s == HealthStatus.HEALTHY for s in statuses):
            overall_status = HealthStatus.HEALTHY
        elif any(s == HealthStatus.UNHEALTHY for s in statuses):
            # If database is unhealthy, system is unhealthy
            if components.get("database", ComponentHealth("", HealthStatus.HEALTHY)).status == HealthStatus.UNHEALTHY:
                overall_status = HealthStatus.UNHEALTHY
            else:
                overall_status = HealthStatus.DEGRADED
        else:
            overall_status = HealthStatus.DEGRADED

        return HealthCheckResult(
            status=overall_status, components=components, timestamp=datetime.utcnow().isoformat() + "Z"
        )


# Liveness probe - always returns 200 if process is running
async def liveness_check() -> Dict[str, str]:
    """
    Kubernetes liveness probe.

    Returns 200 if the process is running.
    Used by k8s to determine if pod should be restarted.
    """
    return {"status": "alive", "timestamp": datetime.utcnow().isoformat() + "Z"}


# Readiness probe - checks if service can handle requests
async def readiness_check(health_checker: HealthChecker) -> Dict[str, Any]:
    """
    Kubernetes readiness probe.

    Returns 200 if service is ready to handle requests.
    Used by k8s to determine if pod should receive traffic.
    """
    result = await health_checker.check_all()

    # Only ready if healthy or degraded (can still serve some requests)
    is_ready = result.status in (HealthStatus.HEALTHY, HealthStatus.DEGRADED)

    return {"ready": is_ready, "status": result.status.value, "timestamp": result.timestamp}
