import logging
import os
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import auth
from .agent_client import agent_client
from .config import settings
from .database import get_database
from .health import HealthChecker, HealthStatus, liveness_check, readiness_check
from .logging_config import get_logger, logging_middleware, set_request_context, setup_logging
from .metrics import increment_chat_errors, increment_chat_requests, metrics, metrics_middleware
from .models import (
    AnonymousUserResponse,
    AuthResponse,
    ChatRequest,
    ChatResponse,
    LoginRequest,
    MessageHistoryResponse,
    MessageInfo,
    RegisterRequest,
    RenameSessionRequest,
    SessionListResponse,
)
from .rate_limiter import RateLimitDependency

# Initialize structured logging
# Use JSON format in production, human-readable in development
is_production = os.getenv("ENVIRONMENT", "development") == "production"
setup_logging(level=os.getenv("LOG_LEVEL", "INFO"), json_format=is_production, service_name="customer-support-api")
logger = get_logger(__name__)

# Model Armor (optional — only initialized when enabled)
_MODEL_ARMOR_ENABLED = os.getenv("MODEL_ARMOR_ENABLED", "false").lower() == "true"
_MODEL_ARMOR_TEMPLATE_ID = os.getenv("MODEL_ARMOR_TEMPLATE_ID", "")
_MODEL_ARMOR_MODE = os.getenv("MODEL_ARMOR_MODE", "INSPECT_AND_BLOCK").upper()
_model_armor_client = None
_modelarmor_v1 = None
_parse_ma_response = None
if _MODEL_ARMOR_ENABLED and _MODEL_ARMOR_TEMPLATE_ID:
    try:
        from google.api_core.client_options import ClientOptions as _ClientOptions
        from google.cloud import modelarmor_v1 as _modelarmor_v1

        from app.safety_util import parse_model_armor_response as _parse_ma_response

        _location = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
        _model_armor_client = _modelarmor_v1.ModelArmorClient(
            client_options=_ClientOptions(api_endpoint=f"modelarmor.{_location}.rep.googleapis.com")
        )
    except Exception as _ma_init_err:
        logging.getLogger(__name__).warning("Model Armor init failed: %s", _ma_init_err)

# Initialize database
db = get_database(project_id=settings.google_cloud_project, database_id="customer-support-db")

# Initialize health checker (agent_client added after import)
health_checker = HealthChecker(db=db, agent_client=None)

