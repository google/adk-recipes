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

"""OIDC verifier for the ``/scheduler/*`` endpoints.

Verifies an inbound ID token via ``google.oauth2.id_token``, the same library
Cloud Scheduler's callers use to mint one.

Configuration:

* ``LHA_SCHEDULER_AUDIENCE`` — required when auth is enabled. Set in
  Terraform to the Cloud Run service URL (Cloud Scheduler's OIDC token
  carries this as its ``aud`` claim).
* ``LHA_SCHEDULER_SA`` — comma-separated allowlist of service-account
  emails. Verified token's ``email`` claim must match one (case-insensitive).
  Omit to allow any verified caller (rare — usually you want exactly one SA).
* ``LHA_SCHEDULER_AUTH_DISABLED`` — set truthy to skip all checks.
  Intended for local dev (``curl localhost:8000/scheduler/routine-tick``). A
  WARNING is logged on first bypass so accidental prod use is visible.

Fails closed: if audience is unset and auth is not explicitly disabled,
returns 500 rather than letting unauthenticated callers through.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from fastapi import HTTPException, Request

from horizon.infrastructure.env import env_flag

logger = logging.getLogger(__name__)

_bypass_warning_emitted = False


def _is_auth_disabled() -> bool:
    return env_flag("LHA_SCHEDULER_AUTH_DISABLED")


def _verify_oauth2_token(token: str, audience: str) -> dict[str, Any]:
    """Verify the JWT and return the decoded claims.

    Wrapped so unit tests can monkeypatch without touching google.auth.
    """
    from google.auth.transport.requests import Request as GoogleRequest
    from google.oauth2 import id_token

    return id_token.verify_oauth2_token(token, GoogleRequest(), audience)


def _extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


def _allowlist() -> list[str]:
    raw = os.environ.get("LHA_SCHEDULER_SA", "").strip()
    if not raw:
        return []
    return [s.strip().lower() for s in raw.split(",") if s.strip()]


def _reset_bypass_warning_for_tests() -> None:
    global _bypass_warning_emitted
    _bypass_warning_emitted = False


async def verify_cloud_scheduler_token(request: Request) -> None:
    """FastAPI dependency: gate the scheduler endpoints on Cloud Scheduler OIDC."""
    global _bypass_warning_emitted
    if _is_auth_disabled():
        if not _bypass_warning_emitted:
            logger.warning(
                "LHA_SCHEDULER_AUTH_DISABLED is set; scheduler endpoints "
                "are accepting unauthenticated requests. Unset this env var "
                "outside local development."
            )
            _bypass_warning_emitted = True
        return

    audience = os.environ.get("LHA_SCHEDULER_AUDIENCE", "").strip()
    if not audience:
        logger.error(
            "verify_cloud_scheduler_token: LHA_SCHEDULER_AUDIENCE not set; "
            "refusing to verify token. Set LHA_SCHEDULER_AUTH_DISABLED=1 "
            "for local dev."
        )
        raise HTTPException(
            status_code=500, detail="scheduler audience not configured"
        )

    token = _extract_bearer(request.headers.get("authorization"))
    if token is None:
        raise HTTPException(
            status_code=401, detail="missing or invalid bearer token"
        )

    try:
        # google.auth's verify is sync and performs a blocking JWKS fetch
        # on cold cache; offload so the FastAPI event loop keeps moving.
        claims = await asyncio.to_thread(_verify_oauth2_token, token, audience)
    except Exception as exc:
        logger.warning("scheduler token verification failed: %s", exc)
        raise HTTPException(
            status_code=401, detail="token verification failed"
        ) from exc

    allowlist = _allowlist()
    if allowlist:
        email = claims.get("email")
        normalized = email.lower() if isinstance(email, str) else None
        if not normalized or normalized not in allowlist:
            logger.warning(
                "scheduler token email %r not on allowlist; rejecting", email
            )
            raise HTTPException(
                status_code=403, detail="caller not on scheduler allowlist"
            )


__all__ = ["verify_cloud_scheduler_token"]
