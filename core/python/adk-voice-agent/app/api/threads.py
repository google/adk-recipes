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

"""REST endpoints over the thread registry, scoped to the signed-in user."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.api.serializers import thread_detail, thread_summary
from app.threads.model import (
    FINISHED_STATES,
    GATE_STATES,
    RUNNING_STATES,
    ThreadState,
)
from app.threads.registry import (
    ThreadError,
    ThreadInputError,
    ThreadNotFound,
    get_thread_registry,
)

router = APIRouter()


_STATE_GROUPS: dict[str, frozenset[ThreadState]] = {
    "finished": FINISHED_STATES,
    "active": RUNNING_STATES | GATE_STATES,
}


class CreateThreadRequest(BaseModel):
    goal: str = ""
    request: str = ""
    title: str = ""
    project: str = ""
    harness: str | None = None


class MessageThreadRequest(BaseModel):
    instruction: str = ""
    message: str = ""
    harness: str | None = None


class ApproveThreadRequest(BaseModel):
    permission_key: str | None = None


@contextmanager
def _thread_errors() -> Iterator[None]:
    try:
        yield
    except ThreadNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ThreadInputError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ThreadError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/threads")
async def api_create_thread(req: CreateThreadRequest) -> dict[str, Any]:
    goal = (req.goal or req.request).strip()
    with _thread_errors():
        handle = await get_thread_registry().dispatch(
            goal,
            req.project,
            harness=req.harness,
            title=req.title,
        )
    return thread_detail(handle)


@router.get("/threads")
async def api_list_threads(
    q: str = "",
    before: str | None = None,
    limit: int = Query(30, ge=1, le=100),
    state: str | None = None,
) -> dict[str, Any]:
    states: frozenset[ThreadState] | None = None
    if state:
        try:
            states = _STATE_GROUPS.get(state) or frozenset({ThreadState(state)})
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail=f"Unknown thread state: {state}."
            ) from exc
    with _thread_errors():
        handles, next_before = await get_thread_registry().query(
            q=q,
            before=before,
            limit=limit,
            states=sorted(states) if states else None,
        )
    summaries = [thread_summary(h) for h in handles]
    return {
        "threads": summaries,
        "next_before": next_before,
    }


@router.delete("/threads/{thread_id}")
async def api_delete_thread(thread_id: str) -> dict[str, Any]:
    with _thread_errors():
        await get_thread_registry().delete(thread_id)
    return {
        "ok": True,
        "deleted": thread_id,
        "message": f"Deleted {thread_id}.",
    }


@router.get("/threads/{thread_id}")
async def api_get_thread(thread_id: str) -> dict[str, Any]:
    with _thread_errors():
        registry = get_thread_registry()
        handle = await registry.require(thread_id)
    return thread_detail(handle)


@router.get("/threads/{thread_id}/events")
async def api_thread_events(
    thread_id: str, since_seq: int = 0
) -> dict[str, Any]:
    with _thread_errors():
        handle = await get_thread_registry().require(thread_id)
    events = [e.to_dict() for e in handle.events if e.seq > since_seq]
    return {
        "thread_id": handle.thread_id,
        "events": events,
        "last_seq": handle.last_seq,
    }


@router.post("/threads/{thread_id}/message")
async def api_message_thread(
    thread_id: str, req: MessageThreadRequest
) -> dict[str, Any]:
    instruction = req.instruction or req.message
    with _thread_errors():
        handle = await get_thread_registry().steer(
            thread_id, instruction, harness=req.harness
        )
    return thread_detail(handle)


@router.post("/threads/{thread_id}/approve")
async def api_approve_thread(
    thread_id: str, req: ApproveThreadRequest | None = None
) -> dict[str, Any]:
    key = (req.permission_key if req and req.permission_key else "").strip()
    with _thread_errors():
        registry = get_thread_registry()
        await registry.require(thread_id)
        if not key:
            raise ThreadInputError("permission_key is required.")
        handle = await registry.approve(
            thread_id,
            permission_key=key,
        )
    return thread_detail(handle)


@router.post("/threads/{thread_id}/cancel")
async def api_cancel_thread(thread_id: str) -> dict[str, Any]:
    with _thread_errors():
        handle = await get_thread_registry().cancel(thread_id)
    return thread_detail(handle)
