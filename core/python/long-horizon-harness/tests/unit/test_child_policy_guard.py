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

"""Child-scoped before_tool_callback: profile allowlist + inherited grants."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.asyncio


class _Tool:
    def __init__(self, name: str) -> None:
        self.name = name


class _Ctx:
    def __init__(self, state: dict[str, Any] | None = None) -> None:
        self.state: dict[str, Any] = state if state is not None else {}


async def test_profile_blocks_tool_not_in_allowlist() -> None:
    from horizon.subagents.child_guard import make_child_policy_guard
    from horizon.subagents.profiles import get_profile

    guard = make_child_policy_guard(
        parent_grants=None, profile=get_profile("explore")
    )
    result = await guard(tool=_Tool("bash"), args={}, tool_context=_Ctx())
    assert result is not None
    assert result.get("denied_by_profile") is True
    assert "bash" in result["error"]


async def test_profile_allows_tool_in_allowlist() -> None:
    from horizon.subagents.child_guard import make_child_policy_guard
    from horizon.subagents.profiles import get_profile

    guard = make_child_policy_guard(
        parent_grants=None, profile=get_profile("explore")
    )
    # read is in the allowlist; with no policy match it passes (None).
    result = await guard(
        tool=_Tool("read"), args={"path": "x.txt"}, tool_context=_Ctx()
    )
    assert result is None


async def test_no_profile_falls_through_to_policy_guard() -> None:
    from horizon.subagents.child_guard import make_child_policy_guard

    guard = make_child_policy_guard(parent_grants=None, profile=None)
    # A benign tool with no policy rule passes.
    result = await guard(
        tool=_Tool("read"), args={"path": "x.txt"}, tool_context=_Ctx()
    )
    assert result is None


async def test_inherited_grant_seeded_onto_child_state() -> None:
    from horizon.guardrails.policy_grants import POLICY_GRANTS_STATE_KEY
    from horizon.subagents.child_guard import make_child_policy_guard

    grant = {"tool_name": "bash", "signature": {"command": "rm -rf build/"}}
    guard = make_child_policy_guard(parent_grants=[grant], profile=None)
    ctx = _Ctx()
    await guard(tool=_Tool("read"), args={"path": "x.txt"}, tool_context=ctx)
    # The inherited grant is now visible on the child's state for policy eval.
    assert ctx.state[POLICY_GRANTS_STATE_KEY] == [grant]


async def test_malformed_parent_grants_treated_as_empty() -> None:
    from horizon.subagents.child_guard import make_child_policy_guard

    guard = make_child_policy_guard(parent_grants="not-a-list", profile=None)
    result = await guard(
        tool=_Tool("read"), args={"path": "x.txt"}, tool_context=_Ctx()
    )
    assert result is None


async def test_child_still_blocks_risky_commands() -> None:
    from horizon.subagents.child_guard import make_child_policy_guard

    guard = make_child_policy_guard(parent_grants=None, profile=None)
    result = await guard(
        tool=_Tool("bash"),
        args={"command": "sudo apt-get update"},
        tool_context=_Ctx(),
    )
    assert result is not None


# ---- resurfacing mode (inside a delegate child drain) ---------------------


class _ConfirmCtx(_Ctx):
    def __init__(
        self, state: dict[str, Any] | None = None, tool_confirmation: Any = None
    ) -> None:
        super().__init__(state)
        self.tool_confirmation = tool_confirmation
        self.agent_name = "child"
        self.actions = SimpleNamespace(skip_summarization=False)
        self.requested: list[dict[str, Any]] = []

    def request_confirmation(self, *, hint: str, payload: Any = None) -> None:
        self.requested.append({"hint": hint, "payload": payload})


async def test_resurface_asks_when_bubble_available() -> None:
    from horizon.subagents.child_guard import make_child_policy_guard
    from horizon.subagents.resurface_context import child_drain

    guard = make_child_policy_guard(parent_grants=None, profile=None)
    ctx = _ConfirmCtx()
    with child_drain(1):
        result = await guard(
            tool=_Tool("write"),
            args={"path": "x.txt", "content": "y"},
            tool_context=ctx,
        )
    # write is a sandbox-write tool -> allowed by default rules, no prompt.
    assert result is None
    assert ctx.requested == []


async def test_resurface_asks_for_terminal_when_bubble_available() -> None:
    from horizon.subagents.child_guard import make_child_policy_guard
    from horizon.subagents.resurface_context import child_drain

    guard = make_child_policy_guard(parent_grants=None, profile=None)
    ctx = _ConfirmCtx()
    with child_drain(1):
        result = await guard(
            tool=_Tool("bash"),
            args={"command": "git push --force"},
            tool_context=ctx,
        )
    # force-push is a command_safety ask -> with a bubble available it surfaces.
    assert result is not None and result.get("confirmation_required") is True
    assert len(ctx.requested) == 1


async def test_resurface_denies_terminal_when_bubble_exhausted() -> None:
    from horizon.subagents.child_guard import make_child_policy_guard
    from horizon.subagents.resurface_context import child_drain

    guard = make_child_policy_guard(parent_grants=None, profile=None)
    ctx = _ConfirmCtx()
    with child_drain(0):
        result = await guard(
            tool=_Tool("bash"),
            args={"command": "git push --force"},
            tool_context=ctx,
        )
    assert result is not None and result.get("resurface_exhausted") is True
    assert ctx.requested == []


async def test_resurface_confirmed_allows_and_writes_grant() -> None:
    from horizon.guardrails.permission_rules import read_session_grants
    from horizon.subagents.child_guard import make_child_policy_guard
    from horizon.subagents.resurface_context import child_drain

    guard = make_child_policy_guard(parent_grants=None, profile=None)
    ctx = _ConfirmCtx(tool_confirmation=SimpleNamespace(confirmed=True))
    with child_drain(0):
        result = await guard(
            tool=_Tool("bash"),
            args={"command": "git push --force"},
            tool_context=ctx,
        )
    assert result is None  # confirmed -> allowed
    grants = read_session_grants(ctx.state)
    assert any(
        g.get("toolName") == "bash"
        and any("git push" in p for p in (g.get("commandPrefix") or []))
        for g in grants
    ), grants


async def test_resurface_grant_lets_later_same_prefix_op_through() -> None:
    from horizon.subagents.child_guard import make_child_policy_guard
    from horizon.subagents.resurface_context import child_drain

    guard = make_child_policy_guard(parent_grants=None, profile=None)
    ctx = _ConfirmCtx(tool_confirmation=SimpleNamespace(confirmed=True))
    with child_drain(0):
        await guard(
            tool=_Tool("bash"),
            args={"command": "git push --force"},
            tool_context=ctx,
        )
        # later, same prefix, no confirmation, no bubble -> rides the grant.
        ctx.tool_confirmation = None
        result = await guard(
            tool=_Tool("bash"),
            args={"command": "git push --force --tags"},
            tool_context=ctx,
        )
    assert result is None


async def test_resurface_declined_denies() -> None:
    from horizon.subagents.child_guard import make_child_policy_guard
    from horizon.subagents.resurface_context import child_drain

    guard = make_child_policy_guard(parent_grants=None, profile=None)
    ctx = _ConfirmCtx(tool_confirmation=SimpleNamespace(confirmed=False))
    with child_drain(0):
        result = await guard(
            tool=_Tool("bash"),
            args={"command": "git push --force"},
            tool_context=ctx,
        )
    assert result is not None and result.get("permission_denied") is True
