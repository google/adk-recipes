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

"""Project index, thread feed, rename/hide, and history deletion routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.projects import (
    delete_project_history,
    hide_project,
    list_project_items,
    name_project,
    project_thread,
)

router = APIRouter()


@router.get("/projects")
async def list_projects() -> dict[str, Any]:
    return {"projects": await list_project_items()}


@router.get("/projects/{project:path}/thread")
async def get_thread(project: str) -> dict[str, Any]:
    thread = await project_thread(project)
    if thread is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    return thread


class ProjectUpdate(BaseModel):
    name: str | None = None
    hidden: bool | None = None


@router.patch("/projects/{project:path}")
async def update_project(project: str, update: ProjectUpdate) -> dict[str, Any]:
    try:
        if update.name is not None:
            project = await name_project(project, update.name)
        if update.hidden and not await hide_project(project):
            raise ValueError(f"{project!r} isn't a project.")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": project}


@router.delete("/projects/{project:path}")
async def delete_project(project: str) -> dict[str, Any]:
    deleted = await delete_project_history(project)
    if deleted is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    return {"deleted_threads": deleted}
