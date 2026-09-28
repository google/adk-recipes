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

# GET /lha/contexts/{context_id}/tasks — joins the per-context task-id list
# stashed in ADK session state (executor.py) with TaskStore status, so the web
# UI can enumerate tasks A2A's TaskStore can't list by context.

from __future__ import annotations

import logging
from typing import Any

from a2a.server.context import ServerCallContext
from a2a.server.tasks import TaskStore
from a2a.types import TaskState
from fastapi import APIRouter, Depends, FastAPI
from google.adk.runners import Runner

from app.a2a.executor import LHA_ACTIVE_TASK_KEY, LHA_TASK_IDS_KEY
from app.auth import current_user_id
from app.infrastructure.constants import APP_NAME

logger = logging.getLogger(__name__)


def _state_str(state: int) -> str:
    # proto TaskState int -> v0.3 status string ("completed", "input-required").
    return (
        TaskState.Name(state)
        .removeprefix("TASK_STATE_")
        .lower()
        .replace("_", "-")
    )


def attach_task_routes(
    app: FastAPI, *, runner: Runner, task_store: TaskStore
) -> None:
    router = APIRouter(prefix="/lha/contexts", tags=["lha"])

    @router.get("/{context_id}/tasks")
    async def list_tasks(
        context_id: str,
        user_id: str = Depends(current_user_id),
    ) -> list[dict[str, Any]]:
        try:
            session = await runner.session_service.get_session(
                app_name=APP_NAME, user_id=user_id, session_id=context_id
            )
        except Exception:
            logger.exception(
                "lha_tasks: get_session failed for %s/%s", user_id, context_id
            )
            return []
        if session is None:
            return []

        task_ids = session.state.get(LHA_TASK_IDS_KEY) or []
        active_task_id = session.state.get(LHA_ACTIVE_TASK_KEY)

        out: list[dict[str, Any]] = []
        for task_id in task_ids:
            try:
                task = await task_store.get(task_id, ServerCallContext())
            except Exception:
                logger.exception(
                    "lha_tasks: task_store.get failed for %s", task_id
                )
                continue
            if task is None:
                continue
            out.append(
                {
                    "id": task.id,
                    "status": _state_str(task.status.state),
                    "isActive": task_id == active_task_id,
                }
            )
        return out

    app.include_router(router)
