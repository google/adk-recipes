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

"""/model slash command: lists available models, switches per-session."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.asyncio


def _ctx(state: dict) -> SimpleNamespace:
    return SimpleNamespace(state=state)


async def test_registered():
    from app.commands import BUILTIN_COMMAND_REGISTRY

    assert "model" in BUILTIN_COMMAND_REGISTRY


async def test_list_shows_current_and_available(monkeypatch):
    monkeypatch.delenv("LHA_ROOT_MODEL", raising=False)
    from app.commands import BUILTIN_COMMAND_REGISTRY
    from app.models import DEFAULT_MODEL_NAME
    from app.models.registry import MODEL_REGISTRY

    handler = BUILTIN_COMMAND_REGISTRY["model"]
    out = await handler("", _ctx({}))
    assert DEFAULT_MODEL_NAME in out
    for name in MODEL_REGISTRY:
        assert name in out


async def test_list_reflects_session_override():
    from app.commands import BUILTIN_COMMAND_REGISTRY
    from app.models import DEFAULT_MODEL_NAME
    from app.models.registry import MODEL_REGISTRY

    other = next(n for n in MODEL_REGISTRY if n != DEFAULT_MODEL_NAME)
    handler = BUILTIN_COMMAND_REGISTRY["model"]
    out = await handler("", _ctx({"selected_model": other}))
    # The current marker should track the override.
    assert other in out


async def test_set_valid_model_updates_state():
    from app.commands import BUILTIN_COMMAND_REGISTRY
    from app.models import DEFAULT_MODEL_NAME
    from app.models.registry import MODEL_REGISTRY

    other = next(n for n in MODEL_REGISTRY if n != DEFAULT_MODEL_NAME)
    handler = BUILTIN_COMMAND_REGISTRY["model"]
    state: dict = {}
    out = await handler(other, _ctx(state))
    assert state["selected_model"] == other
    assert other in out


async def test_set_invalid_model_rejected():
    from app.commands import BUILTIN_COMMAND_REGISTRY

    handler = BUILTIN_COMMAND_REGISTRY["model"]
    state: dict = {}
    out = await handler("nope-not-a-model", _ctx(state))
    assert "selected_model" not in state
    assert "nope-not-a-model" in out
    assert "unknown" in out.lower() or "available" in out.lower()
