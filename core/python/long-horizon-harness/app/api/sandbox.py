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

"""POST /lha/sandbox/warm and GET /lha/sandbox/status.

These endpoints front-load sandbox provisioning at UI boot instead of
deferring it to the first user message — sandbox cold-start can take
30-60s and the user otherwise sits watching a spinner. The UI fires
``POST /sandbox/warm`` immediately after the chat shell mounts (in
parallel with the A2A client init) and polls ``GET /sandbox/status``
until the state flips to ``ready`` or ``error``.

The warm endpoint is fire-and-forget: it schedules
``session_start._ensure_environment`` on a background task and returns
in <50ms so the UI moves on. Progress is tracked in ``_warm_status``
— a per-instance dict that records this instance's view of the
in-flight provision attempt. Cross-instance UI state (the chat-shell
banner) is derived from Vertex in ``session_start.get_provisioning_status``;
the warm endpoint owns the local "we just kicked off provisioning"
signal that Vertex can't see until the sandbox object actually appears.

Concurrency between the warm-POST and the agent's
``on_session_start_callback`` is handled by a per-user
``asyncio.Lock`` inside ``_ensure_environment`` — both paths join the
same provisioning attempt rather than racing.
"""

from __future__ import annotations

import asyncio
import datetime as _datetime
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI

from app.auth import current_user_id
from app.conversation import session_start

logger = logging.getLogger(__name__)

# Strong refs to in-flight warm tasks. Without this the event loop can
# garbage-collect a fire-and-forget task before it finishes; tasks
# self-remove on done.
_warm_tasks: set[asyncio.Task[None]] = set()

# Per-instance warm progress. Keys are user_ids; values carry
# ``status`` ("provisioning" | "ready" | "error") plus optional
# ``started_at`` / ``ready_at`` / ``error`` fields.
_warm_status: dict[str, dict[str, Any]] = {}


def _now_iso() -> str:
    return _datetime.datetime.now(_datetime.UTC).isoformat()


async def _compute_warm_status(user_id: str) -> dict[str, Any]:
    try:
        await session_start._ensure_environment(user_id)
    except Exception as exc:
        logger.warning("warm sandbox provisioning failed: %s", exc)
        return {"status": "error", "error": str(exc)}
    return {"status": "ready", "ready_at": _now_iso()}


def attach_sandbox_routes(app: FastAPI) -> None:
    """Mount ``POST /lha/sandbox/warm`` and ``GET /lha/sandbox/status``."""
    router = APIRouter(prefix="/lha", tags=["lha"])

    @router.post("/sandbox/warm", status_code=202)
    async def warm_sandbox(
        user_id: Annotated[str, Depends(current_user_id)],
    ) -> dict[str, Any]:
        async def _kick() -> None:
            _warm_status[user_id] = {
                "status": "provisioning",
                "started_at": _now_iso(),
            }
            _warm_status[user_id] = await _compute_warm_status(user_id)

        task = asyncio.create_task(_kick())
        _warm_tasks.add(task)
        task.add_done_callback(_warm_tasks.discard)
        status = _warm_status.get(user_id) or {"status": "provisioning"}
        return {**status, "user_id": user_id}

    @router.get("/sandbox/status")
    async def sandbox_status(
        user_id: Annotated[str, Depends(current_user_id)],
    ) -> dict[str, Any]:
        status = _warm_status.get(user_id)
        if status is None:
            # Distinguishes "no warm has been kicked yet" from
            # "provisioning in progress" so the UI banner stays hidden
            # when the warm-POST itself failed (network blip, 401, etc.).
            return {"status": "idle", "user_id": user_id}
        return {**status, "user_id": user_id}

    app.include_router(router)


__all__ = ["attach_sandbox_routes"]
