import asyncio
import threading
import time
from enum import Enum
from typing import Optional

import vertexai
from google.api_core import exceptions, retry
from vertexai import agent_engines

from .config import settings
from .logging_config import get_logger

logger = get_logger(__name__)

# Timeout configuration
DEFAULT_QUERY_TIMEOUT_SECONDS = 120  # 2 minutes for agent queries
SESSION_CREATE_TIMEOUT_SECONDS = 30  # 30 seconds for session creation

# Configure retry policy for Agent Engine calls
# Handles transient errors with exponential backoff
AGENT_RETRY_POLICY = retry.Retry(
    initial=1.0,  # Initial delay: 1 second
    maximum=60.0,  # Maximum delay: 60 seconds
    multiplier=2.0,  # Exponential backoff multiplier
    deadline=180.0,  # Total deadline: 3 minutes
    predicate=retry.if_exception_type(
        exceptions.ResourceExhausted,  # 429 Rate Limit
        exceptions.ServiceUnavailable,  # 503 Service Unavailable
        exceptions.DeadlineExceeded,  # 504 Gateway Timeout
        exceptions.InternalServerError,  # 500 Internal Server Error
        exceptions.TooManyRequests,  # 429 Too Many Requests
        exceptions.FailedPrecondition,  # Agent Engine uses this for "Rate exceeded"
    ),
)


class _CircuitState(Enum):
    CLOSED = "closed"  # Normal operation — requests flow through
    OPEN = "open"  # Failing fast — requests rejected immediately
    HALF_OPEN = "half_open"  # Probing recovery — one request let through


class CircuitBreaker:
    """
    Simple thread-safe circuit breaker for Agent Engine calls.

    States:
      CLOSED   → normal operation; failures are counted.
      OPEN     → fast-fail; no requests reach Agent Engine.
                 Transitions to HALF_OPEN after recovery_timeout seconds.
      HALF_OPEN → one probe request is allowed through.
                  Success → CLOSED; failure → OPEN (reset timer).

    This prevents cascading timeouts when Agent Engine is unavailable:
    instead of each request burning the full 3-minute retry budget, the
    circuit opens after 5 consecutive failures and fails immediately for
    60 seconds before probing again.
    """

    def __init__(self, failure_threshold: int = 5, recovery_timeout: float = 60.0):
        self._state = _CircuitState.CLOSED
        self._failure_count = 0
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._last_failure_time: float = 0.0
        self._lock = threading.Lock()

    @property
    def state(self) -> _CircuitState:
        with self._lock:
            if self._state == _CircuitState.OPEN:
                elapsed = time.monotonic() - self._last_failure_time
                if elapsed >= self._recovery_timeout:
                    self._state = _CircuitState.HALF_OPEN
                    logger.info("Circuit breaker → HALF_OPEN (probing recovery)")
            return self._state

    def is_open(self) -> bool:
        return self.state == _CircuitState.OPEN

    def record_success(self):
        with self._lock:
            if self._state != _CircuitState.CLOSED:
                logger.info("Circuit breaker → CLOSED (service recovered)")
            self._state = _CircuitState.CLOSED
            self._failure_count = 0

    def record_failure(self):
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()
            if self._failure_count >= self._failure_threshold and self._state != _CircuitState.OPEN:
                logger.warning(
                    "Circuit breaker → OPEN",
                    consecutive_failures=self._failure_count,
                    recovery_in_seconds=self._recovery_timeout,
                )
            self._state = _CircuitState.OPEN


# Module-level circuit breaker shared across all requests
_circuit_breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=60.0)


