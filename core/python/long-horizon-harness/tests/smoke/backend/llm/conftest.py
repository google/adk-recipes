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

"""LLM-in-loop smoke tier — drives the real ``root_agent`` against Gemini.

Gated behind ``RUN_SMOKE_LLM=1`` (on top of ``RUN_SMOKE=1``) because every
test here makes real Gemini calls — measurable cost per run and subject to
LLM non-determinism. The tool-driven tier (sibling tests in the parent
directory) is the cheap regression net; this tier catches *tool selection
drift* — the agent forgetting to call ``terminal`` when asked, or routing
through the wrong tool.

Assertions target tool trajectory + side effects, never natural-language
content. See ``AGENTS.md``: "Never assert on LLM output content in pytest."

The shared sandbox is pre-bound into ``session_start._env_cache`` so the
agent's ``before_agent_callback`` finds it and skips provisioning a new
environment — keeping the LLM tier on the same shared instance the
tool-driven tier uses.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
import pytest_asyncio
from google.adk.environment import BaseEnvironment


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if os.environ.get("RUN_SMOKE_LLM"):
        return
    skip = pytest.mark.skip(
        reason="set RUN_SMOKE_LLM=1 to enable LLM-in-loop smoke tests"
    )
    for item in items:
        if "tests/smoke/backend/llm" in str(item.fspath):
            item.add_marker(skip)


@pytest.fixture(scope="session", autouse=True)
def _bind_shared_env_to_session_cache(
    shared_environment: BaseEnvironment, smoke_user_id: str
) -> Iterator[None]:
    """Pre-populate ``_env_cache`` so ``on_session_start_callback`` reuses
    our shared env instead of provisioning its own."""
    from app.conversation import session_start

    backend = (
        os.environ.get("LHA_ENVIRONMENT_BACKEND", "sandbox").strip().lower()
    )
    key = (backend, smoke_user_id)
    session_start._env_cache[key] = shared_environment
    try:
        yield
    finally:
        session_start._env_cache.pop(key, None)


@pytest_asyncio.fixture(loop_scope="session")
async def llm_runner(smoke_user_id: str) -> AsyncIterator[Any]:
    """Build a ``Runner`` wired to ``root_agent`` + in-memory services
    for one test. Per-test scope so each test gets a fresh session id and
    can't see another's state."""
    from google.adk.artifacts import InMemoryArtifactService
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService

    from app.agent import root_agent

    session_service = InMemorySessionService()
    session = await session_service.create_session(
        user_id=smoke_user_id, app_name="smoke"
    )
    runner = Runner(
        app_name="smoke",
        agent=root_agent,
        session_service=session_service,
        artifact_service=InMemoryArtifactService(),
    )
    yield _RunnerHandle(
        runner=runner, user_id=smoke_user_id, session_id=session.id
    )


class _RunnerHandle:
    """Bundle the Runner with the session/user identifiers the caller
    needs every turn."""

    def __init__(self, *, runner: Any, user_id: str, session_id: str) -> None:
        self.runner = runner
        self.user_id = user_id
        self.session_id = session_id

    async def run_once(self, prompt: str) -> list[Any]:
        """Send ``prompt`` and return the full event list."""
        from google.genai import types as genai_types

        message = genai_types.Content(
            role="user", parts=[genai_types.Part.from_text(text=prompt)]
        )
        events = []
        async for event in self.runner.run_async(
            new_message=message,
            user_id=self.user_id,
            session_id=self.session_id,
        ):
            events.append(event)
        return events


def function_call_names(events: list[Any]) -> list[str]:
    """Extract tool names from a sequence of ADK events."""
    names: list[str] = []
    for event in events:
        content = getattr(event, "content", None)
        if content is None:
            continue
        for part in getattr(content, "parts", None) or []:
            fc = getattr(part, "function_call", None)
            if fc is not None and getattr(fc, "name", None):
                names.append(fc.name)
    return names
