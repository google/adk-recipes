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

"""Per-request user identity (`dev` or IAP JWT) and ADK route binding."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import contextvars
import enum
import json
import logging
import os
import re
import time
from collections.abc import Iterable, Iterator
from typing import Any
from urllib.parse import parse_qsl

from google.auth import exceptions as google_auth_exceptions
from google.auth import jwt as google_jwt
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.app_utils.http_client import get_http_client
from app.auth.providers import is_remote_deployment

logger = logging.getLogger(__name__)

IAP_JWT_HEADER = "x-goog-iap-jwt-assertion"
IAP_ISSUER = "https://cloud.google.com/iap"
# PEM public keys by key id so google-auth can verify ES256 without PyJWT.
IAP_PUBLIC_KEYS_URL = "https://www.gstatic.com/iap/verify/public_key"
IAP_KEYS_TTL_S = 3600.0
IAP_KEYS_MIN_REFETCH_S = 60.0
IAP_CLOCK_SKEW_S = 30
DEFAULT_DEV_USER_ID = "local"

_user_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "voice_agent_user_id", default=None
)


class AuthMode(enum.StrEnum):
    DEV = "dev"
    IAP = "iap"


class IdentityError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def current_user_id() -> str:
    """Return the request's user id; raises `RuntimeError` outside a request."""
    user_id = _user_id.get()
    if not user_id:
        raise RuntimeError(
            "No user identity: current_user_id() was called outside a request."
        )
    return user_id


@contextlib.contextmanager
def run_as(user_id: str) -> Iterator[None]:
    """Bind `user_id` for background tasks running outside an HTTP request."""
    if not user_id:
        raise ValueError("run_as() needs a user id")
    token = _user_id.set(user_id)
    try:
        yield
    finally:
        _user_id.reset(token)


def get_auth_mode() -> AuthMode:
    """Resolve `VOICE_AGENT_AUTH_MODE`, failing closed if `dev` is set on a deployed service."""
    raw = (
        (os.getenv("VOICE_AGENT_AUTH_MODE") or AuthMode.DEV.value)
        .strip()
        .lower()
    )
    try:
        mode = AuthMode(raw)
    except ValueError:
        raise IdentityError(
            500,
            f"VOICE_AGENT_AUTH_MODE={raw!r} is not one of "
            f"{', '.join(m.value for m in AuthMode)}.",
        ) from None
    if mode is AuthMode.DEV and is_remote_deployment():
        raise IdentityError(
            500,
            "VOICE_AGENT_AUTH_MODE=dev is refused on a deployed service. "
            "Set VOICE_AGENT_AUTH_MODE=iap and VOICE_AGENT_IAP_AUDIENCE.",
        )
    return mode


def dev_user_id() -> str:
    return (
        os.getenv("VOICE_AGENT_DEV_USER_ID") or ""
    ).strip() or DEFAULT_DEV_USER_ID


def acting_user_id_or_none() -> str | None:
    try:
        return acting_user_id()
    except RuntimeError:
        return None


def acting_user_id() -> str:
    """Return the bound user id, falling back to `dev_user_id()` only in `dev` mode."""
    user_id = _user_id.get()
    if user_id:
        return user_id
    if get_auth_mode() is AuthMode.DEV:
        return dev_user_id()
    raise RuntimeError(
        "No user identity: this work runs outside a request and outside run_as()."
    )


class IapKeysUnavailableError(Exception):
    pass


class _IapKeys:
    """Cached IAP public keys; refetches on unknown `kid` at most once per minute."""

    def __init__(self) -> None:
        self._keys: dict[str, str] = {}
        self._fetched_at = 0.0
        self._lock = asyncio.Lock()

    async def get(self, key_id: str) -> dict[str, str]:
        async with self._lock:
            age = time.monotonic() - self._fetched_at
            unknown = key_id not in self._keys and age > IAP_KEYS_MIN_REFETCH_S
            if not self._keys or age > IAP_KEYS_TTL_S or unknown:
                await self._fetch()
            return self._keys

    async def _fetch(self) -> None:
        try:
            async with get_http_client() as client:
                response = await client.get(IAP_PUBLIC_KEYS_URL)
            response.raise_for_status()
            keys = response.json()
            if not isinstance(keys, dict):
                raise ValueError("IAP public keys are not a JSON object")
        except Exception as err:
            raise IapKeysUnavailableError(str(err)) from err
        self._keys = {str(k): str(v) for k, v in keys.items()}
        self._fetched_at = time.monotonic()

    def clear(self) -> None:
        self._keys = {}
        self._fetched_at = 0.0


_iap_keys = _IapKeys()


def reset_iap_key_cache() -> None:
    _iap_keys.clear()


def _unverified_header(token: str) -> dict[str, Any]:
    segment = token.split(".", 1)[0]
    padded = segment + "=" * (-len(segment) % 4)
    header = json.loads(base64.urlsafe_b64decode(padded))
    if not isinstance(header, dict):
        raise ValueError("JWT header is not an object")
    return header


