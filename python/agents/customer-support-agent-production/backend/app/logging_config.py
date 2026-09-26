"""
Structured logging configuration for production.

Provides:
- JSON-formatted logs for easy parsing by log aggregators
- Request context (request_id, user_id, session_id)
- Correlation IDs for tracing requests across services
- Configurable log levels per environment
"""

import json
import logging
import sys
import uuid
from contextvars import ContextVar
from datetime import datetime
from functools import wraps
from typing import Any, Dict, Optional

# Context variables for request-scoped data
request_id_var: ContextVar[Optional[str]] = ContextVar("request_id", default=None)
user_id_var: ContextVar[Optional[str]] = ContextVar("user_id", default=None)
session_id_var: ContextVar[Optional[str]] = ContextVar("session_id", default=None)


# =============================================================================
# STRUCTURED LOG FORMATTER
# =============================================================================


class StructuredFormatter(logging.Formatter):
    """
    JSON formatter that includes request context automatically.

    Output format:
    {
        "timestamp": "2025-01-15T10:30:00.000Z",
        "level": "INFO",
        "logger": "app.main",
        "message": "Chat request processed",
        "request_id": "abc-123",
        "user_id": "demo-user-001",
        "session_id": "sess-456",
        "extra": {...}
    }
    """

    def format(self, record: logging.LogRecord) -> str:
        # Base log entry
        log_entry = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Add request context from context vars
        request_id = request_id_var.get()
        if request_id:
            log_entry["request_id"] = request_id

        user_id = user_id_var.get()
        if user_id:
            log_entry["user_id"] = user_id

        session_id = session_id_var.get()
        if session_id:
            log_entry["session_id"] = session_id

        # Add any extra fields passed to the logger
        if hasattr(record, "extra_fields"):
            log_entry["extra"] = record.extra_fields

        # Add exception info if present
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)

        # Add source location for errors
        if record.levelno >= logging.ERROR:
            log_entry["location"] = {"file": record.pathname, "line": record.lineno, "function": record.funcName}

        return json.dumps(log_entry)