app = FastAPI(
    title="Customer Support AI Backend",
    description="Backend API for Customer Support Multi-Agent System with User Management",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_url, "http://localhost:3000", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Add logging middleware for request context
app.middleware("http")(logging_middleware)

# Add metrics middleware for request tracking
app.middleware("http")(metrics_middleware)


# =============================================================================
# APPLICATION LIFECYCLE EVENTS
# =============================================================================


@app.on_event("startup")
async def startup_event():
    """
    Application startup handler.

    Initializes resources and logs startup information.
    """
    logger.info(
        "Application starting up",
        project=settings.google_cloud_project,
        location=settings.google_cloud_location,
        version="2.0.0",
    )

    # Set initial metrics
    metrics.set_gauge("app_info", 1)


@app.on_event("shutdown")
async def shutdown_event():
    """
    Graceful shutdown handler.

    Ensures clean shutdown of resources:
    - Logs shutdown event
    - Allows in-flight requests to complete (handled by uvicorn)
    - Could close database connections if needed
    """
    logger.info("Application shutting down - starting graceful shutdown")

    # Log final metrics before shutdown
    final_metrics = metrics.get_all_metrics()
    logger.info(
        "Final metrics before shutdown",
        total_requests=final_metrics["summary"]["total_requests"],
        total_errors=final_metrics["summary"]["total_errors"],
        uptime_seconds=final_metrics["uptime_seconds"],
    )

    # Note: Uvicorn handles waiting for in-flight requests by default
    # with its --timeout-graceful-shutdown option (default 30s)

    logger.info("Graceful shutdown complete")


# =============================================================================
# AUTHENTICATION DEPENDENCY
# =============================================================================


def get_current_user(authorization: Optional[str] = Header(None)) -> Optional[str]:
    """
    Extract user_id from Authorization header.

    Returns:
        user_id if authenticated, None if anonymous
    """
    if not authorization:
        return None

    # Expected format: "Bearer <token>"
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid authorization header")

    token = parts[1]
    user_id = db.verify_token(token)

    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    return user_id


@app.get("/health")
async def health_check():
    """
    Comprehensive health check endpoint.

    Checks connectivity to:
    - Database (Firestore)
    - Agent Engine

    Returns:
        - 200: All systems healthy
        - 503: System degraded or unhealthy
    """
    from fastapi.responses import JSONResponse

    # Update health checker with agent client if available
    health_checker.agent_client = agent_client

    result = await health_checker.check_all()

    # Return 503 if unhealthy
    status_code = 200 if result.status != HealthStatus.UNHEALTHY else 503

    return JSONResponse(content=result.to_dict(), status_code=status_code)


@app.get("/health/live")
async def liveness_probe():
    """
    Kubernetes liveness probe.

    Returns 200 if the process is running.
    Used to determine if pod should be restarted.
    """
    return await liveness_check()


@app.get("/health/ready")
async def readiness_probe():
    """
    Kubernetes readiness probe.

    Returns 200 if service can handle requests.
    Used to determine if pod should receive traffic.
    """
    from fastapi.responses import JSONResponse

    health_checker.agent_client = agent_client
    result = await readiness_check(health_checker)

    status_code = 200 if result["ready"] else 503

    return JSONResponse(content=result, status_code=status_code)


# =============================================================================
# METRICS ENDPOINTS
# =============================================================================


@app.get("/metrics")
async def get_metrics():
    """
    Get application metrics in JSON format.

    Returns request counts, latencies, error rates by endpoint.
    """
    return metrics.get_all_metrics()


@app.get("/metrics/prometheus")
async def get_prometheus_metrics():
    """
    Get application metrics in Prometheus format.

    Use this endpoint for Prometheus scraping.
    """
    from fastapi.responses import PlainTextResponse

    return PlainTextResponse(content=metrics.get_prometheus_format(), media_type="text/plain")


# =============================================================================
# AUTHENTICATION ENDPOINTS
# =============================================================================


@app.post("/api/auth/register", response_model=AuthResponse)
async def register(request: RegisterRequest, _rate_check: bool = Depends(RateLimitDependency("auth"))):
    """Register a new user account."""
    try:
        # Hash password and create user
        # Note: create_user() handles demo email validation and duplicate check
        password_hash = auth.hash_password(request.password)
        user_id = db.create_user(email=request.email, name=request.name, password_hash=password_hash)

        # Generate auth token
        token = db.create_token(user_id)

        logger.info("User registered", user_id=user_id, email=request.email)

        return AuthResponse(user_id=user_id, token=token, name=request.name, email=request.email)

    except ValueError as e:
        # Handle demo email registration attempt or duplicate email
        logger.warning("Registration rejected", reason=str(e), email=request.email)
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Registration error", error=str(e), email=request.email)
        raise HTTPException(status_code=500, detail="Registration failed")


@app.post("/api/auth/login", response_model=AuthResponse)
async def login(request: LoginRequest, _rate_check: bool = Depends(RateLimitDependency("auth"))):
    """Login with email and password."""
    try:
        # Get user by email
        user = db.get_user_by_email(request.email)
        if not user:
            raise HTTPException(status_code=401, detail="Invalid email or password")

        # Verify password
        if not auth.verify_password(request.password, user["password_hash"]):
            raise HTTPException(status_code=401, detail="Invalid email or password")

        # Update last login
        db.update_last_login(user["user_id"])

        # Generate auth token
        token = db.create_token(user["user_id"])

        logger.info("User logged in", user_id=user["user_id"], email=request.email)

        return AuthResponse(user_id=user["user_id"], token=token, name=user["name"], email=user["email"])

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Login error", error=str(e), email=request.email)
        raise HTTPException(status_code=500, detail="Login failed")


@app.post("/api/auth/anonymous", response_model=AnonymousUserResponse)
async def create_anonymous(_rate_check: bool = Depends(RateLimitDependency("auth"))):
    """Create an anonymous user (for users who don't want to register)."""
    try:
        user_id = db.create_anonymous_user()

        return AnonymousUserResponse(user_id=user_id, is_anonymous=True)

    except Exception as e:
        logger.error("Anonymous user creation error", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to create anonymous user")


@app.post("/api/auth/logout")
async def logout(authorization: str = Header(...)):
    """Logout (revoke token)."""
    try:
        # Extract token
        parts = authorization.split()
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token = parts[1]
            db.revoke_token(token)
            return {"status": "logged_out"}

        raise HTTPException(status_code=400, detail="Invalid authorization header")

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Logout error", error=str(e))
        raise HTTPException(status_code=500, detail="Logout failed")


# =============================================================================
# CHAT ENDPOINT
# =============================================================================


@app.post("/api/chat", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    user_id: Optional[str] = Depends(get_current_user),
    x_user_id: Optional[str] = Header(None),
    _rate_check: bool = Depends(RateLimitDependency("chat")),
):
    """
    Send a message to the customer support agent.

    Supports both:
    - Authenticated users (via Authorization: Bearer token header)
    - Anonymous users (via X-User-Id header with anon-* user_id)

    Args:
        request: ChatRequest with message and optional session_id
        user_id: Extracted from Authorization header (if authenticated)
        x_user_id: Extracted from X-User-Id header (if anonymous)

    Returns:
        ChatResponse with agent response, user_id, and session_id
    """
    try:
        # Determine user_id (auth takes precedence over anonymous)
        actual_user_id = user_id or x_user_id

        if not actual_user_id:
            raise HTTPException(
                status_code=401,
                detail="Authentication required. Use Authorization header or X-User-Id for anonymous users.",
            )

        # Set user context for logging
        set_request_context(user_id=actual_user_id, session_id=request.session_id)
        logger.info("Chat request received", message_preview=request.message[:50])

        # Track chat request metric
        increment_chat_requests()

        # Check if this is a new session or existing one
        if request.session_id:
            # Verify session belongs to user
            session = db.get_session(request.session_id)
            if not session:
                raise HTTPException(status_code=404, detail="Session not found")
            if session["user_id"] != actual_user_id:
                raise HTTPException(status_code=403, detail="Session does not belong to user")

            internal_session_id = request.session_id
            agent_engine_session_id = session["agent_engine_session_id"]

            logger.info("Using existing session", session_id=internal_session_id)
        else:
            # Create new session
            internal_session_id = None
            agent_engine_session_id = None
            logger.info("Creating new session")

        # Model Armor safety check — screen user prompt before sending to agent
        if _model_armor_client and _MODEL_ARMOR_TEMPLATE_ID:
            try:
                ma_response = _model_armor_client.sanitize_user_prompt(
                    request=_modelarmor_v1.SanitizeUserPromptRequest(
                        name=_MODEL_ARMOR_TEMPLATE_ID,
                        user_prompt_data=_modelarmor_v1.DataItem(text=request.message),
                    )
                )
                violations = _parse_ma_response(ma_response)
                if violations:
                    if _MODEL_ARMOR_MODE == "INSPECT_AND_BLOCK":
                        logger.warning("Model Armor blocked user prompt", violations=str(violations))
                        raise HTTPException(
                            status_code=400,
                            detail="I'm sorry, I can't process this request as it violates our safety policy. Please contact support if you need assistance.",
                        )
                    else:
                        logger.info(
                            "Model Armor flagged user prompt (INSPECT_ONLY — not blocked)", violations=str(violations)
                        )
            except HTTPException:
                raise
            except Exception as ma_err:
                logger.error("Model Armor check error (failing open)", error=str(ma_err))

        # Query the agent
        response_text, agent_engine_session_id = await agent_client.query_agent(
            user_id=actual_user_id, agent_engine_session_id=agent_engine_session_id, message=request.message
        )

        # If new session, create it in database
        if not internal_session_id:
            internal_session_id = db.create_session(
                user_id=actual_user_id, agent_engine_session_id=agent_engine_session_id
            )
            logger.info("Created new session", session_id=internal_session_id)
        else:
            # Update existing session
            db.update_session(internal_session_id)

        # Save messages to database for UI display
        db.save_message(internal_session_id, "user", request.message)
        db.save_message(internal_session_id, "assistant", response_text)

        return ChatResponse(response=response_text, user_id=actual_user_id, session_id=internal_session_id)

    except HTTPException:
        increment_chat_errors()
        raise
    except TimeoutError as e:
        increment_chat_errors()
        logger.warning("Chat request timed out", error=str(e))
        raise HTTPException(status_code=504, detail=str(e))
    except Exception as e:
        increment_chat_errors()
        logger.error("Error processing chat request", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error processing request: {str(e)}")


# =============================================================================
# SESSION MANAGEMENT ENDPOINTS
# =============================================================================


@app.get("/api/sessions", response_model=SessionListResponse)
async def list_sessions(
    user_id: Optional[str] = Depends(get_current_user),
    x_user_id: Optional[str] = Header(None),
    _rate_check: bool = Depends(RateLimitDependency("sessions")),
):
    """Get all sessions for the current user."""
    try:
        actual_user_id = user_id or x_user_id

        if not actual_user_id:
            raise HTTPException(status_code=401, detail="Authentication required")

        sessions = db.get_user_sessions(actual_user_id)

        from .models import SessionInfo

        session_list = [SessionInfo(**session) for session in sessions]

        return SessionListResponse(user_id=actual_user_id, sessions=session_list)

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error listing sessions", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to list sessions")


@app.put("/api/sessions/{session_id}/rename")
async def rename_session(
    session_id: str,
    request: RenameSessionRequest,
    user_id: Optional[str] = Depends(get_current_user),
    x_user_id: Optional[str] = Header(None),
    _rate_check: bool = Depends(RateLimitDependency("sessions")),
):
    """Rename a session."""
    try:
        actual_user_id = user_id or x_user_id

        if not actual_user_id:
            raise HTTPException(status_code=401, detail="Authentication required")

        # Verify session belongs to user
        session = db.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        if session["user_id"] != actual_user_id:
            raise HTTPException(status_code=403, detail="Session does not belong to user")

        db.rename_session(session_id, request.session_name)

        return {"status": "success", "session_id": session_id}

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error renaming session", error=str(e), session_id=session_id)
        raise HTTPException(status_code=500, detail="Failed to rename session")


@app.delete("/api/sessions/{session_id}")
async def delete_session(
    session_id: str,
    user_id: Optional[str] = Depends(get_current_user),
    x_user_id: Optional[str] = Header(None),
    _rate_check: bool = Depends(RateLimitDependency("sessions")),
):
    """Delete a session."""
    try:
        actual_user_id = user_id or x_user_id

        if not actual_user_id:
            raise HTTPException(status_code=401, detail="Authentication required")

        # Verify session belongs to user
        session = db.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        if session["user_id"] != actual_user_id:
            raise HTTPException(status_code=403, detail="Session does not belong to user")

        db.delete_session(session_id)

        return {"status": "deleted", "session_id": session_id}

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error deleting session", error=str(e), session_id=session_id)
        raise HTTPException(status_code=500, detail="Failed to delete session")


@app.get("/api/sessions/{session_id}/messages", response_model=MessageHistoryResponse)
async def get_session_messages(
    session_id: str,
    user_id: Optional[str] = Depends(get_current_user),
    x_user_id: Optional[str] = Header(None),
    _rate_check: bool = Depends(RateLimitDependency("sessions")),
):
    """Get message history for a session."""
    try:
        actual_user_id = user_id or x_user_id

        if not actual_user_id:
            raise HTTPException(status_code=401, detail="Authentication required")

        # Verify session belongs to user
        session = db.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        if session["user_id"] != actual_user_id:
            raise HTTPException(status_code=403, detail="Session does not belong to user")

        # Get messages
        messages = db.get_session_messages(session_id)

        message_list = [MessageInfo(**msg) for msg in messages]

        return MessageHistoryResponse(session_id=session_id, messages=message_list)

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error fetching messages", error=str(e), session_id=session_id)
        raise HTTPException(status_code=500, detail="Failed to fetch messages")


@app.get("/api")
async def api_root():
    """API root endpoint"""
    return {
        "message": "Customer Support AI Backend v2.0",
        "docs": "/docs",
        "health": "/health",
        "endpoints": {
            "auth": {
                "register": "POST /api/auth/register",
                "login": "POST /api/auth/login",
                "anonymous": "POST /api/auth/anonymous",
                "logout": "POST /api/auth/logout",
            },
            "chat": "POST /api/chat",
            "sessions": {
                "list": "GET /api/sessions",
                "rename": "PUT /api/sessions/{id}/rename",
                "delete": "DELETE /api/sessions/{id}",
            },
        },
    }


# Serve static frontend files
static_dir = Path(__file__).parent.parent / "static"
if static_dir.exists():
    app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="assets")

    @app.get("/")
    async def serve_frontend():
        """Serve the React frontend"""
        index_file = static_dir / "index.html"
        if index_file.exists():
            return FileResponse(index_file)
        return {"message": "Frontend not found. Build the frontend first."}

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        """Serve SPA - return index.html for all non-API routes"""
        if full_path.startswith("api/") or full_path.startswith("docs") or full_path.startswith("openapi.json"):
            raise HTTPException(status_code=404, detail="Not found")

        file_path = static_dir / full_path
        if file_path.is_file():
            return FileResponse(file_path)

        index_file = static_dir / "index.html"
        if index_file.exists():
            return FileResponse(index_file)
        raise HTTPException(status_code=404, detail="Not found")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=settings.port)