class AgentEngineClient:
    def __init__(self):
        """Initialize the Agent Engine client (lazy — no network calls at import time)."""
        vertexai.init(
            project=settings.google_cloud_project,
            location=settings.google_cloud_location,
        )
        self.resource_name = settings.agent_engine_resource_name
        self._remote_app = None

    def _get_remote_app(self):
        """Lazily connect to Agent Engine on first use."""
        if self._remote_app is None:
            try:
                self._remote_app = agent_engines.get(self.resource_name)
                self.agent_engine_app = self._remote_app  # Alias for health checks
                logger.info("Connected to Agent Engine", resource_name=self.resource_name)
            except Exception as e:
                logger.error("Failed to connect to Agent Engine", error=str(e))
                raise
        return self._remote_app

    @property
    def remote_app(self):
        return self._get_remote_app()

    async def query_agent(
        self,
        user_id: str,
        agent_engine_session_id: Optional[str],
        message: str,
        timeout_seconds: float = DEFAULT_QUERY_TIMEOUT_SECONDS,
    ) -> tuple[str, str]:
        """
        Query the deployed Agent Engine using async_stream_query with retry logic.

        Implements automatic retry with exponential backoff for transient errors:
        - Rate limiting (429)
        - Service unavailability (503)
        - Gateway timeouts (504)
        - Internal server errors (500)

        Architecture:
        - user_id: Identifies the user (from auth or anonymous)
        - agent_engine_session_id: Agent Engine's session ID for this conversation
        - If agent_engine_session_id is None, creates a new session on Agent Engine

        Args:
            user_id: User ID (from auth or anonymous)
            agent_engine_session_id: Agent Engine session ID (None for new session)
            message: User message
            timeout_seconds: Maximum time to wait for response (default: 120s)

        Returns:
            Tuple of (response_text, agent_engine_session_id)

        Raises:
            TimeoutError: If the operation exceeds timeout_seconds
            Exception: For other failures
        """
        # Fail fast if the circuit is open (Agent Engine repeatedly unavailable)
        if _circuit_breaker.is_open():
            logger.warning("Circuit breaker OPEN — rejecting request without hitting Agent Engine")
            raise Exception("The agent service is temporarily unavailable. Please try again in a moment.")

        try:
            async with asyncio.timeout(timeout_seconds):
                # Check if we need to create a new session on Agent Engine
                if not agent_engine_session_id:
                    logger.info("Creating new Agent Engine session", user_id=user_id)

                    # Create session on Agent Engine with retry logic
                    @AGENT_RETRY_POLICY
                    async def _create_session():
                        async with asyncio.timeout(SESSION_CREATE_TIMEOUT_SECONDS):
                            return await self.remote_app.async_create_session(user_id=user_id)

                    try:
                        remote_session = await _create_session()
                    except asyncio.TimeoutError:
                        logger.error("Session creation timed out", user_id=user_id)
                        raise TimeoutError("Session creation timed out. Please try again.")
                    except Exception as e:
                        logger.error("Failed to create session after retries", error=str(e))
                        raise Exception("Unable to create session: Service temporarily unavailable")

                    # Extract the actual session_id from Agent Engine's response
                    agent_engine_session_id = remote_session["id"]

                    logger.info("Agent Engine session created", session_id=agent_engine_session_id, user_id=user_id)
                else:
                    logger.info(
                        "Using existing Agent Engine session", session_id=agent_engine_session_id, user_id=user_id
                    )

                logger.info("Querying agent", message_preview=message[:50])

                # Stream the query to the agent using Agent Engine's session
                response_text = ""
                event_count = 0

                async for event in self.remote_app.async_stream_query(
                    user_id=user_id, session_id=agent_engine_session_id, message=message
                ):
                    event_count += 1
                    logger.debug(
                        "Received event",
                        event_num=event_count,
                        author=event.get("author", "unknown") if isinstance(event, dict) else "unknown",
                    )

                    # Extract text from event
                    if isinstance(event, dict):
                        content = event.get("content", event.get("parts", {}))

                        if isinstance(content, dict):
                            parts = content.get("parts", [])

                            for part in parts:
                                if isinstance(part, dict):
                                    if part.get("text"):
                                        response_text += part["text"]
                                    elif part.get("function_call"):
                                        fn = part["function_call"]
                                        logger.debug("Tool call", tool=fn.get("name"), args=fn.get("args", {}))

                        # Also check for direct text field
                        if "text" in event:
                            response_text += event["text"]

                logger.info("Query processing complete", event_count=event_count, response_length=len(response_text))

                if not response_text:
                    logger.error("No response text extracted", event_count=event_count)
                    response_text = "I apologize, but I didn't receive a response. Please try again."

                _circuit_breaker.record_success()
                # Return agent_engine_session_id so it can be tracked in our database
                return response_text, agent_engine_session_id

        except asyncio.TimeoutError:
            _circuit_breaker.record_failure()
            logger.error("Agent query timed out", timeout_seconds=timeout_seconds, user_id=user_id)
            raise TimeoutError(
                f"Request timed out after {timeout_seconds} seconds. " "The system is busy. Please try again."
            )
        except TimeoutError:
            # Re-raise TimeoutError from session creation (already counted above)
            raise
        except Exception as e:
            error_str = str(e)
            # Rate limit errors (Agent Engine returns FAILED_PRECONDITION with "Rate exceeded")
            # are transient quota exhaustion — don't trip the circuit breaker.
            if "Rate exceeded" in error_str or "rate exceeded" in error_str.lower():
                logger.warning("Agent Engine rate limit hit — not tripping circuit breaker", error=error_str[:200])
                raise Exception("The service is currently rate limited. Please wait a moment and try again.")
            _circuit_breaker.record_failure()
            logger.error("Error querying agent", error=str(e), exc_info=True)
            raise Exception(f"Failed to query agent: {str(e)}")


agent_client = AgentEngineClient()
