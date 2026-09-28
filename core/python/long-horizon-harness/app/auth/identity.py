# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Identity resolution for incoming HTTP requests.

Three modes, selected by ``LHA_AUTH_MODE``:

* ``dev`` (default) — the user_id is ``LHA_DEV_USER_ID`` (default
  ``dev@local``). For local development; no headers required.
* ``iap`` — the request carries an IAP JWT, verified against ``LHA_IAP_AUDIENCE``.
  The web proxy forwards it under ``X-LHA-IAP-Assertion`` (Google strips the native
  ``X-Goog-IAP-JWT-Assertion`` inbound to a non-IAP backend); a backend behind IAP
  directly would carry the native header. With no IAP JWT, agent-to-agent calls may
  instead present a Google OAuth access token in ``Authorization: Bearer`` (Gemini
  Enterprise), verified via tokeninfo with ``aud == LHA_GCP_OAUTH_CLIENT_ID``. The
  verified ``email`` becomes the user_id.
* ``trusted_header`` — the user_id is read verbatim from
  ``X-LHA-User-Id``. The header is trusted because Cloud Run IAM gates
  who can invoke the backend (only the web-frontend SA), and the
  frontend validates IAP before forwarding. Used when the backend sits
  behind a separate web service that handles browser auth.

Routes consume the resolved id via ``request.state.user_id`` (populated by
``IdentityMiddleware``) or by calling ``current_user_id(request)``.
"""

from __future__ import annotations

import contextlib
import contextvars
import enum
import logging
import os
from collections.abc import Awaitable, Callable, Iterator

from fastapi import HTTPException, Request, Response, status
from google.auth.transport import requests as google_auth_requests
from google.oauth2 import id_token
from starlette.middleware.base import BaseHTTPMiddleware

from app.infrastructure.env import env_str

logger = logging.getLogger(__name__)

# Carries the authenticated user_id through async call chains (A2A converters,
# OAuth callbacks) that don't receive the FastAPI Request directly.
_user_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "lha_user_id", default=None
)


def get_user_id_from_context() -> str | None:
    """Return the user_id stashed by ``IdentityMiddleware`` for this request,
    or None if called outside an authenticated request scope.
    """
    return _user_id_var.get()


@contextlib.contextmanager
def user_identity_scope(user_id: str) -> Iterator[None]:
    """Run a block as ``user_id`` for code that reads the identity ContextVar.

    Used by background jobs (the scheduler tick) that drive the A2A handler
    outside an HTTP request, so ``request_converter`` keys the run under
    the right user instead of ADK's synthetic ``A2A_USER_*`` fallback.
    """
    token = _user_id_var.set(user_id)
    try:
        yield
    finally:
        _user_id_var.reset(token)


_IAP_JWT_HEADER = "X-Goog-IAP-JWT-Assertion"
# The web proxy forwards the (already-verified) IAP JWT under this custom header:
# Google strips X-Goog-* headers inbound to a non-IAP backend, so the native one
# never arrives. We re-verify it here regardless (signature + audience).
_FORWARDED_IAP_JWT_HEADER = "X-LHA-IAP-Assertion"
_IAP_PUBLIC_KEYS_URL = "https://www.gstatic.com/iap/verify/public_key"
_TRUSTED_USER_ID_HEADER = "X-LHA-User-Id"


class AuthMode(enum.StrEnum):
    DEV = "dev"
    IAP = "iap"
    TRUSTED_HEADER = "trusted_header"


class UnknownAuthModeError(RuntimeError):
    """Raised at request time if LHA_AUTH_MODE is set to an unknown value."""


class DevAuthInProductionError(RuntimeError):
    """Raised when LHA_AUTH_MODE resolves to dev while running on Cloud Run."""


def get_auth_mode() -> AuthMode:
    raw = os.environ.get("LHA_AUTH_MODE", AuthMode.DEV.value).strip().lower()
    try:
        mode = AuthMode(raw)
    except ValueError as err:
        raise UnknownAuthModeError(
            f"LHA_AUTH_MODE={raw!r} is not one of {[m.value for m in AuthMode.__members__.values()]}"
        ) from err
    # K_SERVICE is injected by Cloud Run. `dev` mode collapses every request to
    # one identity with no auth — safe locally, a full cross-user breach if a
    # deploy forgets to set the mode. Fail closed instead of silently open.
    if mode is AuthMode.DEV and os.environ.get("K_SERVICE"):
        raise DevAuthInProductionError(
            "LHA_AUTH_MODE=dev refused: K_SERVICE is set (running on Cloud Run). "
            "Set LHA_AUTH_MODE=iap or trusted_header for deployed environments."
        )
    return mode


def _dev_user_id() -> str:
    return env_str("LHA_DEV_USER_ID", "dev@local").strip() or "dev@local"


def _iap_audience() -> str:
    audience = os.environ.get("LHA_IAP_AUDIENCE", "").strip()
    if not audience:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="LHA_IAP_AUDIENCE is not configured",
        )
    return audience


# Module-level request adapter is reused so we don't rebuild a new HTTP
# session per request (verify_token caches the JWKS internally too).
_GOOGLE_AUTH_REQUEST = google_auth_requests.Request()


def _verify_iap_jwt(jwt: str) -> dict[str, object]:
    try:
        claims = id_token.verify_token(
            jwt,
            request=_GOOGLE_AUTH_REQUEST,
            audience=_iap_audience(),
            certs_url=_IAP_PUBLIC_KEYS_URL,
        )
    except HTTPException:
        raise
    except Exception as err:
        # Log at info; failed JWTs are expected for unauthenticated probes.
        logger.info("IAP JWT verification failed: %s", err)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid IAP JWT",
        ) from err
    if not isinstance(claims, dict):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="IAP JWT decoded to non-dict claims",
        )
    return claims


def _extract_bearer(request: Request) -> str:
    raw = request.headers.get("Authorization", "")
    if raw[:7].lower() == "bearer ":
        return raw[7:].strip()
    return ""


def _oauth_client_id() -> str:
    return os.environ.get("LHA_GCP_OAUTH_CLIENT_ID", "").strip()


def _resolve_oauth_bearer(token: str) -> str:
    """Verify a GE-forwarded end-user access token; return its email.

    Side effect: stashes the token for this turn so secret_env() can overlay it
    as CLOUDSDK_AUTH_ACCESS_TOKEN (delegated Google access).
    """
    from app.auth import oauth_verify
    from app.secrets.inject import set_delegated_google_token

    client_id = _oauth_client_id()
    if not client_id:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="LHA_GCP_OAUTH_CLIENT_ID is not configured",
        )
    try:
        email = oauth_verify.verify_google_access_token(
            token, expected_aud=client_id
        )
    except oauth_verify.OAuthVerifyError as err:
        logger.info("OAuth bearer verification failed: %s", err)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid OAuth bearer",
        ) from err
    # Request-scoped, like _user_id_var: the ASGI server runs each request in its
    # own task, so this ContextVar does not leak across requests — no reset needed.
    set_delegated_google_token(token)
    return email


def resolve_user_id(request: Request) -> str:
    """Return the authenticated user_id for ``request`` or raise 401/500.

    Idempotent: subsequent calls return the same id without re-verifying.
    """
    cached = getattr(request.state, "user_id", None)
    if isinstance(cached, str) and cached:
        return cached

    try:
        mode = get_auth_mode()
    except (UnknownAuthModeError, DevAuthInProductionError) as err:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(err),
        ) from err

    if mode is AuthMode.DEV:
        user_id = _dev_user_id()
    elif mode is AuthMode.TRUSTED_HEADER:
        forwarded = request.headers.get(_TRUSTED_USER_ID_HEADER, "").strip()
        if not forwarded:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"missing {_TRUSTED_USER_ID_HEADER}",
            )
        user_id = forwarded
    else:  # AuthMode.IAP
        jwt = (
            request.headers.get(_IAP_JWT_HEADER, "").strip()
            or request.headers.get(_FORWARDED_IAP_JWT_HEADER, "").strip()
        )
        if jwt:
            claims = _verify_iap_jwt(jwt)
            email = claims.get("email")
            if not isinstance(email, str) or not email:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="IAP JWT missing email claim",
                )
            user_id = email
        else:
            bearer = _extract_bearer(request)
            if not bearer:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=f"missing {_IAP_JWT_HEADER} or OAuth bearer",
                )
            user_id = _resolve_oauth_bearer(bearer)

    request.state.user_id = user_id
    _user_id_var.set(user_id)
    return user_id


def current_user_id(request: Request) -> str:
    """FastAPI dependency / helper: return the user_id resolved earlier by
    ``IdentityMiddleware``. Falls back to ``resolve_user_id`` so unit tests
    can hit routes without going through the middleware.
    """
    return resolve_user_id(request)


# Paths that must skip identity resolution entirely — health probes and
# IAP's own OAuth round-trip. The OAuth tool callback is NOT here: when IAP
# is in front, the user is already authenticated by the time Google
# redirects back, and the callback handler cross-checks the state's
# user_id against the IAP identity to prevent token theft.
_UNAUTHENTICATED_PATHS: frozenset[str] = frozenset(
    {
        "/",
        "/.well-known/agent-card.json",
    }
)
_UNAUTHENTICATED_PREFIXES: tuple[str, ...] = (
    "/_gcp_iap/",  # IAP's own endpoints
    "/openapi",
    "/docs",
    "/redoc",
    # Cloud Scheduler calls the backend directly with its own OIDC token;
    # the routes verify it via verify_cloud_scheduler_token. They don't
    # operate on a request-bound user — they iterate stored users
    # themselves — so bypassing IdentityMiddleware here is correct.
    "/scheduler/",
)


def _is_public_path(path: str) -> bool:
    if path in _UNAUTHENTICATED_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in _UNAUTHENTICATED_PREFIXES)


class IdentityMiddleware(BaseHTTPMiddleware):
    """Resolve the user_id once per request and stash it on ``request.state``.

    Routes that need the id call ``current_user_id(request)`` (or read
    ``request.state.user_id`` directly). Public paths (health, OAuth
    callback, IAP machinery) bypass resolution.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        # OPTIONS preflight always passes — CORS handles its own auth model.
        if request.method == "OPTIONS" or _is_public_path(request.url.path):
            return await call_next(request)
        try:
            resolve_user_id(request)
        except HTTPException as exc:
            # Use the same shape as FastAPI's exception handler would.
            from fastapi.responses import JSONResponse

            return JSONResponse(
                status_code=exc.status_code,
                content={"detail": exc.detail},
            )
        return await call_next(request)
