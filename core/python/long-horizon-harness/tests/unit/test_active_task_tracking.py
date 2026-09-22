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

"""Unit tests for the active-task tracking hook on ``_TaskTrackingExecutor``."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.adk.agents.llm_agent import LlmAgent
from google.adk.artifacts import InMemoryArtifactService
from google.adk.memory import InMemoryMemoryService
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService

from horizon.a2a.executor import (
    LHA_ACTIVE_TASK_KEY,
    LHA_TASK_IDS_KEY,
    build_executor,
)
from horizon.auth.identity import _user_id_var


@pytest.fixture(autouse=True)
def _authenticated_user():
    """Simulate IdentityMiddleware having resolved ``user_id="u"`` for this
    request; the A2A converter reads from the contextvar.
    """
    token = _user_id_var.set("u")
    try:
        yield
    finally:
        _user_id_var.reset(token)


def _make_runner() -> Runner:
    return Runner(
        app_name="test_app",
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


def _make_context(
    *,
    task_id: str,
    session_id: str = "s",
) -> MagicMock:
    """Stub RequestContext with the attributes the request converter reads."""
    context = MagicMock()
    context.task_id = task_id
    context.context_id = session_id
    context.requested_extensions = set()
    # _get_user_id reads call_context.user.user_name — keep None so the
    # converter falls back to the authenticated user from the contextvar.
    context.call_context = None
    context.metadata = None
    context.current_task = None
    message = MagicMock()
    message.metadata = None
    message.context_id = session_id
    message.task_id = task_id
    message.parts = []
    context.message = message
    return context


@pytest.mark.asyncio
async def test_execute_records_task_in_state_on_start() -> None:
    executor = build_executor(runner=_make_runner())
    runner = await executor._resolve_runner()
    context = _make_context(task_id="task-A", session_id="s-record")

    # Stub the parent execute so the test doesn't try to run an LLM.
    with patch(
        "google.adk.a2a.executor.a2a_agent_executor.A2aAgentExecutor.execute",
        new_callable=AsyncMock,
    ):
        await executor.execute(context, MagicMock())

    session = await runner.session_service.get_session(
        app_name=runner.app_name, user_id="u", session_id="s-record"
    )
    assert session is not None
    assert session.state.get(LHA_TASK_IDS_KEY) == ["task-A"]
    # Cleared on finally — verified separately below.


@pytest.mark.asyncio
async def test_execute_clears_active_flag_on_completion() -> None:
    executor = build_executor(runner=_make_runner())
    runner = await executor._resolve_runner()
    context = _make_context(task_id="task-X", session_id="s-clear")

    with patch(
        "google.adk.a2a.executor.a2a_agent_executor.A2aAgentExecutor.execute",
        new_callable=AsyncMock,
    ):
        await executor.execute(context, MagicMock())

    session = await runner.session_service.get_session(
        app_name=runner.app_name, user_id="u", session_id="s-clear"
    )
    assert session is not None
    assert session.state.get(LHA_ACTIVE_TASK_KEY) is None


@pytest.mark.asyncio
async def test_execute_clears_active_flag_on_exception() -> None:
    executor = build_executor(runner=_make_runner())
    runner = await executor._resolve_runner()
    context = _make_context(task_id="task-boom", session_id="s-boom")

    with patch(
        "google.adk.a2a.executor.a2a_agent_executor.A2aAgentExecutor.execute",
        new_callable=AsyncMock,
        side_effect=RuntimeError("agent blew up"),
    ):
        with pytest.raises(RuntimeError, match="agent blew up"):
            await executor.execute(context, MagicMock())

    session = await runner.session_service.get_session(
        app_name=runner.app_name, user_id="u", session_id="s-boom"
    )
    assert session is not None
    assert session.state.get(LHA_ACTIVE_TASK_KEY) is None
    # The task id is still recorded — it ran, it just failed.
    assert session.state.get(LHA_TASK_IDS_KEY) == ["task-boom"]


@pytest.mark.asyncio
async def test_execute_clears_active_flag_when_producer_cancelled() -> None:
    """Cloud Run reaping the request after a client disconnect cancels the
    producer task mid-flight. ``_mark_task_done`` must still run so the
    session's active-task flag clears — that's what ``asyncio.shield`` in the
    finally block buys us.
    """
    executor = build_executor(runner=_make_runner())
    runner = await executor._resolve_runner()
    context = _make_context(task_id="task-cancel", session_id="s-cancel")

    async def _slow_then_cancelled(*_a, **_kw):
        # Simulate the SDK doing real work, then being cancelled by the
        # surrounding task (Cloud Run reap / SIGTERM / etc.).
        await asyncio.sleep(0)
        raise asyncio.CancelledError

    with patch(
        "google.adk.a2a.executor.a2a_agent_executor.A2aAgentExecutor.execute",
        new=_slow_then_cancelled,
    ):
        with pytest.raises(asyncio.CancelledError):
            await executor.execute(context, MagicMock())

    session = await runner.session_service.get_session(
        app_name=runner.app_name, user_id="u", session_id="s-cancel"
    )
    assert session is not None
    assert session.state.get(LHA_ACTIVE_TASK_KEY) is None
    assert session.state.get(LHA_TASK_IDS_KEY) == ["task-cancel"]


@pytest.mark.asyncio
async def test_execute_clears_active_flag_when_outer_task_cancelled() -> None:
    """If the asyncio task wrapping ``execute`` itself is cancelled (the
    realistic Cloud Run shape — Cloud Run cancels the request task, which
    propagates CancelledError into the producer), the shielded
    ``_mark_task_done`` should still complete in the background.
    """
    executor = build_executor(runner=_make_runner())
    runner = await executor._resolve_runner()
    context = _make_context(task_id="task-outer", session_id="s-outer")

    started = asyncio.Event()

    async def _block_until_cancelled(*_a, **_kw):
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            raise

    with patch(
        "google.adk.a2a.executor.a2a_agent_executor.A2aAgentExecutor.execute",
        new=_block_until_cancelled,
    ):
        task = asyncio.create_task(executor.execute(context, MagicMock()))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    # Give the shielded background coroutine a tick to land.
    await asyncio.sleep(0.05)

    session = await runner.session_service.get_session(
        app_name=runner.app_name, user_id="u", session_id="s-outer"
    )
    assert session is not None
    assert session.state.get(LHA_ACTIVE_TASK_KEY) is None


@pytest.mark.asyncio
async def test_execute_appends_to_existing_task_list() -> None:
    executor = build_executor(runner=_make_runner())
    runner = await executor._resolve_runner()
    session_id = "s-multi"

    with patch(
        "google.adk.a2a.executor.a2a_agent_executor.A2aAgentExecutor.execute",
        new_callable=AsyncMock,
    ):
        await executor.execute(
            _make_context(task_id="task-1", session_id=session_id), MagicMock()
        )
        await executor.execute(
            _make_context(task_id="task-2", session_id=session_id), MagicMock()
        )
        await executor.execute(
            _make_context(task_id="task-3", session_id=session_id), MagicMock()
        )

    session = await runner.session_service.get_session(
        app_name=runner.app_name, user_id="u", session_id=session_id
    )
    assert session is not None
    assert session.state.get(LHA_TASK_IDS_KEY) == ["task-1", "task-2", "task-3"]
    assert session.state.get(LHA_ACTIVE_TASK_KEY) is None
