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

"""/lha/routines — per-user read + cancel of scheduled routines.

Read/cancel surface over the same RoutineStore the agent's routine() tool writes
to; creating routines stays with the agent (HITL-gated).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException

from app.auth import current_user_id
from app.scheduler.routine_store import get_routine_store


def attach_routines_routes(app: FastAPI) -> None:
    router = APIRouter(prefix="/lha", tags=["lha"])

    @router.get("/routines")
    async def list_routines(
        user_id: str = Depends(current_user_id),
    ) -> dict[str, Any]:
        rows = await get_routine_store().list_for_user(user_id)
        return {
            "routines": [
                {
                    "id": r.id,
                    "task": r.task,
                    "schedule": r.schedule,
                    "next_fire_at": r.next_fire_at.isoformat(),
                    "secrets": list(r.secrets),
                    "delivery": r.delivery,
                }
                for r in rows
            ]
        }

    @router.delete("/routines/{routine_id}")
    async def cancel_routine(
        routine_id: str,
        user_id: str = Depends(current_user_id),
    ) -> dict[str, Any]:
        cancelled = await get_routine_store().cancel(routine_id, user_id)
        if not cancelled:
            raise HTTPException(status_code=404, detail="routine not found")
        return {"id": routine_id, "ok": True}

    app.include_router(router)
