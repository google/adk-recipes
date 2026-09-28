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

"""IterationBudgetPlugin — session.state mirroring contract.

The plugin's primary state (the IterationBudget instance) lives in a
sidecar dict keyed by (user_id, session_id) because IterationBudget is
not JSON-serializable. But the system_prompt volatile tier reads
``state["iteration"]`` — so the plugin MUST mirror the iteration count
into ``session.state["iteration"]`` after each ``record_iteration()``.

These tests pin only the mirroring contract — broader plugin behavior
(halt semantics, on_event tool-call counting) is covered by
``tests/integration/test_conversation_loop_runtime.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from google.adk.events import Event
from google.genai import types

from app.conversation.iteration_budget_plugin import (
    ITERATION_STATE_KEY,
    IterationBudgetPlugin,
)


def _fake_invocation_context(
    *, user_id: str = "alice", session_id: str = "s1"
) -> SimpleNamespace:
    """Duck-typed InvocationContext exposing only the fields the plugin reads.

    Real `InvocationContext.user_id` is a property that derives from
    `session.user_id`; the plugin reads both `ctx.user_id` and
    `ctx.session.id` / `ctx.session.state`. A flat SimpleNamespace
    satisfies both.
    """
    state: dict[str, Any] = {}
    session = SimpleNamespace(id=session_id, state=state, user_id=user_id)
    return SimpleNamespace(user_id=user_id, session=session)


def _fake_callback_context(state: dict[str, Any]) -> SimpleNamespace:
    """Duck-typed CallbackContext exposing only `.state`, which is all the
    plugin's `before_model_callback` reads."""
    return SimpleNamespace(state=state)


async def _consume_bare_halt(
    plugin: IterationBudgetPlugin, state: dict[str, Any]
):
    """Drive past the one-shot graceful handoff and return the bare halt.

    The first before_model_callback on a halted budget runs the graceful
    handoff (mutates the request, returns None); the second returns the
    bare [halted: ...] LlmResponse.
    """
    from google.adk.models import LlmRequest

    await plugin.before_model_callback(
        callback_context=_fake_callback_context(state), llm_request=LlmRequest()
    )
    return await plugin.before_model_callback(
        callback_context=_fake_callback_context(state), llm_request=LlmRequest()
    )


def test_iteration_state_key_is_a_documented_constant():
    """Production code and tests must agree on the spelling — the volatile
    tier reads ``state["iteration"]`` and the plugin writes the same key."""
    assert ITERATION_STATE_KEY == "iteration"


@pytest.mark.asyncio
async def test_before_run_mirrors_iteration_to_session_state():
    """After before_run_callback succeeds, session.state["iteration"] must
    equal the budget's iterations_used."""
    plugin = IterationBudgetPlugin(max_iterations=10)
    ctx = _fake_invocation_context()

    assert "iteration" not in ctx.session.state, "sanity: starts unset"

    result = await plugin.before_run_callback(invocation_context=ctx)
    assert result is None, "no halt expected — plenty of budget"

    assert ctx.session.state["iteration"] == 1


@pytest.mark.asyncio
async def test_iteration_mirror_increments_across_runs():
    """Three consecutive before_run_callback calls on the same session
    leave state["iteration"] == 3."""
    plugin = IterationBudgetPlugin(max_iterations=10)
    ctx = _fake_invocation_context()

    for expected in (1, 2, 3):
        await plugin.before_run_callback(invocation_context=ctx)
        assert ctx.session.state["iteration"] == expected


@pytest.mark.asyncio
async def test_iteration_mirror_is_per_session():
    """Two different (user_id, session_id) keys must each track their own
    iteration count in their own session.state."""
    plugin = IterationBudgetPlugin(max_iterations=10)
    ctx_a = _fake_invocation_context(user_id="alice", session_id="s1")
    ctx_b = _fake_invocation_context(user_id="bob", session_id="s2")

    await plugin.before_run_callback(invocation_context=ctx_a)
    await plugin.before_run_callback(invocation_context=ctx_a)
    await plugin.before_run_callback(invocation_context=ctx_b)

    assert ctx_a.session.state["iteration"] == 2
    assert ctx_b.session.state["iteration"] == 1


