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

"""GET /lha/memories — cross-session list of a user's memories + profile."""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query
from google.adk.runners import Runner

from app.auth import current_user_id
from app.infrastructure.constants import APP_NAME
from app.memory.memory_list import list_memory_writes

logger = logging.getLogger(__name__)


def attach_memories_routes(app: FastAPI, *, runner: Runner) -> None:
    router = APIRouter(prefix="/lha", tags=["lha"])

    @router.get("/memories")
    async def get_memories(
        limit: int = Query(5, ge=1, le=50),
        offset: int = Query(0, ge=0),
        user_id: str = Depends(current_user_id),
    ) -> dict[str, Any]:
        try:
            result = await list_memory_writes(
                memory_service=runner.memory_service,
                app_name=APP_NAME,
                user_id=user_id,
                limit=limit,
                offset=offset,
            )
        except Exception as err:
            logger.exception("lha_memories: list_memory_writes failed")
            raise HTTPException(
                status_code=500, detail="memory listing failed"
            ) from err
        return {
            "entries": [asdict(e) for e in result.entries],
            "counts": result.counts,
            "has_more": result.has_more,
        }

    app.include_router(router)
