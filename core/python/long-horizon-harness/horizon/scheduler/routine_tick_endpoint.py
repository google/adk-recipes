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

"""``POST /scheduler/routine-tick`` — fire every due routine.

Each due routine runs an agent turn through the shared A2A handler under
``routine_isolation`` (OWN isolated sandbox + headless + declared-secrets scope),
in a job_type="routine" session.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from a2a.server.context import ServerCallContext
from a2a.types import Message, Part, SendMessageRequest, Task
from fastapi import APIRouter, Depends, Request
from google.adk.a2a import _compat as a2a_compat

from horizon.auth import user_identity_scope
from horizon.routines.isolation import HEADLESS_PREAMBLE, routine_isolation
from horizon.scheduler.auth import verify_cloud_scheduler_token
from horizon.scheduler.routine_store import RoutineRow, get_routine_store
from horizon.scheduler.sessions import create_scheduled_session

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/scheduler",
    dependencies=[Depends(verify_cloud_scheduler_token)],
)


def _text_from_parts(parts: list[Part] | None) -> str:
    out: list[str] = []
    for part in parts or []:
        if a2a_compat.is_text_part(part):
            text = a2a_compat.part_text(part).strip()
            if text:
                out.append(text)
    return "\n".join(out)


def _final_text_from_result(result: Message | Task) -> str:
    """Pull the agent's final reply text out of a Message or Task result.

    Inlined here (formerly shared with tick_endpoint.py, deleted with the
    reminder capability) since this is now the only caller.
    """
    if isinstance(result, Message):
        return _text_from_parts(result.parts)
    status_message = getattr(result.status, "message", None)
    if status_message is not None:
        text = _text_from_parts(status_message.parts)
        if text:
            return text
    for message in reversed(result.history or []):
        if message.role == a2a_compat.ROLE_AGENT:
            text = _text_from_parts(message.parts)
            if text:
                return text
    for artifact in result.artifacts or []:
        text = _text_from_parts(artifact.parts)
        if text:
            return text
    return ""


async def _run_routine_turn(
    handler: Any, *, session_id: str, routine: RoutineRow
) -> str:
    message = HEADLESS_PREAMBLE.format(routine_id=routine.id) + routine.task
    params = SendMessageRequest(
        message=a2a_compat.make_message(
            message_id=uuid4().hex,
            role=a2a_compat.ROLE_USER,
            parts=[a2a_compat.make_text_part(message)],
            context_id=session_id,
        )
    )
    result = await handler.on_message_send(params, ServerCallContext())
    return _final_text_from_result(result)


async def _fire_routine(runner: Any, handler: Any, routine: RoutineRow) -> bool:
    if runner is None or handler is None:
        return False
    try:
        with routine_isolation(
            routine.id, owner=routine.user_id, secrets=routine.secrets
        ):
            session = await create_scheduled_session(
                runner.session_service,
                app_name=getattr(runner, "app_name", None) or routine.app_name,
                user_id=routine.user_id,
                job_type="routine",
                title=f"Routine: {routine.id}",
            )
            with user_identity_scope(routine.user_id):
                await _run_routine_turn(
                    handler,
                    session_id=session.id,
                    routine=routine,
                )
        return True
    except Exception:
        logger.exception("routine-tick: turn failed for %s", routine.id)
        return False


@router.post("/routine-tick")
async def routine_tick(request: Request) -> dict[str, Any]:
    store = get_routine_store()
    runner = getattr(request.app.state, "runner", None)
    handler = getattr(request.app.state, "a2a_handler", None)
    claimed = await store.claim_due(datetime.now(UTC))
    fired = 0
    failed = 0
    for routine in claimed:
        ok = await _fire_routine(runner, handler, routine)
        fired += 1
        if not ok:
            failed += 1
    body: dict[str, Any] = {"fired": fired}
    if failed:
        body["failed"] = failed
    return body


__all__ = ["router"]
