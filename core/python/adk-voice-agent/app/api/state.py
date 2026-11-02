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

"""The `/state` snapshot the UI polls, the `/config` Settings reads, and their mutations."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.api.apps import apps_state
from app.api.me import account_summary
from app.api.serializers import thread_summary
from app.api.surfaces import build_a2ui_surfaces
from app.app_utils.user_context import warm_user_context
from app.auth.identity import acting_user_id
from app.auth.providers import is_remote_deployment
from app.context_cards import dismiss_context_card
from app.memory.store import memory_enabled
from app.threads.registry import get_thread_registry
from app.user_settings import (
    DEFAULT_HARNESS,
    save_user_settings,
    user_default_harness,
)
from app.voice.focus import set_focus
from app.voice.preferences import get_runtime_preferences, save_preferences
from app.workers.harnesses.registry import get_harness_registry
from app.workers.providers.last_known import last_known_status

router = APIRouter()


class SetHarnessRequest(BaseModel):
    harness: str


class DismissSurfaceRequest(BaseModel):
    surface_id: str


class FocusRequest(BaseModel):
    view: str = "home"
    project: str = ""
    thread_id: str = ""


class UpdatePreferencesRequest(BaseModel):
    timezone: str | None = None
    voice_name: str | None = None
    voice_mode: str | None = None
    save_to_drive: str | None = None


@router.get("/state")
async def get_orchestrator_state() -> dict[str, Any]:
    """In-memory snapshot only; shares the event loop with the live audio stream."""
    handles = await get_thread_registry().list_all()
    computer = last_known_status(acting_user_id())
    summaries = [thread_summary(h) for h in handles]
    return {
        "threads": summaries,
        "a2ui_surfaces": build_a2ui_surfaces(),
        "computer": {
            "state": computer.state.value if computer else None,
            "detail": computer.detail if computer else "",
        },
    }


@router.post("/focus")
async def api_set_focus(req: FocusRequest) -> dict[str, Any]:
    focus = set_focus(
        user_id=acting_user_id(),
        view=req.view,
        project=req.project,
        thread_id=req.thread_id,
    )
    return {"ok": True, "focus": focus}


@router.get("/config")
async def get_orchestrator_config() -> dict[str, Any]:
    await warm_user_context()
    harness_reg = get_harness_registry()
    default = user_default_harness().name
    return {
        "account": account_summary(),
        "harnesses": [
            {**harness_reg.ref(h.name), "is_default": h.name == default}
            for h in harness_reg.list_all()
        ],
        "apps": apps_state(),
        "is_remote": is_remote_deployment(),
        "preferences": get_runtime_preferences(),
        "memory_enabled": memory_enabled(),
    }


@router.patch("/preferences")
async def api_update_preferences(
    req: UpdatePreferencesRequest,
) -> dict[str, Any]:
    try:
        preferences = await save_preferences(
            timezone=req.timezone,
            voice_name=req.voice_name,
            voice_mode=req.voice_mode,
            save_to_drive=req.save_to_drive,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "preferences": preferences}


@router.post("/harnesses/default")
async def api_set_default_harness(req: SetHarnessRequest) -> dict[str, Any]:
    registry = get_harness_registry()
    try:
        selected = registry.get(req.harness)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await save_user_settings({DEFAULT_HARNESS: selected.name})
    return {
        "ok": True,
        "message": f"New threads will use {selected.display_name}.",
        "default_harness": registry.ref(selected.name),
    }


@router.post("/a2ui/dismiss")
async def api_dismiss_surface(req: DismissSurfaceRequest) -> dict[str, Any]:
    dismiss_context_card(req.surface_id)
    return {"ok": True}
