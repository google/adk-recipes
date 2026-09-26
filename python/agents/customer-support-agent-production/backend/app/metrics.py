"""
Metrics collection and exposure for monitoring.

Provides:
- Request counters by endpoint and status code
- Latency histograms
- Error rate tracking
- Rate limiter statistics
- Custom application metrics

Compatible with Prometheus format for scraping.
"""

import time
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from threading import Lock
from typing import Any, Dict, List, Optional

from .logging_config import get_logger

logger = get_logger(__name__)


# =============================================================================
# METRICS STORAGE
# =============================================================================


@dataclass
class RequestMetrics:
    """Metrics for a single endpoint."""

    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    total_latency_ms: float = 0.0
    latencies: List[float] = field(default_factory=list)
    status_codes: Dict[int, int] = field(default_factory=lambda: defaultdict(int))
    lock: Lock = field(default_factory=Lock)

    def record(self, latency_ms: float, status_code: int) -> None:
        """Record a request."""
        with self.lock:
            self.total_requests += 1
            self.total_latency_ms += latency_ms

            if 200 <= status_code < 400:
                self.successful_requests += 1
            else:
                self.failed_requests += 1

            self.status_codes[status_code] += 1

            # Keep last 1000 latencies for percentile calculations
            self.latencies.append(latency_ms)
            if len(self.latencies) > 1000:
                self.latencies.pop(0)

    def get_percentile(self, p: float) -> Optional[float]:
        """Get the p-th percentile latency."""
        with self.lock:
            if not self.latencies:
                return None
            sorted_latencies = sorted(self.latencies)
            idx = int(len(sorted_latencies) * p / 100)
            return sorted_latencies[min(idx, len(sorted_latencies) - 1)]

    def get_avg_latency(self) -> Optional[float]:
        """Get average latency in ms."""
        with self.lock:
            if self.total_requests == 0:
                return None
            return self.total_latency_ms / self.total_requests

    def get_error_rate(self) -> float:
        """Get error rate as percentage."""
        with self.lock:
            if self.total_requests == 0:
                return 0.0
            return (self.failed_requests / self.total_requests) * 100