async def verify_iap_assertion(token: str) -> str:
    audience = (os.getenv("VOICE_AGENT_IAP_AUDIENCE") or "").strip()
    if not audience:
        raise IdentityError(
            500,
            "VOICE_AGENT_AUTH_MODE=iap needs VOICE_AGENT_IAP_AUDIENCE to be set.",
        )
    try:
        header = _unverified_header(token)
        # google-auth selects a verifier from the token's own header unless pinned.
        if header.get("alg") != "ES256":
            raise ValueError(f"unexpected JWT algorithm {header.get('alg')!r}")
        key_id = header.get("kid")
        if not isinstance(key_id, str) or not key_id:
            raise ValueError("JWT has no key id")
        keys = await _iap_keys.get(key_id)
        if key_id not in keys:
            raise ValueError(f"unknown IAP key id {key_id!r}")
        claims = google_jwt.decode(
            token,
            certs=keys,
            audience=audience,
            clock_skew_in_seconds=IAP_CLOCK_SKEW_S,
        )
        if claims.get("iss") != IAP_ISSUER:
            raise ValueError(f"unexpected issuer {claims.get('iss')!r}")
    except IapKeysUnavailableError as err:
        logger.warning("Could not fetch IAP public keys: %s", err)
        raise IdentityError(
            503, "Could not fetch IAP public keys to verify the request."
        ) from err
    except (
        ValueError,
        google_auth_exceptions.GoogleAuthError,
    ) as err:
        logger.info("IAP JWT rejected: %s", err)
        raise IdentityError(401, "Invalid IAP assertion.") from err
    email = claims.get("email")
    if not isinstance(email, str) or not email.strip():
        raise IdentityError(401, "IAP assertion has no email claim.")
    return email.strip().lower()


async def resolve_user_id(headers: dict[bytes, bytes]) -> str:
    if get_auth_mode() is AuthMode.DEV:
        return dev_user_id()
    token = headers.get(IAP_JWT_HEADER.encode(), b"").decode("latin-1").strip()
    if not token:
        raise IdentityError(401, "Missing X-Goog-IAP-JWT-Assertion header.")
    return await verify_iap_assertion(token)


_ADK_USER_PATH = re.compile(r"^(?:/dev)?/apps/[^/]+/users/([^/]+)(?:/|$)")
_ADK_USER_BODY_PATHS = frozenset(
    {"/run", "/run_sse", "/agent-identity/finalize"}
)
_USER_ID_KEYS = frozenset({"user_id", "userId"})
PUBLIC_PATHS = frozenset({"/health"})


def _json_user_ids(body: bytes) -> list[str] | None:
    """Collect every top-level `user_id`/`userId` value, including duplicate keys."""
    last_pairs: list[tuple[str, Any]] = []

    def collect(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        last_pairs[:] = pairs
        return dict(pairs)

    try:
        parsed = json.loads(body or b"{}", object_pairs_hook=collect)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    return [str(value) for key, value in last_pairs if key in _USER_ID_KEYS]


def claimed_user_ids(scope: Scope, body: bytes | None = None) -> list[str]:
    path = scope.get("path", "")
    claims: list[str] = []
    if match := _ADK_USER_PATH.match(path):
        claims.append(match.group(1))
    if path == "/run_live":
        query = scope.get("query_string", b"").decode("latin-1")
        claims.extend(v for k, v in parse_qsl(query) if k in _USER_ID_KEYS)
    if body is not None:
        claims.extend(_json_user_ids(body) or [])
    return claims


def _mismatch(claims: Iterable[str], user_id: str) -> bool:
    return any(claim != user_id for claim in claims)


def _needs_body_check(scope: Scope) -> bool:
    return (
        scope["type"] == "http"
        and scope.get("method") == "POST"
        and scope.get("path") in _ADK_USER_BODY_PATHS
    )


def _is_mcp_path(path: str) -> bool:
    return path == "/api/v1/mcp" or path.startswith("/api/v1/mcp/")


def _is_service_account_user(user_id: str) -> bool:
    uid = user_id.strip().lower()
    if not uid:
        return False
    if uid.endswith(".gserviceaccount.com"):
        return True
    computer_sa = os.getenv("COMPUTER_SERVICE_ACCOUNT", "").strip().lower()
    return bool(computer_sa and uid == computer_sa)


async def _read_body(receive: Receive) -> tuple[bytes, list[Message]]:
    messages: list[Message] = []
    chunks: list[bytes] = []
    while True:
        message = await receive()
        messages.append(message)
        if message["type"] != "http.request":
            break
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            break
    return b"".join(chunks), messages


def _replay(messages: list[Message], receive: Receive) -> Receive:
    pending = list(messages)

    async def replayed() -> Message:
        if pending:
            return pending.pop(0)
        return await receive()

    return replayed


class IdentityMiddleware:
    """Resolve the signed-in user and reject requests whose claimed ADK user differs."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        if scope["type"] not in {"http", "websocket"} or self._is_public(scope):
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        try:
            user_id = await resolve_user_id(headers)
        except IdentityError as err:
            await self._deny(scope, receive, send, err.status_code, err.detail)
            return

        if _is_service_account_user(user_id) and not _is_mcp_path(
            scope.get("path", "")
        ):
            await self._deny(
                scope,
                receive,
                send,
                403,
                "Service account identities may only access /api/v1/mcp.",
            )
            return

        body: bytes | None = None
        if _needs_body_check(scope):
            body, messages = await _read_body(receive)
            receive = _replay(messages, receive)
        if _mismatch(claimed_user_ids(scope, body), user_id):
            await self._deny(
                scope,
                receive,
                send,
                403,
                "The user id in this request is not the signed-in user.",
            )
            return

        token = _user_id.set(user_id)
        try:
            await self.app(scope, receive, send)
        finally:
            _user_id.reset(token)

    @staticmethod
    def _is_public(scope: Scope) -> bool:
        if scope["type"] == "http" and scope.get("method") == "OPTIONS":
            return True
        return scope.get("path") in PUBLIC_PATHS

    @staticmethod
    async def _deny(
        scope: Scope, receive: Receive, send: Send, status: int, detail: str
    ) -> None:
        if scope["type"] == "websocket":
            await send(
                {
                    "type": "websocket.close",
                    "code": 1008,
                    "reason": detail[:120],
                }
            )
            return
        await JSONResponse({"detail": detail}, status_code=status)(
            scope, receive, send
        )
