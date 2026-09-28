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

"""select_model_callback writes the chosen model name into llm_request.model
using precedence: session.state > LHA_ROOT_MODEL env > hardcoded default."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from google.adk.models.llm_request import LlmRequest

pytestmark = pytest.mark.asyncio


def _ctx(state: dict) -> SimpleNamespace:
    return SimpleNamespace(state=state)


async def test_state_wins_over_env(monkeypatch):
    from app.models import DEFAULT_MODEL_NAME, select_model_callback
    from app.models.registry import MODEL_REGISTRY

    other = next(n for n in MODEL_REGISTRY if n != DEFAULT_MODEL_NAME)
    monkeypatch.setenv("LHA_ROOT_MODEL", other)

    req = LlmRequest()
    await select_model_callback(
        callback_context=_ctx({"selected_model": DEFAULT_MODEL_NAME}),
        llm_request=req,
    )
    assert req.model == DEFAULT_MODEL_NAME


async def test_env_used_when_state_unset(monkeypatch):
    from app.models import DEFAULT_MODEL_NAME, select_model_callback
    from app.models.registry import MODEL_REGISTRY

    other = next(n for n in MODEL_REGISTRY if n != DEFAULT_MODEL_NAME)
    monkeypatch.setenv("LHA_ROOT_MODEL", other)

    req = LlmRequest()
    await select_model_callback(callback_context=_ctx({}), llm_request=req)
    assert req.model == other


async def test_hardcoded_default_when_no_state_no_env(monkeypatch):
    from app.models import DEFAULT_MODEL_NAME, select_model_callback

    monkeypatch.delenv("LHA_ROOT_MODEL", raising=False)

    req = LlmRequest()
    await select_model_callback(callback_context=_ctx({}), llm_request=req)
    assert req.model == DEFAULT_MODEL_NAME


async def test_unknown_state_value_falls_back_to_default(monkeypatch):
    """An unknown selected_model (e.g. stale state from a removed model)
    must not break the turn — fall through to default."""
    from app.models import DEFAULT_MODEL_NAME, select_model_callback

    monkeypatch.delenv("LHA_ROOT_MODEL", raising=False)
    req = LlmRequest()
    await select_model_callback(
        callback_context=_ctx({"selected_model": "no-such-model"}),
        llm_request=req,
    )
    assert req.model == DEFAULT_MODEL_NAME


async def test_returns_none():
    from app.models import select_model_callback

    result = await select_model_callback(
        callback_context=_ctx({}), llm_request=LlmRequest()
    )
    assert result is None
