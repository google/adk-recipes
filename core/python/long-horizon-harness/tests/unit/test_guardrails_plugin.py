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

"""Pins the GuardrailsPlugin contract; the underlying detection logic is covered in test_repeated_failure*.py / test_no_progress*.py."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from google.adk.models import LlmResponse
from google.genai.types import Content, Part

pytestmark = pytest.mark.asyncio


def _ctx(state: dict[str, Any] | None = None) -> SimpleNamespace:
    return SimpleNamespace(state=dict(state or {}))


def _llm_response(text: str) -> LlmResponse:
    return LlmResponse(content=Content(role="model", parts=[Part(text=text)]))


# =============================================================================
# BasePlugin contract
# =============================================================================


async def test_is_base_plugin():
    from google.adk.plugins.base_plugin import BasePlugin

    from app.guardrails.guardrails_plugin import GuardrailsPlugin

    plugin = GuardrailsPlugin()
    assert isinstance(plugin, BasePlugin)
    assert plugin.name == "guardrails"


# =============================================================================
# before_model_callback — halt consumer
# =============================================================================


class TestBeforeModel:
    async def test_returns_none_when_no_halt(self):
        from google.adk.models import LlmRequest

        from app.guardrails.guardrails_plugin import GuardrailsPlugin

        plugin = GuardrailsPlugin()
        result = await plugin.before_model_callback(
            callback_context=_ctx(), llm_request=LlmRequest()
        )
        assert result is None

    async def test_first_halt_turn_does_graceful_handoff(self):
        from google.adk.models import LlmRequest

        from app.conversation.graceful_halt import HANDOFF_MARKER
        from app.guardrails.guardrails_plugin import GuardrailsPlugin
        from app.guardrails.halt_consumer import HALT_REASON_STATE_KEY

        plugin = GuardrailsPlugin()
        ctx = _ctx({HALT_REASON_STATE_KEY: "tripped"})
        req = LlmRequest()
        req.tools_dict = {"terminal": object()}

        result = await plugin.before_model_callback(
            callback_context=ctx, llm_request=req
        )

        # Lets the model run one final turn (no short-circuit), tools stripped.
        assert result is None
        assert req.tools_dict == {}
        tail_text = req.contents[-1].parts[0].text
        assert HANDOFF_MARKER in tail_text
        assert "tripped" in tail_text

    async def test_second_halt_turn_short_circuits(self):
        from google.adk.models import LlmRequest

        from app.guardrails.guardrails_plugin import GuardrailsPlugin
        from app.guardrails.halt_consumer import HALT_REASON_STATE_KEY

        plugin = GuardrailsPlugin()
        ctx = _ctx({HALT_REASON_STATE_KEY: "tripped"})

        # First turn delivers the handoff.
        await plugin.before_model_callback(
            callback_context=ctx, llm_request=LlmRequest()
        )
        # Second turn: handoff already delivered → bare short-circuit.
        result = await plugin.before_model_callback(
            callback_context=ctx, llm_request=LlmRequest()
        )
        assert isinstance(result, LlmResponse)
        text = "".join(
            p.text for p in result.content.parts if getattr(p, "text", None)
        )
        assert "halted" in text.lower()


# =============================================================================
# after_model_callback — no-progress streak
# =============================================================================


class TestAfterModel:
    async def test_increments_streak_on_identical_text(self):
        from app.guardrails.guardrails_plugin import GuardrailsPlugin
        from app.guardrails.halt_consumer import HALT_REASON_STATE_KEY

        plugin = GuardrailsPlugin(no_progress_window=3)
        ctx = _ctx()
        for _ in range(2):
            await plugin.after_model_callback(
                callback_context=ctx, llm_response=_llm_response("same answer")
            )
        # Two identical responses — below window=3, no halt yet.
        assert HALT_REASON_STATE_KEY not in ctx.state or not ctx.state.get(
            HALT_REASON_STATE_KEY
        )

        await plugin.after_model_callback(
            callback_context=ctx, llm_response=_llm_response("same answer")
        )
        assert ctx.state.get(HALT_REASON_STATE_KEY)
        assert "no progress" in ctx.state[HALT_REASON_STATE_KEY].lower()

    async def test_resets_streak_when_text_differs(self):
        from app.guardrails.guardrails_plugin import GuardrailsPlugin
        from app.guardrails.halt_consumer import HALT_REASON_STATE_KEY

        plugin = GuardrailsPlugin(no_progress_window=3)
        ctx = _ctx()
        await plugin.after_model_callback(
            callback_context=ctx, llm_response=_llm_response("first")
        )
        await plugin.after_model_callback(
            callback_context=ctx, llm_response=_llm_response("second")
        )
        await plugin.after_model_callback(
            callback_context=ctx, llm_response=_llm_response("third")
        )
        # Three distinct responses → no halt despite hitting window count.
        assert not ctx.state.get(HALT_REASON_STATE_KEY)


# =============================================================================
# after_tool_callback — repeated failure + param adapter
# =============================================================================


class TestAfterTool:
    async def test_halts_after_threshold_identical_failures(self):
        from app.guardrails.guardrails_plugin import GuardrailsPlugin
        from app.guardrails.halt_consumer import HALT_REASON_STATE_KEY

        plugin = GuardrailsPlugin(failure_threshold=3)
        ctx = _ctx()
        tool = SimpleNamespace(name="frob")

        for _ in range(3):
            await plugin.after_tool_callback(
                tool=tool,
                tool_args={"x": 1},
                tool_context=ctx,
                result={"error": "nope"},
            )

        assert ctx.state.get(HALT_REASON_STATE_KEY)
        assert "frob" in ctx.state[HALT_REASON_STATE_KEY]

    async def test_no_halt_on_success(self):
        from app.guardrails.guardrails_plugin import GuardrailsPlugin
        from app.guardrails.halt_consumer import HALT_REASON_STATE_KEY

        plugin = GuardrailsPlugin(failure_threshold=2)
        ctx = _ctx()
        tool = SimpleNamespace(name="frob")

        for _ in range(5):
            await plugin.after_tool_callback(
                tool=tool,
                tool_args={"x": 1},
                tool_context=ctx,
                result={"ok": True},
            )
        assert not ctx.state.get(HALT_REASON_STATE_KEY)

    async def test_records_last_error_via_adapter(self):
        from app.guardrails.guardrails_plugin import GuardrailsPlugin
        from app.guardrails.repeated_failure import LAST_ERROR_STATE_KEY

        plugin = GuardrailsPlugin()
        ctx = _ctx()
        await plugin.after_tool_callback(
            tool=SimpleNamespace(name="frob"),
            tool_args={},
            tool_context=ctx,
            result={"error": "disk full"},
        )
        assert ctx.state.get(LAST_ERROR_STATE_KEY) == "disk full"
