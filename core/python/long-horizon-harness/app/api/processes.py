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

"""/lha/processes — per-user read + kill of running sandbox processes.

Peek-only over the BaseEnvironment interface: a passive read never provisions a
sandbox (empty list when none is warm).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException

from app.auth import current_user_id
from app.conversation import session_start


def attach_processes_routes(app: FastAPI) -> None:
    router = APIRouter(prefix="/lha", tags=["lha"])

    @router.get("/processes")
    async def list_processes(
        user_id: str = Depends(current_user_id),
    ) -> dict[str, Any]:
        env = await session_start.peek_environment(user_id)
        if env is None:
            return {"processes": []}
        try:
            rows = await env.list_processes()
        except Exception:
            rows = []
        return {"processes": rows}

    @router.delete("/processes/{session_id}")
    async def kill_process(
        session_id: str,
        user_id: str = Depends(current_user_id),
    ) -> dict[str, Any]:
        env = await session_start.peek_environment(user_id)
        if env is None or not await env.kill_process(session_id):
            raise HTTPException(status_code=404, detail="process not found")
        return {"session_id": session_id, "ok": True}

    app.include_router(router)