def _function_call_event(*call_ids: str) -> Event:
    return Event(
        invocation_id="inv-test",
        author="root_agent",
        content=types.Content(
            role="model",
            parts=[
                types.Part(
                    function_call=types.FunctionCall(
                        id=call_id, name="terminal", args={"command": "ls"}
                    )
                )
                for call_id in call_ids
            ],
        ),
    )


@pytest.mark.asyncio
async def test_on_event_does_not_replace_function_call_event_on_overflow():
    """When the per-iteration tool-call cap trips, on_event_callback MUST
    return None (let the original event through) — not a synthetic halt event.

    Replacing a function_call event with a halt event removes the call from
    session.events while ADK keeps the corresponding function_response
    events (the tools have already been dispatched upstream of this hook).
    The next turn's contents preprocessor walks events backwards looking
    for the call event matching the response IDs, can't find it, and
    raises 'No function call event found for function responses ids: {...}'.
    The user-visible halt envelope is surfaced on the NEXT turn by
    before_model_callback, which short-circuits the model when halted.
    """
    plugin = IterationBudgetPlugin(
        max_iterations=10, max_tool_calls_per_iteration=2
    )
    ctx = _fake_invocation_context()

    # Burn the 2-call budget across two single-call events.
    for call_id in ("call-1", "call-2"):
        result = await plugin.on_event_callback(
            invocation_context=ctx, event=_function_call_event(call_id)
        )
        assert result is None, "under-cap events must pass through untouched"

    # The 3rd call overflows. The plugin must still return None and halt
    # the budget — not return a halt event that would orphan the response.
    overflow_event = _function_call_event("call-3")
    result = await plugin.on_event_callback(
        invocation_context=ctx, event=overflow_event
    )
    assert result is None, (
        "overflow event must NOT be replaced — orphans function_response "
        "events and breaks the next turn's content preprocessor"
    )
    assert ctx.session.state.get("_iteration_budget_halted") is True

    # The halt envelope is surfaced on the next model turn via before_model
    # (after the one-shot graceful handoff turn).
    response = await _consume_bare_halt(plugin, ctx.session.state)
    assert response is not None
    text = " ".join(
        part.text
        for part in (response.content.parts if response.content else [])
        if part.text
    )
    assert "[halted:" in text
    assert "Tool-call limit reached" in text


@pytest.mark.asyncio
async def test_on_event_handles_batched_function_calls_above_cap():
    """A single event may contain multiple function calls (Gemini parallel
    calls). When the batch crosses the cap mid-loop, the plugin must still
    return None and halt — never partially-process and return a halt event."""
    plugin = IterationBudgetPlugin(
        max_iterations=10, max_tool_calls_per_iteration=2
    )
    ctx = _fake_invocation_context()

    # 4 calls in one event, cap is 2.
    batch = _function_call_event("c1", "c2", "c3", "c4")
    result = await plugin.on_event_callback(invocation_context=ctx, event=batch)

    assert result is None
    assert ctx.session.state.get("_iteration_budget_halted") is True


@pytest.mark.asyncio
async def test_iteration_mirror_not_written_when_budget_pre_halted():
    """If the budget is already halted before this run, the plugin returns
    a halt Content WITHOUT calling record_iteration — state["iteration"]
    must NOT be bumped past the last valid value."""
    plugin = IterationBudgetPlugin(max_iterations=10)
    ctx = _fake_invocation_context()

    # One real iteration, then pre-halt via the live state-bound view.
    await plugin.before_run_callback(invocation_context=ctx)
    assert ctx.session.state["iteration"] == 1

    plugin._budget_for(ctx).halt("test-only halt")

    # Next before_run must leave iteration unchanged (no record_iteration).
    await plugin.before_run_callback(invocation_context=ctx)
    assert ctx.session.state["iteration"] == 1

    # And before_model_callback must surface the halt envelope (after the
    # one-shot graceful handoff turn).
    response = await _consume_bare_halt(plugin, ctx.session.state)
    assert response is not None, "pre-halted run must return halt LlmResponse"
    text = " ".join(
        part.text
        for part in (response.content.parts if response.content else [])
        if part.text
    )
    assert "[halted:" in text
