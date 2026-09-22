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

"""The ``bound_skills`` catalog written by ``bind_session_skills_callback``
must survive ADK's state-delta commit and be readable from the session
service after the turn — not merely land on a transient ``CallbackContext``.

The unit test in ``tests/unit/test_skill_catalog_state.py`` drives the
callback with a plain-dict context, which proves the *write call* but not
the round-trip through ``Runner`` + ``InMemorySessionService``. This pins
the persistence contract that ``/lha/state`` relies on.

No Vertex, no LLM: a second ``before_agent_callback`` short-circuits the
agent with a synthetic response before the model is reached, while the
real Runner machinery still commits the callback's ``state`` delta.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from google.adk.agents import Agent
from google.adk.agents.callback_context import CallbackContext
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from horizon.environment import LocalEnvironment
from horizon.environment_context import set_active_environment
from horizon.tools import skill_reload
from horizon.tools.skill_loader import build_skill_toolset, builtin_skills_root

pytestmark = pytest.mark.asyncio

APP_NAME = "test_bound_skills"

_USER_SKILL_MD = (
    "---\n"
    "name: my-changelog\n"
    "description: keep a running changelog\n"
    "---\n"
    "# my-changelog\n\nbody\n"
)


def _write_skill(root: Path, name: str, content: str) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")


def _short_circuit(callback_context: CallbackContext) -> types.Content:
    """Skip the model so the turn never needs an LLM; the prior
    ``bind_session_skills_callback`` has already staged its state delta."""
    return types.Content(role="model", parts=[types.Part(text="ok")])


@pytest.fixture
def bound_workspace(tmp_path: Path):
    ws = tmp_path / "ws"
    skills_dir = ws / ".agents" / "skills"
    skills_dir.mkdir(parents=True)
    _write_skill(skills_dir, "my-changelog", _USER_SKILL_MD)
    set_active_environment(LocalEnvironment(working_dir=ws))

    toolset = build_skill_toolset(
        user_dir=skills_dir, builtin_dir=builtin_skills_root()
    )
    skill_reload.bind_toolset(
        toolset, user_dir=skills_dir, builtin_dir=builtin_skills_root()
    )
    try:
        yield ws
    finally:
        skill_reload.unbind_toolset()


def _agent_with_bind_callback() -> Agent:
    return Agent(
        name="root_agent",
        model="gemini-3.7-flash",
        before_agent_callback=[
            skill_reload.bind_session_skills_callback,
            _short_circuit,
        ],
    )


async def _run_one_turn(
    runner_factory: Callable[..., Runner],
    session_service: InMemorySessionService,
) -> str:
    runner = runner_factory(
        agent=_agent_with_bind_callback(),
        app_name=APP_NAME,
        session_service=session_service,
    )
    session = await session_service.create_session(
        app_name=APP_NAME, user_id="u1"
    )
    async for _ in runner.run_async(
        user_id="u1",
        session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part(text="hi")]),
    ):
        pass
    return session.id


async def test_bound_skills_persists_through_session_service(
    bound_workspace: Path,
    runner_factory: Callable[..., Runner],
    fake_session_service: InMemorySessionService,
) -> None:
    session_id = await _run_one_turn(runner_factory, fake_session_service)

    refetched = await fake_session_service.get_session(
        app_name=APP_NAME, user_id="u1", session_id=session_id
    )
    assert refetched is not None
    catalog = refetched.state.get(skill_reload.BOUND_SKILLS_STATE_KEY)
    assert isinstance(catalog, list) and catalog, (
        "bound_skills did not round-trip into the session service"
    )

    by_name = {row["name"]: row for row in catalog}
    # The freshly-created (never-invoked) user skill is present and tagged.
    assert by_name["my-changelog"]["source"] == "user"
    assert by_name["my-changelog"]["description"] == "keep a running changelog"
    # A real built-in is present without any invocation.
    assert by_name["policy"]["source"] == "builtin"
    # Every row carries the contract shape /lha/state echoes verbatim.
    for row in catalog:
        assert set(row) == {"name", "description", "source"}
