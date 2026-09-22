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

"""``process(action='spawn')`` must run through the same guard-chain command
extraction as every other shell entry point, proven behaviorally: a fork
bomb or ``rm -rf /`` must hard-deny, and command substitution must demote
to ``ask_user``, exactly like ``bash``. If either guard extracted the
command from ``bash`` only, this file is where that gap shows up.
"""

from __future__ import annotations

import pytest

from horizon.environment_context import set_active_environment
from horizon.guardrails.permission_guard import permission_guard
from horizon.guardrails.policies import policies_guard
from horizon.tools import names

pytestmark = pytest.mark.asyncio


class _Actions:
    def __init__(self) -> None:
        self.skip_summarization = False


class _Ctx:
    """Minimal ToolContext stand-in, mirroring
    tests/unit/guardrails/test_permission_guard.py's _Ctx."""

    def __init__(self) -> None:
        self.state: dict = {}
        self.actions = _Actions()
        self.tool_confirmation = None
        self.function_call_id = "fc-1"
        self.agent_name = "root"
        self._requested = None

    def request_confirmation(self, *, hint=None, payload=None):
        self._requested = {"hint": hint, "payload": payload}


class _Tool:
    def __init__(self, name: str) -> None:
        self.name = name


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    from horizon.environment import LocalEnvironment

    env = LocalEnvironment(working_dir=tmp_path)
    set_active_environment(env)
    monkeypatch.setenv("HOME", str(tmp_path))
    return env


# =============================================================================
# Layer C (policies_guard): the JSONL destructive-command seed and
# command_safety's structural deny both extract via _command_for, which
# must recognize process(action='spawn') the same way it recognizes bash.
# =============================================================================


@pytest.mark.parametrize(
    "command",
    ["rm -rf /", ":(){:|:&};:"],
)
async def test_spawn_hard_deny_matches_bash(command: str):
    bash_result = await policies_guard(
        tool=_Tool(names.BASH),
        args={"command": command},
        tool_context=_Ctx(),
    )
    spawn_result = await policies_guard(
        tool=_Tool(names.PROCESS),
        args={"action": "spawn", "command": command},
        tool_context=_Ctx(),
    )

    assert (
        isinstance(bash_result, dict)
        and bash_result.get("confirmation_required") is True
    ), f"bash must hard-deny {command!r}"
    assert (
        isinstance(spawn_result, dict)
        and spawn_result.get("confirmation_required") is True
    ), f"process(action='spawn') must hard-deny {command!r} exactly like bash"


async def test_spawn_safe_command_is_allowed():
    result = await policies_guard(
        tool=_Tool(names.PROCESS),
        args={"action": "spawn", "command": "sleep 30"},
        tool_context=_Ctx(),
    )
    assert result is None


async def test_process_write_hard_deny_is_unaffected_by_the_spawn_addition():
    # Regression guard: adding the spawn branch to _command_for /
    # _shell_command must not disturb the existing write-action extraction.
    result = await policies_guard(
        tool=_Tool(names.PROCESS),
        args={"action": "write", "data": "rm -rf /"},
        tool_context=_Ctx(),
    )
    assert (
        isinstance(result, dict) and result.get("confirmation_required") is True
    )


# =============================================================================
# Layer D (permission_guard): command-substitution demotion to ask_user.
# =============================================================================


async def test_spawn_command_substitution_demotes_to_ask_matches_bash():
    bash_ctx, spawn_ctx = _Ctx(), _Ctx()

    bash_result = await permission_guard(
        tool=_Tool(names.BASH),
        args={"command": "echo $(cat /etc/passwd)"},
        tool_context=bash_ctx,
    )
    spawn_result = await permission_guard(
        tool=_Tool(names.PROCESS),
        args={"action": "spawn", "command": "echo $(cat /etc/passwd)"},
        tool_context=spawn_ctx,
    )

    assert (
        bash_result is not None
        and bash_result.get("confirmation_required") is True
    )
    assert (
        spawn_result is not None
        and spawn_result.get("confirmation_required") is True
    ), (
        "process(action='spawn') must demote command substitution to ask_user exactly like bash"
    )


async def test_spawn_plain_command_is_allowed_without_a_prompt():
    ctx = _Ctx()
    result = await permission_guard(
        tool=_Tool(names.PROCESS),
        args={"action": "spawn", "command": "sleep 30"},
        tool_context=ctx,
    )
    assert result is None
    assert ctx._requested is None


async def test_process_poll_is_unaffected_by_the_spawn_addition():
    # Regression guard: process actions with no command (poll/list/etc.)
    # must not suddenly need shell-command handling.
    ctx = _Ctx()
    result = await permission_guard(
        tool=_Tool(names.PROCESS),
        args={"action": "poll", "session_id": "proc_x"},
        tool_context=ctx,
    )
    assert result is None
