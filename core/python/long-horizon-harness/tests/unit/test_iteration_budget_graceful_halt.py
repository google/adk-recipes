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

"""IterationBudgetPlugin graceful-halt handoff (request mutation only)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from google.adk.models import LlmRequest, LlmResponse

from app.conversation.graceful_halt import HANDOFF_MARKER
from app.conversation.iteration_budget_plugin import (
    _HALT_REASON_STATE_KEY,
    _HALTED_STATE_KEY,
    IterationBudgetPlugin,
)

pytestmark = pytest.mark.asyncio


def _ctx(state):
    return SimpleNamespace(state=dict(state))


async def test_first_halt_turn_strips_tools_and_injects_handoff():
    plugin = IterationBudgetPlugin()
    ctx = _ctx(
        {
            _HALTED_STATE_KEY: True,
            _HALT_REASON_STATE_KEY: "Iteration limit reached: 50",
        }
    )
    req = LlmRequest()
    req.tools_dict = {"terminal": object()}

    result = await plugin.before_model_callback(
        callback_context=ctx, llm_request=req
    )

    assert result is None
    assert req.tools_dict == {}
    tail_text = req.contents[-1].parts[0].text
    assert HANDOFF_MARKER in tail_text
    assert "Iteration limit reached: 50" in tail_text


async def test_second_halt_turn_short_circuits():
    plugin = IterationBudgetPlugin()
    ctx = _ctx(
        {
            _HALTED_STATE_KEY: True,
            _HALT_REASON_STATE_KEY: "Iteration limit reached: 50",
        }
    )
    await plugin.before_model_callback(
        callback_context=ctx, llm_request=LlmRequest()
    )
    result = await plugin.before_model_callback(
        callback_context=ctx, llm_request=LlmRequest()
    )
    assert isinstance(result, LlmResponse)


async def test_no_halt_returns_none():
    plugin = IterationBudgetPlugin()
    result = await plugin.before_model_callback(
        callback_context=_ctx({}), llm_request=LlmRequest()
    )
    assert result is None