class MetricsCollector:
    """
    Collects and stores application metrics.

    Thread-safe singleton for global metrics collection.
    """

    _instance = None
    _lock = Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self.endpoints: Dict[str, RequestMetrics] = defaultdict(RequestMetrics)
        self.start_time = datetime.utcnow()
        self.custom_counters: Dict[str, int] = defaultdict(int)
        self.custom_gauges: Dict[str, float] = {}
        self._lock = Lock()
        self._initialized = True

    def record_request(self, endpoint: str, method: str, latency_ms: float, status_code: int) -> None:
        """Record a request to an endpoint."""
        key = f"{method}:{endpoint}"
        self.endpoints[key].record(latency_ms, status_code)

    def increment_counter(self, name: str, value: int = 1) -> None:
        """Increment a custom counter."""
        with self._lock:
            self.custom_counters[name] += value

    def set_gauge(self, name: str, value: float) -> None:
        """Set a custom gauge value."""
        with self._lock:
            self.custom_gauges[name] = value

    def get_counter(self, name: str) -> int:
        """Get a custom counter value."""
        with self._lock:
            return self.custom_counters.get(name, 0)

    def get_gauge(self, name: str) -> Optional[float]:
        """Get a custom gauge value."""
        with self._lock:
            return self.custom_gauges.get(name)

    @contextmanager
    def measure_latency(self, endpoint: str, method: str):
        """Context manager to measure request latency."""
        start = time.time()
        status_code = 200
        try:
            yield lambda code: setattr(status_code, "value", code) if hasattr(status_code, "value") else None
        except Exception:
            status_code = 500
            raise
        finally:
            latency_ms = (time.time() - start) * 1000
            self.record_request(endpoint, method, latency_ms, status_code)

    def get_all_metrics(self) -> Dict[str, Any]:
        """Get all metrics as a dictionary."""
        uptime = datetime.utcnow() - self.start_time

        # Aggregate endpoint metrics
        endpoint_metrics = {}
        total_requests = 0
        total_errors = 0

        for key, metrics in self.endpoints.items():
            total_requests += metrics.total_requests
            total_errors += metrics.failed_requests

            endpoint_metrics[key] = {
                "total_requests": metrics.total_requests,
                "successful_requests": metrics.successful_requests,
                "failed_requests": metrics.failed_requests,
                "error_rate_percent": round(metrics.get_error_rate(), 2),
                "avg_latency_ms": round(metrics.get_avg_latency() or 0, 2),
                "p50_latency_ms": round(metrics.get_percentile(50) or 0, 2),
                "p95_latency_ms": round(metrics.get_percentile(95) or 0, 2),
                "p99_latency_ms": round(metrics.get_percentile(99) or 0, 2),
                "status_codes": dict(metrics.status_codes),
            }

        return {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "uptime_seconds": int(uptime.total_seconds()),
            "summary": {
                "total_requests": total_requests,
                "total_errors": total_errors,
                "overall_error_rate_percent": round(
                    (total_errors / total_requests * 100) if total_requests > 0 else 0, 2
                ),
            },
            "endpoints": endpoint_metrics,
            "counters": dict(self.custom_counters),
            "gauges": dict(self.custom_gauges),
        }

    def get_prometheus_format(self) -> str:
        """Export metrics in Prometheus format."""
        lines = []

        # Help and type comments
        lines.append("# HELP http_requests_total Total HTTP requests")
        lines.append("# TYPE http_requests_total counter")

        lines.append("# HELP http_request_duration_ms HTTP request latency in milliseconds")
        lines.append("# TYPE http_request_duration_ms gauge")

        lines.append("# HELP http_errors_total Total HTTP errors")
        lines.append("# TYPE http_errors_total counter")

        # Endpoint metrics
        for key, metrics in self.endpoints.items():
            method, endpoint = key.split(":", 1)
            labels = f'method="{method}",endpoint="{endpoint}"'

            lines.append(f"http_requests_total{{{labels}}} {metrics.total_requests}")
            lines.append(f"http_errors_total{{{labels}}} {metrics.failed_requests}")

            avg_latency = metrics.get_avg_latency()
            if avg_latency is not None:
                lines.append(f"http_request_duration_ms{{{labels}}} {avg_latency:.2f}")

            # Status code breakdown
            for status_code, count in metrics.status_codes.items():
                lines.append(f'http_requests_total{{{labels},status="{status_code}"}} {count}')

        # Custom counters
        for name, value in self.custom_counters.items():
            lines.append(f"# HELP {name} Custom counter")
            lines.append(f"# TYPE {name} counter")
            lines.append(f"{name} {value}")

        # Custom gauges
        for name, value in self.custom_gauges.items():
            lines.append(f"# HELP {name} Custom gauge")
            lines.append(f"# TYPE {name} gauge")
            lines.append(f"{name} {value}")

        return "\n".join(lines)

    def reset(self) -> None:
        """Reset all metrics (useful for testing)."""
        with self._lock:
            self.endpoints.clear()
            self.custom_counters.clear()
            self.custom_gauges.clear()
            self.start_time = datetime.utcnow()


# Global metrics collector instance
metrics = MetricsCollector()


# =============================================================================
# FASTAPI MIDDLEWARE
# =============================================================================


async def metrics_middleware(request, call_next):
    """
    FastAPI middleware to collect request metrics.

    Usage:
        app.middleware("http")(metrics_middleware)
    """
    start_time = time.time()

    try:
        response = await call_next(request)
        status_code = response.status_code
    except Exception:
        status_code = 500
        raise
    finally:
        latency_ms = (time.time() - start_time) * 1000
        endpoint = request.url.path
        method = request.method

        # Don't track metrics endpoints to avoid recursion
        if not endpoint.startswith("/metrics"):
            metrics.record_request(endpoint, method, latency_ms, status_code)

    return response


# =============================================================================
# CONVENIENCE FUNCTIONS
# =============================================================================


def increment_chat_requests():
    """Increment chat request counter."""
    metrics.increment_counter("chat_requests_total")


def increment_chat_errors():
    """Increment chat error counter."""
    metrics.increment_counter("chat_errors_total")


def increment_rate_limit_hits():
    """Increment rate limit hit counter."""
    metrics.increment_counter("rate_limit_hits_total")


def set_active_sessions(count: int):
    """Set active sessions gauge."""
    metrics.set_gauge("active_sessions", count)


def set_active_users(count: int):
    """Set active users gauge."""
    metrics.set_gauge("active_users", count)
