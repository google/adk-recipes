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

"""Unit tests for ``GET /lha/contexts/{context_id}/tasks``."""

from __future__ import annotations

import pytest
from a2a.server.context import ServerCallContext
from a2a.server.tasks import InMemoryTaskStore
from fastapi import FastAPI
from fastapi.testclient import TestClient
from google.adk.a2a import _compat as a2a_compat
from google.adk.agents.invocation_context import new_invocation_context_id
from google.adk.agents.llm_agent import LlmAgent
from google.adk.artifacts import InMemoryArtifactService
from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions
from google.adk.memory import InMemoryMemoryService
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService

from horizon.a2a.executor import LHA_ACTIVE_TASK_KEY, LHA_TASK_IDS_KEY
from horizon.api.tasks import attach_task_routes
from horizon.auth import current_user_id

APP_NAME = "app"
USER_ID = "u"
_CTX = ServerCallContext()


def _make_runner() -> Runner:
    return Runner(
        app_name=APP_NAME,
        agent=LlmAgent(
            name="test_agent",
            model="gemini-3.7-flash",
            description="A test agent",
            instruction="Test.",
        ),
        session_service=InMemorySessionService(),
        memory_service=InMemoryMemoryService(),
        artifact_service=InMemoryArtifactService(),
    )


def _make_task(task_id: str, *, state: int, context_id: str):
    return a2a_compat.make_task(
        id=task_id,
        context_id=context_id,
        status=a2a_compat.make_task_status(state),
        history=[
            a2a_compat.make_message(
                message_id=f"{task_id}-msg",
                role=a2a_compat.ROLE_USER,
                parts=[a2a_compat.make_text_part("hi")],
                context_id=context_id,
                task_id=task_id,
            )
        ],
    )


async def _seed_session(
    runner: Runner,
    *,
    session_id: str,
    task_ids: list[str] | None = None,
    active_task_id: str | None = None,
) -> None:
    await runner.session_service.create_session(
        app_name=APP_NAME,
        user_id=USER_ID,
        state={},
        session_id=session_id,
    )
    if task_ids is None and active_task_id is None:
        return
    session = await runner.session_service.get_session(
        app_name=APP_NAME, user_id=USER_ID, session_id=session_id
    )
    state_delta: dict[str, object] = {}
    if task_ids is not None:
        state_delta[LHA_TASK_IDS_KEY] = task_ids
    if active_task_id is not None or task_ids is not None:
        state_delta[LHA_ACTIVE_TASK_KEY] = active_task_id
    await runner.session_service.append_event(
        session,
        Event(
            invocation_id=new_invocation_context_id(),
            author="system",
            actions=EventActions(state_delta=state_delta),
        ),
    )


def _build_app(runner: Runner, task_store: InMemoryTaskStore) -> FastAPI:
    app = FastAPI()
    attach_task_routes(app, runner=runner, task_store=task_store)
    app.dependency_overrides[current_user_id] = lambda: USER_ID
    return app


@pytest.mark.asyncio
async def test_unknown_context_returns_empty_list() -> None:
    runner = _make_runner()
    task_store = InMemoryTaskStore()
    client = TestClient(_build_app(runner, task_store))

    resp = client.get("/lha/contexts/does-not-exist/tasks")

    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_known_context_with_no_tasks_returns_empty_list() -> None:
    runner = _make_runner()
    task_store = InMemoryTaskStore()
    await _seed_session(runner, session_id="ctx-empty")
    client = TestClient(_build_app(runner, task_store))

    resp = client.get("/lha/contexts/ctx-empty/tasks")

    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_lists_tasks_in_insertion_order_with_status() -> None:
    runner = _make_runner()
    task_store = InMemoryTaskStore()
    ctx = "ctx-multi"
    await task_store.save(
        _make_task("t-1", state=a2a_compat.TS_COMPLETED, context_id=ctx), _CTX
    )
    await task_store.save(
        _make_task("t-2", state=a2a_compat.TS_COMPLETED, context_id=ctx), _CTX
    )
    await task_store.save(
        _make_task("t-3", state=a2a_compat.TS_WORKING, context_id=ctx), _CTX
    )
    await _seed_session(
        runner,
        session_id=ctx,
        task_ids=["t-1", "t-2", "t-3"],
        active_task_id="t-3",
    )
    client = TestClient(_build_app(runner, task_store))

    resp = client.get(f"/lha/contexts/{ctx}/tasks")

    assert resp.status_code == 200
    body = resp.json()
    assert [row["id"] for row in body] == ["t-1", "t-2", "t-3"]
    assert [row["status"] for row in body] == [
        "completed",
        "completed",
        "working",
    ]
    assert [row["isActive"] for row in body] == [False, False, True]


@pytest.mark.asyncio
async def test_skips_tasks_missing_from_task_store() -> None:
    runner = _make_runner()
    task_store = InMemoryTaskStore()
    ctx = "ctx-gap"
    await task_store.save(
        _make_task("t-1", state=a2a_compat.TS_COMPLETED, context_id=ctx), _CTX
    )
    # t-2 was started but task store evicted it (or never persisted) — endpoint
    # must skip it rather than 500 or invent a status.
    await _seed_session(
        runner,
        session_id=ctx,
        task_ids=["t-1", "t-2"],
        active_task_id=None,
    )
    client = TestClient(_build_app(runner, task_store))

    resp = client.get(f"/lha/contexts/{ctx}/tasks")

    assert resp.status_code == 200
    body = resp.json()
    assert [row["id"] for row in body] == ["t-1"]
    assert body[0]["isActive"] is False
