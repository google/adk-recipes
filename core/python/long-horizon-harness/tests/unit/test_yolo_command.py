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

"""The /yolo slash command toggles approval_mode between default and yolo."""

from __future__ import annotations

import pytest

from app.commands import BUILTIN_COMMAND_REGISTRY
from app.guardrails.permission_rules import (
    APPROVAL_MODE_STATE_KEY,
    read_approval_mode,
)

pytestmark = pytest.mark.asyncio


class _Ctx:
    def __init__(self):
        self.state: dict = {}


class _FakeState:
    """ADK State stand-in: .get / __setitem__ but NOT a dict subclass."""

    def __init__(self):
        self._d: dict = {}

    def get(self, key, default=None):
        return self._d.get(key, default)

    def __getitem__(self, key):
        return self._d[key]

    def __setitem__(self, key, value):
        self._d[key] = value


async def test_yolo_toggles_to_yolo():
    handler = BUILTIN_COMMAND_REGISTRY["yolo"]
    ctx = _Ctx()
    out = await handler("", ctx)
    assert "yolo" in out.lower()
    assert ctx.state[APPROVAL_MODE_STATE_KEY] == "yolo"
    assert read_approval_mode(ctx.state) == "yolo"


async def test_yolo_toggles_back_to_default():
    handler = BUILTIN_COMMAND_REGISTRY["yolo"]
    ctx = _Ctx()
    ctx.state[APPROVAL_MODE_STATE_KEY] = "yolo"
    out = await handler("", ctx)
    assert "default" in out.lower()
    assert ctx.state[APPROVAL_MODE_STATE_KEY] == "default"
    assert read_approval_mode(ctx.state) == "default"


async def test_yolo_on_non_dict_state():
    handler = BUILTIN_COMMAND_REGISTRY["yolo"]
    ctx = _Ctx()
    ctx.state = _FakeState()
    await handler("", ctx)
    assert ctx.state[APPROVAL_MODE_STATE_KEY] == "yolo"
    await handler("", ctx)
    assert ctx.state[APPROVAL_MODE_STATE_KEY] == "default"
