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

"""``halt_consumer_callback`` response-shape tests.

Replaces the would-be ``guardrail_halt.evalset.json`` behavioral evalset.
The evalset shape was rejected because ``halt_consumer_callback`` is wired
as a ``before_model_callback`` whose halt path is deterministic Python. An
evalset grading "what the model says when halted" would tautologically
grade Python's hardcoded string. Pinning the response shape
deterministically here is the right home: same coverage, no LLM cost.

Halt handling is two-phase. The FIRST turn after a halt latches runs a
graceful handoff: it strips tools off the per-turn ``llm_request`` and
appends a text-only handoff ``<system-reminder>``, then returns ``None``
so the model produces one final text turn. EVERY subsequent turn (handoff
already delivered) returns the bare ``[halted: <reason>]`` ``LlmResponse``.
This file pins the bare-response shape on that second-and-later path, and
the graceful-handoff mutation on the first path.

The state-setting side (RepeatedFailureGuard, NoProgressGuard) is
covered in ``test_guardrails.py``. The two halves together pin the full
contract: guards write the key, halt_consumer reads it, both agree on
the spelling.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from google.adk.models import LlmRequest

from app.guardrails import HALT_REASON_STATE_KEY
from app.guardrails.halt_consumer import (
    HALT_HANDOFF_DELIVERED_STATE_KEY,
    halt_consumer_callback,
)

pytestmark = pytest.mark.asyncio


def _fake_context(state: dict[str, Any] | None = None) -> SimpleNamespace:
    """Duck-typed CallbackContext with a mutable ``.state`` dict.

    Matches the helper in ``test_guardrails.py`` — the production code's
    only contact with the context is ``ctx.state.get(...)``.
    """
    return SimpleNamespace(state=state if state is not None else {})


def _halted_context_past_handoff(reason: str) -> SimpleNamespace:
    """Context whose halt is latched AND whose handoff was already
    delivered — i.e. the bare-short-circuit path (second turn onward)."""
    return _fake_context(
        {HALT_REASON_STATE_KEY: reason, HALT_HANDOFF_DELIVERED_STATE_KEY: True}
    )


# =============================================================================
# First halt turn → graceful handoff (strip tools, inject reminder, run once)
# =============================================================================


async def test_first_halt_turn_strips_tools_and_returns_none():
    from app.conversation.graceful_halt import HANDOFF_MARKER

    ctx = _fake_context({HALT_REASON_STATE_KEY: "tool loop detected"})
    req = LlmRequest()
    req.tools_dict = {"terminal": object()}

    response = await halt_consumer_callback(
        callback_context=ctx, llm_request=req
    )

    assert response is None, (
        "First halt turn must run the model once, not short-circuit"
    )
    assert req.tools_dict == {}
    assert HANDOFF_MARKER in req.contents[-1].parts[0].text
    assert "tool loop detected" in req.contents[-1].parts[0].text
    assert ctx.state.get(HALT_HANDOFF_DELIVERED_STATE_KEY) is True


# =============================================================================
# Second+ halt turn → bare short-circuit LlmResponse
# =============================================================================


async def test_halt_reason_set_returns_llm_response_containing_reason_verbatim():
    """After the handoff is delivered, the exact reason string set by the
    guard must appear in the returned LlmResponse's text. Substring
    containment — the production code wraps it in a ``"[halted: ...]"``
    envelope, but the verbatim reason must be present so the user-facing
    UI can surface it.
    """
    reason = (
        "terminal failed 3 times in a row with the same arguments — halting "
        "to avoid a tool loop."
    )
    ctx = _halted_context_past_handoff(reason)

    response = await halt_consumer_callback(
        callback_context=ctx, llm_request=object()
    )

    assert response is not None, (
        "Halt active (post-handoff) must short-circuit with an LlmResponse, not None"
    )
    text = "".join(
        part.text or "" for part in response.content.parts if part is not None
    )
    assert reason in text, (
        f"Reason must appear verbatim in response text. "
        f"Reason: {reason!r}, got: {text!r}"
    )


async def test_response_role_is_model():
    """Downstream consumers (Runner, UI) treat ``role=='model'`` as 'this
    came from the agent's turn'. A halt response must masquerade as a
    model turn, not a system or tool turn.
    """
    ctx = _halted_context_past_handoff("irrelevant")

    response = await halt_consumer_callback(
        callback_context=ctx, llm_request=object()
    )

    assert response is not None
    assert response.content is not None
    assert response.content.role == "model"


async def test_response_has_single_text_part():
    """One text Part — not zero (would be empty content), not many
    (downstream concatenation of multiple text parts is fragile).
    """
    ctx = _halted_context_past_handoff("anything")

    response = await halt_consumer_callback(
        callback_context=ctx, llm_request=object()
    )

    assert response is not None
    assert response.content is not None
    parts = response.content.parts
    assert len(parts) == 1, (
        f"Expected exactly 1 part, got {len(parts)}: {parts!r}"
    )
    assert parts[0].text is not None, "The single part must carry text"
    assert parts[0].text != "", "Text must not be empty"


async def test_response_has_no_function_call_parts():
    """A halted session must not appear to issue a tool call. Retrying
    the failing tool is exactly what the halt exists to prevent — if any
    Part carries a FunctionCall, the Runner would dispatch it.
    """
    ctx = _halted_context_past_handoff("anything")

    response = await halt_consumer_callback(
        callback_context=ctx, llm_request=object()
    )

    assert response is not None
    assert response.content is not None
    for part in response.content.parts:
        assert part.function_call is None, (
            f"Halt response must not carry a FunctionCall — got {part.function_call!r}"
        )


# =============================================================================
# Halt unset → pass-through
# =============================================================================


async def test_halt_unset_returns_none_so_model_runs():
    """Empty state means "no halt has fired" — the callback must return
    None so ADK's Runner proceeds to call the model normally. Inventing
    a halt here would silently brick every healthy session.
    """
    ctx = _fake_context({})

    response = await halt_consumer_callback(
        callback_context=ctx, llm_request=object()
    )

    assert response is None


async def test_halt_reason_empty_string_treated_as_unset():
    """An explicit empty string for halt_reason is indistinguishable from
    "no reason worth reporting" — the callback must not short-circuit
    with empty text, which would produce a content-less model turn. The
    production code uses ``if not reason`` for exactly this.
    """
    ctx = _fake_context({HALT_REASON_STATE_KEY: ""})

    response = await halt_consumer_callback(
        callback_context=ctx, llm_request=object()
    )

    assert response is None


async def test_unrelated_state_keys_do_not_trigger_halt():
    """The callback must key strictly on HALT_REASON_STATE_KEY, not on
    "is there anything in state". A regression that did ``if state:``
    would halt every session that had any state at all.
    """
    ctx = _fake_context(
        {
            "__repeated_failure_streak__": {"terminal:abc": 1},
            "user_id": "alice",
        }
    )

    response = await halt_consumer_callback(
        callback_context=ctx, llm_request=object()
    )

    assert response is None


# =============================================================================
# State is the source of truth — callback holds no memory of past halts
# =============================================================================


async def test_clearing_halt_reason_between_calls_makes_second_call_pass_through():
    """The halt signal lives in ``state``, not in the callback's memory.
    Once a caller clears ``state["halt_reason"]``, the next invocation
    must return None even though a previous invocation halted.

    This is the contract that lets a wrapper layer "acknowledge" a halt
    by clearing the state key — without that, the agent would stay halted
    forever after the first trigger.
    """
    state: dict[str, Any] = _halted_context_past_handoff(
        "tool loop detected"
    ).state
    ctx = _fake_context(state)

    first = await halt_consumer_callback(
        callback_context=ctx, llm_request=object()
    )
    assert first is not None, (
        "First call (halt set, post-handoff) must short-circuit"
    )

    state.pop(HALT_REASON_STATE_KEY)

    second = await halt_consumer_callback(
        callback_context=ctx, llm_request=object()
    )
    assert second is None, (
        "Second call (halt cleared) must pass through to the model — "
        "the callback must not carry sticky state across invocations"
    )


async def test_first_turn_sets_handoff_flag_only():
    """The bare-short-circuit path (post-handoff) must not further mutate
    halt state — it only reports. The single state mutation the callback
    is allowed is latching the handoff flag on the FIRST halt turn.
    """
    reason = "halted for testing"
    ctx = _halted_context_past_handoff(reason)

    await halt_consumer_callback(callback_context=ctx, llm_request=object())

    assert ctx.state == {
        HALT_REASON_STATE_KEY: reason,
        HALT_HANDOFF_DELIVERED_STATE_KEY: True,
    }, (
        f"Post-handoff callback must not mutate state further — got {ctx.state!r}"
    )


async def test_repeated_calls_post_handoff_return_equivalent_responses():
    """Once the handoff is delivered and the halt stays active, the
    callback must keep returning the bare short-circuit response — once is
    not enough. If a flake or retry re-enters the callback before the halt
    has been cleared, it must still short-circuit, not let the model run.
    """
    ctx = _halted_context_past_handoff("halted for testing")

    first = await halt_consumer_callback(
        callback_context=ctx, llm_request=object()
    )
    second = await halt_consumer_callback(
        callback_context=ctx, llm_request=object()
    )

    assert first is not None
    assert second is not None
    first_text = "".join(p.text or "" for p in first.content.parts)
    second_text = "".join(p.text or "" for p in second.content.parts)
    assert first_text == second_text


# =============================================================================
# Reason content is preserved across distinct trigger paths
# =============================================================================


@pytest.mark.parametrize(
    "reason",
    [
        # RepeatedFailureGuard-style reason (from repeated_failure.py:68-71)
        "terminal failed 3 times in a row with the same arguments — halting "
        "to avoid a tool loop.",
        # NoProgressGuard-style reason (from no_progress.py:58-61)
        "no progress detected: the model produced the same response 5 times "
        "in a row — halting to avoid a response loop.",
        # Short reason
        "x",
        # Reason with newlines and special characters
        "line one\nline two\twith\ttabs and \"quotes\" and 'apostrophes'",
        # Unicode
        "tool failed — α β γ — halting",  # noqa: RUF001
    ],
)
async def test_reason_content_preserved_for_varied_inputs(reason: str):
    """The wrapping envelope must not mangle the reason. Whatever the
    guard wrote into state must come back out intact in the response
    text — UI surfaces depend on it.
    """
    ctx = _halted_context_past_handoff(reason)

    response = await halt_consumer_callback(
        callback_context=ctx, llm_request=object()
    )

    assert response is not None
    text = "".join(part.text or "" for part in response.content.parts)
    assert reason in text, (
        f"Reason mangled in response. Input: {reason!r}, got: {text!r}"
    )
