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

"""The /permissions slash command lists session grants and clears them."""

from __future__ import annotations

import pytest

from app.commands import BUILTIN_COMMAND_REGISTRY
from app.guardrails.permission_rules import PERMISSION_GRANTS_STATE_KEY

pytestmark = pytest.mark.asyncio


class _Ctx:
    def __init__(self):
        self.state: dict = {}


class _StateCtx:
    """callback_context whose .state is an ADK State stand-in (not a dict)."""

    def __init__(self):
        self.state = _FakeState()


class _FakeState:
    def __init__(self):
        self._d: dict = {}

    def get(self, key, default=None):
        return self._d.get(key, default)

    def __getitem__(self, key):
        return self._d[key]

    def __setitem__(self, key, value):
        self._d[key] = value


async def test_permissions_lists_empty():
    handler = BUILTIN_COMMAND_REGISTRY["permissions"]
    out = await handler("", _Ctx())
    assert "no" in out.lower()


async def test_permissions_lists_and_clears():
    handler = BUILTIN_COMMAND_REGISTRY["permissions"]
    ctx = _Ctx()
    ctx.state[PERMISSION_GRANTS_STATE_KEY] = [
        {
            "toolName": "terminal",
            "commandPrefix": ["bq rm"],
            "decision": "allow",
        }
    ]
    listed = await handler("", ctx)
    assert "bq rm" in listed
    cleared = await handler("clear", ctx)
    assert "cleared" in cleared.lower()
    assert ctx.state[PERMISSION_GRANTS_STATE_KEY] == []


async def test_permissions_lists_and_clears_on_non_dict_state():
    # ADK session State isn't a dict subclass; the command must still list/clear.
    handler = BUILTIN_COMMAND_REGISTRY["permissions"]
    ctx = _StateCtx()
    ctx.state[PERMISSION_GRANTS_STATE_KEY] = [
        {"toolName": "routine", "decision": "allow"}
    ]
    listed = await handler("", ctx)
    assert "routine" in listed
    await handler("clear", ctx)
    assert ctx.state[PERMISSION_GRANTS_STATE_KEY] == []
