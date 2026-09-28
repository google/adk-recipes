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

"""``POST /scheduler/snapshot`` — daily per-user sandbox snapshot + prune.

Body: ``{"user_ids": [], "app_name": "lha"}``. An **empty** ``user_ids`` (what
the nightly cron sends) means "every user with a session in the lookback
window" — discovered via :func:`list_active_users`. A non-empty list targets
those users explicitly.

For each user it snapshots the current RUNNING sandbox (capturing the full
``$HOME`` the zip ``/workspace`` migration can't carry) and prunes to the
newest ``LHA_SNAPSHOT_KEEP`` snapshots, so an environment survives the sandbox
TTL/idle teardown. No-op unless ``LHA_SNAPSHOT_ENABLED`` is set (Phase C is
gated on the snapshot probe — see scripts/probes/probe_sandbox_snapshot.py).
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.memory.dream_review import list_active_users, lookback_seconds
from app.scheduler.auth import verify_cloud_scheduler_token

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/scheduler",
    dependencies=[Depends(verify_cloud_scheduler_token)],
)

DEFAULT_SNAPSHOT_TTL = "30d"
DEFAULT_SNAPSHOT_KEEP = 2


class SnapshotRequest(BaseModel):
    user_ids: list[str] = []
    app_name: str = ""


def _keep() -> int:
    try:
        return max(1, int(os.environ.get("LHA_SNAPSHOT_KEEP", "")))
    except (TypeError, ValueError):
        return DEFAULT_SNAPSHOT_KEEP


@router.post("/snapshot")
async def snapshot_tick(payload: SnapshotRequest, request: Request) -> Any:
    """Snapshot + prune every active user (empty body) or the listed users."""
    from app.conversation.session_start import (
        _will_attempt_sandbox,
        snapshots_enabled,
    )
    from app.sandbox.lifecycle import snapshot_and_prune_user
    from app.sandbox.provider import (
        _resolve_sandbox_engine,
        _vertex_client_factory,
    )

    if not snapshots_enabled() or not _will_attempt_sandbox():
        return {"snapshotted": 0, "failed": 0, "reason": "snapshots disabled"}

    runner = getattr(request.app.state, "runner", None)
    session_service = getattr(runner, "session_service", None)
    app_name = getattr(runner, "app_name", None) or payload.app_name
    if session_service is None or not app_name:
        return {
            "snapshotted": 0,
            "failed": 0,
            "reason": "runner not registered",
        }

    user_ids = payload.user_ids
    if not user_ids:
        since_ts = datetime.now(UTC).timestamp() - lookback_seconds()
        user_ids = await list_active_users(
            session_service, app_name=app_name, since_ts=since_ts
        )
    if not user_ids:
        return {"snapshotted": 0, "failed": 0, "reason": "no active users"}

    try:
        client = _vertex_client_factory()
        agent_instance = _resolve_sandbox_engine(client)
    except Exception:
        logger.exception("snapshot_tick: sandbox engine resolution failed")
        return JSONResponse(
            status_code=500,
            content={
                "snapshotted": 0,
                "failed": len(user_ids),
                "errors": user_ids,
            },
        )

    ttl = os.environ.get("LHA_SNAPSHOT_TTL", "").strip() or DEFAULT_SNAPSHOT_TTL
    keep = _keep()
    snapshotted = 0
    failed = 0
    errors: list[str] = []
    for user_id in user_ids:
        try:
            result = await asyncio.to_thread(
                snapshot_and_prune_user,
                client,
                agent_instance_name=agent_instance,
                user_id=user_id,
                snapshot_ttl=ttl,
                keep=keep,
            )
        except Exception:
            logger.exception(
                "snapshot_tick: per-user pass raised for %s", user_id
            )
            failed += 1
            errors.append(user_id)
            continue
        if result.get("status") == "snapshotted":
            snapshotted += 1

    body = {"snapshotted": snapshotted, "failed": failed, "errors": errors}
    if snapshotted == 0 and failed > 0:
        return JSONResponse(status_code=500, content=body)
    return body


__all__ = ["router"]