class DevelopmentFormatter(logging.Formatter):
    """
    Human-readable formatter for local development.

    Output format:
    2025-01-15 10:30:00 | INFO | app.main | [req-abc123] Chat request processed
    """

    COLORS = {
        "DEBUG": "\033[36m",  # Cyan
        "INFO": "\033[32m",  # Green
        "WARNING": "\033[33m",  # Yellow
        "ERROR": "\033[31m",  # Red
        "CRITICAL": "\033[35m",  # Magenta
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        # Timestamp
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Color based on level
        color = self.COLORS.get(record.levelname, "")
        level = f"{color}{record.levelname:8}{self.RESET}"

        # Request ID if available
        request_id = request_id_var.get()
        req_str = f"[{request_id[:8]}] " if request_id else ""

        # User ID if available
        user_id = user_id_var.get()
        user_str = f"(user:{user_id}) " if user_id else ""

        # Format message
        message = record.getMessage()

        # Base format
        formatted = f"{timestamp} | {level} | {record.name:20} | {req_str}{user_str}{message}"

        # Add exception if present
        if record.exc_info:
            formatted += "\n" + self.formatException(record.exc_info)

        return formatted


# =============================================================================
# CONTEXT-AWARE LOGGER
# =============================================================================


class ContextLogger(logging.LoggerAdapter):
    """
    Logger adapter that automatically includes extra context.

    Usage:
        logger = get_logger(__name__)
        logger.info("Processing order", order_id="ORD-12345", amount=99.99)
    """

    def process(self, msg: str, kwargs: Dict[str, Any]) -> tuple:
        # Extract extra fields from kwargs
        extra_fields = {}
        standard_keys = {"exc_info", "stack_info", "stacklevel", "extra"}

        for key in list(kwargs.keys()):
            if key not in standard_keys:
                extra_fields[key] = kwargs.pop(key)

        # Attach extra fields to the record
        if extra_fields:
            kwargs.setdefault("extra", {})["extra_fields"] = extra_fields

        return msg, kwargs


# =============================================================================
# SETUP FUNCTIONS
# =============================================================================


def setup_logging(level: str = "INFO", json_format: bool = True, service_name: str = "customer-support-api") -> None:
    """
    Configure logging for the application.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        json_format: Use JSON format (True for production, False for development)
        service_name: Name of the service for log identification
    """
    # Get root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, level.upper()))

    # Remove existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # Create console handler
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(getattr(logging, level.upper()))

    # Set formatter based on environment
    if json_format:
        formatter = StructuredFormatter()
    else:
        formatter = DevelopmentFormatter()

    handler.setFormatter(formatter)
    root_logger.addHandler(handler)

    # Set levels for noisy libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("google").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    # Log startup
    logger = get_logger(__name__)
    logger.info(
        "Logging configured", service=service_name, log_level=level, format="json" if json_format else "development"
    )


def get_logger(name: str) -> ContextLogger:
    """
    Get a context-aware logger for the given module.

    Args:
        name: Logger name (typically __name__)

    Returns:
        ContextLogger instance
    """
    return ContextLogger(logging.getLogger(name), {})


# =============================================================================
# CONTEXT MANAGEMENT
# =============================================================================


def set_request_context(
    request_id: Optional[str] = None, user_id: Optional[str] = None, session_id: Optional[str] = None
) -> str:
    """
    Set request context for logging.

    Args:
        request_id: Unique request identifier (generated if not provided)
        user_id: User ID if authenticated
        session_id: Session ID if available

    Returns:
        The request_id being used
    """
    req_id = request_id or str(uuid.uuid4())
    request_id_var.set(req_id)

    if user_id:
        user_id_var.set(user_id)

    if session_id:
        session_id_var.set(session_id)

    return req_id


def clear_request_context() -> None:
    """Clear all request context variables."""
    request_id_var.set(None)
    user_id_var.set(None)
    session_id_var.set(None)


def get_request_id() -> Optional[str]:
    """Get the current request ID."""
    return request_id_var.get()


# =============================================================================
# FASTAPI MIDDLEWARE
# =============================================================================


async def logging_middleware(request, call_next):
    """
    FastAPI middleware that sets up request context for logging.

    Usage:
        app.middleware("http")(logging_middleware)
    """
    # Generate or extract request ID
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())

    # Set context
    set_request_context(request_id=request_id)

    # Get logger
    logger = get_logger("middleware")

    # Log request start
    logger.info(
        "Request started",
        method=request.method,
        path=request.url.path,
        client_ip=request.client.host if request.client else "unknown",
    )

    try:
        # Process request
        response = await call_next(request)

        # Log request completion
        logger.info("Request completed", method=request.method, path=request.url.path, status_code=response.status_code)

        # Add request ID to response headers
        response.headers["X-Request-ID"] = request_id

        return response

    except Exception as e:
        logger.error("Request failed", method=request.method, path=request.url.path, error=str(e))
        raise
    finally:
        clear_request_context()


# =============================================================================
# DECORATOR FOR FUNCTION LOGGING
# =============================================================================


def log_function_call(logger: Optional[ContextLogger] = None):
    """
    Decorator to log function entry and exit.

    Usage:
        @log_function_call()
        def process_refund(order_id: str):
            ...
    """

    def decorator(func):
        nonlocal logger
        if logger is None:
            logger = get_logger(func.__module__)

        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            func_name = func.__name__
            logger.debug(f"Entering {func_name}", args=str(args)[:100], kwargs=str(kwargs)[:100])
            try:
                result = await func(*args, **kwargs)
                logger.debug(f"Exiting {func_name}", success=True)
                return result
            except Exception as e:
                logger.error(f"Error in {func_name}", error=str(e))
                raise

        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            func_name = func.__name__
            logger.debug(f"Entering {func_name}", args=str(args)[:100], kwargs=str(kwargs)[:100])
            try:
                result = func(*args, **kwargs)
                logger.debug(f"Exiting {func_name}", success=True)
                return result
            except Exception as e:
                logger.error(f"Error in {func_name}", error=str(e))
                raise

        import asyncio

        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper

    return decorator
