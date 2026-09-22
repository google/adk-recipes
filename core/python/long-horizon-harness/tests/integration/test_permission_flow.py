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

"""End-to-end: a gated command asks, the user approves for the session, and an
identical second call runs without asking again."""

from __future__ import annotations

import pytest

from horizon.guardrails.permission_guard import permission_guard
from horizon.guardrails.permission_rules import PERMISSION_GRANTS_STATE_KEY

pytestmark = pytest.mark.asyncio


class _Tool:
    name = "bash"


class _Actions:
    skip_summarization = False


class _Ctx:
    def __init__(self, state, tool_confirmation=None):
        self.state = state
        self.actions = _Actions()
        self.tool_confirmation = tool_confirmation
        self.function_call_id = "fc"
        self.agent_name = "root"

    def request_confirmation(self, *, hint=None, payload=None):
        self.last_payload = payload


class _TC:
    def __init__(self, confirmed, payload):
        self.confirmed = confirmed
        self.payload = payload


async def test_session_grant_suppresses_second_prompt(tmp_path, monkeypatch):
    from horizon.environment import LocalEnvironment
    from horizon.environment_context import set_active_environment

    env = LocalEnvironment(working_dir=tmp_path)
    env._working_dir = tmp_path
    set_active_environment(env)

    state: dict = {}

    # First call → asks (pending).
    ctx1 = _Ctx(state)
    r1 = await permission_guard(
        tool=_Tool(), args={"command": "bq rm x"}, tool_context=ctx1
    )
    assert r1 and r1.get("confirmation_required")

    # Resume with "this session".
    ctx2 = _Ctx(
        state, tool_confirmation=_TC(True, {"outcome": "proceed_always"})
    )
    r2 = await permission_guard(
        tool=_Tool(), args={"command": "bq rm x"}, tool_context=ctx2
    )
    assert r2 is None
    assert state[PERMISSION_GRANTS_STATE_KEY]

    # A fresh identical call in the same session → allowed, no prompt.
    ctx3 = _Ctx(state)
    r3 = await permission_guard(
        tool=_Tool(), args={"command": "bq rm y"}, tool_context=ctx3
    )
    assert r3 is None  # "bq rm" prefix grant covers "bq rm y"


@pytest.mark.parametrize(
    "command",
    [
        "git push --force origin HEAD",
        # Sneaky rephrases a model might try instead of the caught spelling
        # above must be caught too, or the confirmation is a one-word filter.
        "git push -f origin HEAD",
        "git push --force-with-lease origin HEAD",
    ],
)
async def test_git_push_force_and_rephrases_all_require_confirmation(
    tmp_path, command
):
    from horizon.environment import LocalEnvironment
    from horizon.environment_context import set_active_environment

    env = LocalEnvironment(working_dir=tmp_path)
    env._working_dir = tmp_path
    set_active_environment(env)

    ctx = _Ctx(state={})
    result = await permission_guard(
        tool=_Tool(), args={"command": command}, tool_context=ctx
    )

    # A before_tool_callback that returns a dict short-circuits the real tool
    # call entirely (ADK contract) -- confirmation_required=True here IS the
    # proof the push never ran, not merely a claim about it.
    assert result is not None
    assert result.get("confirmation_required") is True
    assert "approval" in result["error"].lower()


async def test_git_push_without_force_does_not_require_confirmation(tmp_path):
    # Contrast case: the gate is force-specific, not a blanket push blocker.
    from horizon.environment import LocalEnvironment
    from horizon.environment_context import set_active_environment

    env = LocalEnvironment(working_dir=tmp_path)
    env._working_dir = tmp_path
    set_active_environment(env)

    ctx = _Ctx(state={})
    result = await permission_guard(
        tool=_Tool(),
        args={"command": "git push origin main"},
        tool_context=ctx,
    )
    assert result is None
