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

"""``POST /scheduler/dream-review`` — daily cross-session memory pass.

Body: ``{"user_ids": [], "app_name": "lha"}``. An **empty** ``user_ids``
(what the nightly cron sends) means "every user with a session in the
lookback window" — discovered via :func:`list_active_users`. A non-empty
list targets those users explicitly.

For each user it calls :func:`app.memory.dream_review._run_dream_review_for_user`,
forwarding the runner-bound ``memory_service`` + ``session_service`` (from
``app.state.runner``) so the user's recent session events can be read and handed
to Memory Bank's ``generate`` to consolidate their Structured Profile. The
``app_name`` is taken from the runner (where sessions are actually keyed), not
the request body. Sums the ``success: True`` results into
``{"reviewed": N, "failed": M}``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.memory.dream_review import (
    _run_dream_review_for_user,
    list_active_users,
    lookback_seconds,
)
from app.scheduler.auth import verify_cloud_scheduler_token

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/scheduler",
    dependencies=[Depends(verify_cloud_scheduler_token)],
)


class DreamReviewRequest(BaseModel):
    user_ids: list[str] = []
    app_name: str = ""


@router.post("/dream-review")
async def dream_review_tick(
    payload: DreamReviewRequest, request: Request
) -> Any:
    """Dream-review every active user (empty body) or the listed users."""
    runner = getattr(request.app.state, "runner", None)
    memory_service = getattr(runner, "memory_service", None)
    if memory_service is None:
        return {
            "reviewed": 0,
            "failed": 0,
            "reason": "memory service not registered",
        }

    session_service = getattr(runner, "session_service", None)
    if session_service is None:
        return {
            "reviewed": 0,
            "failed": 0,
            "reason": "session service not registered",
        }

    # Sessions are keyed by the runner's app name; trust it over the request
    # body so discovery and per-user lookup match where sessions live.
    app_name = getattr(runner, "app_name", None) or payload.app_name
    if not app_name:
        return {"reviewed": 0, "failed": 0, "reason": "no app_name"}

    user_ids = payload.user_ids
    discovered = not user_ids
    if discovered:
        since_ts = datetime.now(UTC).timestamp() - lookback_seconds()
        user_ids = await list_active_users(
            session_service, app_name=app_name, since_ts=since_ts
        )
    logger.warning(
        "dream_review: %d user(s) to review (%s, app_name=%s)",
        len(user_ids),
        "auto-discovered" if discovered else "from request",
        app_name,
    )
    if not user_ids:
        return {"reviewed": 0, "failed": 0, "reason": "no active users"}

    reviewed = 0
    failed = 0
    errors: list[str] = []
    for user_id in user_ids:
        try:
            result = await _run_dream_review_for_user(
                memory_service=memory_service,
                app_name=app_name,
                user_id=user_id,
                session_service=session_service,
            )
        except Exception:
            logger.exception(
                "dream_review: per-user pass raised for %s", user_id
            )
            failed += 1
            errors.append(user_id)
            continue
        if result.get("success"):
            reviewed += 1
        else:
            failed += 1
            errors.append(user_id)

    logger.warning("dream_review: reviewed=%d failed=%d", reviewed, failed)
    body = {"reviewed": reviewed, "failed": failed, "errors": errors}
    if reviewed == 0 and failed > 0:
        return JSONResponse(status_code=500, content=body)
    return body


__all__ = ["router"]
