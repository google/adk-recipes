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

"""Headless routine mode: a shell ask_user runs in the isolated sandbox (allow);
a non-shell ask_user stays fail-closed (deny). Explicit allow/deny unchanged."""

from __future__ import annotations

import json

import pytest

from horizon.environment_context import set_active_environment
from horizon.guardrails.permission_guard import (
    permission_guard,
    reset_headless_mode,
    set_headless_mode,
)
from horizon.guardrails.permission_rules import PERMISSION_OVERLAY_FILENAME
from tests.stubs import FakeTool, FakeToolContext

pytestmark = pytest.mark.asyncio

_Tool = FakeTool
_Ctx = FakeToolContext


@pytest.fixture(autouse=True)
def _env(tmp_path):
    from horizon.environment import LocalEnvironment

    env = LocalEnvironment(working_dir=tmp_path)
    env._working_dir = tmp_path
    set_active_environment(env)
    yield tmp_path


async def test_headless_shell_ask_now_allowed():
    """A mutating shell command that would normally prompt runs unattended — it is
    confined to the routine's own isolated sandbox."""
    token = set_headless_mode(True)
    try:
        ctx = _Ctx()
        res = await permission_guard(
            tool=_Tool("bash"),
            args={"command": "git clone https://example.com/repo"},
            tool_context=ctx,
        )
        assert res is None
        assert ctx._requested is None  # never dispatched a confirmation
    finally:
        reset_headless_mode(token)


async def test_headless_process_write_ask_now_allowed():
    token = set_headless_mode(True)
    try:
        ctx = _Ctx()
        res = await permission_guard(
            tool=_Tool("process"),
            args={"action": "write", "data": "make build"},
            tool_context=ctx,
        )
        assert res is None
    finally:
        reset_headless_mode(token)


async def test_headless_nonshell_ask_becomes_deny_without_prompting():
    token = set_headless_mode(True)
    try:
        ctx = _Ctx()
        res = await permission_guard(
            tool=_Tool("send_email"), args={"to": "x@y"}, tool_context=ctx
        )
        assert res is not None
        assert res.get("permission_denied") is True
        assert res.get("headless_denied") is True
        assert ctx._requested is None  # never dispatched a confirmation
    finally:
        reset_headless_mode(token)


async def test_headless_allow_still_allowed():
    token = set_headless_mode(True)
    try:
        ctx = _Ctx()
        res = await permission_guard(
            tool=_Tool("bash"), args={"command": "ls -la"}, tool_context=ctx
        )
        assert res is None
    finally:
        reset_headless_mode(token)


async def test_headless_hard_deny_rule_still_denies_not_headless(_env):
    rule = {
        "toolName": "bash",
        "commandPrefix": "bq rm",
        "decision": "deny",
        "denyMessage": "blocked by policy",
    }
    (_env / PERMISSION_OVERLAY_FILENAME).parent.mkdir(
        parents=True, exist_ok=True
    )
    (_env / PERMISSION_OVERLAY_FILENAME).write_text(
        json.dumps(rule) + "\n", encoding="utf-8"
    )
    token = set_headless_mode(True)
    try:
        ctx = _Ctx()
        res = await permission_guard(
            tool=_Tool("bash"),
            args={"command": "bq rm ds"},
            tool_context=ctx,
        )
        assert res is not None
        assert res.get("permission_denied") is True
        assert (
            res.get("headless_denied") is None
        )  # deny-rule path, not the headless path
        assert "blocked by policy" in res.get("error", "")
    finally:
        reset_headless_mode(token)


async def test_non_headless_ask_still_dispatches_confirmation():
    ctx = _Ctx()
    res = await permission_guard(
        tool=_Tool("bash"), args={"command": "bq rm ds"}, tool_context=ctx
    )
    assert res is not None
    assert res.get("confirmation_required") is True
    assert ctx._requested is not None


async def test_headless_does_not_reach_destructive_blocked_shell():
    """The shell allow-branch is safe only because the destructive policy guard
    runs ahead of it in the chain and is headless-independent: it blocks a
    destructive command whether or not headless mode is set."""
    from horizon.guardrails.policies import policies_guard

    args = {"command": "rm -rf /"}
    blocked_plain = await policies_guard(
        tool=_Tool("bash"), args=args, tool_context=_Ctx()
    )
    assert blocked_plain is not None

    token = set_headless_mode(True)
    try:
        blocked_headless = await policies_guard(
            tool=_Tool("bash"), args=args, tool_context=_Ctx()
        )
        assert blocked_headless is not None
    finally:
        reset_headless_mode(token)
