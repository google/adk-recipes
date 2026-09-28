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

"""Halt-state clearing at the user-turn boundary.

Pairs with ``test_halt_consumer_response_shape.py``: that file pins the
read-only halt *consumer*, this file pins the wrapper that *acknowledges*
the halt. Together they prevent the latch bug where ``halt_reason``,
once set, would brick the session forever — every subsequent model call
short-circuits, and the streak counters stay at-threshold so even
clearing only the reason would re-trip the halt on the next event.

The acknowledgment runs in ``on_session_start_callback`` (wired as
``before_agent_callback``, fires once per user-turn invocation). The
contract enforced here:

1. ``clear_halt_state`` clears all three signals: ``halt_reason``, the
   per-tool failure streak, and the no-progress streak + last-text.
2. After ``clear_halt_state``, ``halt_consumer_callback`` returns
   ``None`` (the model is allowed to run).
3. After ``clear_halt_state``, a single subsequent failure does NOT
   re-trip ``RepeatedFailureGuard`` — proving the streak counter was
   actually reset, not just the reason.
4. After ``clear_halt_state``, a single subsequent identical response
   does NOT re-trip ``NoProgressGuard`` — same reasoning.
5. ``on_session_start_callback`` calls ``clear_halt_state`` on every
   invocation (not just the first), so subsequent user turns unbrick.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.guardrails import (
    HALT_REASON_STATE_KEY,
    NoProgressGuard,
    RepeatedFailureGuard,
    clear_halt_state,
    halt_consumer_callback,
)
from app.guardrails.no_progress import (
    _LAST_TEXT_STATE_KEY as NP_LAST_TEXT_KEY,
)
from app.guardrails.no_progress import _STREAK_STATE_KEY as NP_STREAK_KEY
from app.guardrails.repeated_failure import (
    _STREAK_STATE_KEY as RF_STREAK_KEY,
)


def _ctx(state: dict[str, Any] | None = None) -> SimpleNamespace:
    return SimpleNamespace(state=state if state is not None else {})


# =============================================================================
# clear_halt_state — the user-turn-boundary helper
# =============================================================================


def test_clear_halt_state_resets_halt_reason():
    state: dict[str, Any] = {HALT_REASON_STATE_KEY: "tool loop"}

    clear_halt_state(state)

    assert not state.get(HALT_REASON_STATE_KEY)


def test_clear_halt_state_resets_failure_streak():
    state: dict[str, Any] = {
        RF_STREAK_KEY: {"terminal:abc": {"count": 3, "last_failure_at": 0.0}},
    }

    clear_halt_state(state)

    assert not state.get(RF_STREAK_KEY)


def test_clear_halt_state_resets_no_progress_streak():
    state: dict[str, Any] = {
        NP_STREAK_KEY: 5,
        NP_LAST_TEXT_KEY: "stuck text",
    }

    clear_halt_state(state)

    assert state.get(NP_STREAK_KEY) == 0
    assert state.get(NP_LAST_TEXT_KEY) is None


def test_clear_halt_state_is_idempotent_on_empty_state():
    """Calling on a session that never halted must not introduce keys
    that change other callbacks' behavior. The reset values must be
    semantically equivalent to "absent" for every reader."""
    state: dict[str, Any] = {}

    clear_halt_state(state)

    # halt_consumer treats None and "" the same as absent.
    assert not state.get(HALT_REASON_STATE_KEY)
    # repeated_failure._load_streaks coerces non-dict to {}.
    assert not state.get(RF_STREAK_KEY)
    # NoProgressGuard does `streak += 1` after .get(..., 0), so a stored
    # 0 must round-trip identically to a missing key.
    assert state.get(NP_STREAK_KEY) == 0


# =============================================================================
# After clear, the consumer pass-through and the guards start from zero
# =============================================================================


@pytest.mark.asyncio
async def test_consumer_returns_none_after_clear():
    state: dict[str, Any] = {HALT_REASON_STATE_KEY: "tool loop"}
    ctx = _ctx(state)

    clear_halt_state(state)
    response = await halt_consumer_callback(
        callback_context=ctx, llm_request=object()
    )

    assert response is None, (
        "After acknowledgment, halt_consumer must let the model run"
    )


@pytest.mark.asyncio
async def test_repeated_failure_guard_does_not_re_halt_after_clear():
    """The streak counter is the real footgun: if it's still at threshold
    after clearing only the reason, the very next failure re-trips the
    halt and the user sees no difference. This test pins that the streak
    is genuinely reset."""
    guard = RepeatedFailureGuard(threshold=3)
    state: dict[str, Any] = {
        HALT_REASON_STATE_KEY: "terminal failed 3 times in a row …",
        RF_STREAK_KEY: {
            # Exact signature that the next failing call will produce.
            # Pre-loaded at threshold so a stale streak would re-trip
            # immediately.
            _signature("terminal", {"cmd": "curl example.com"}): {
                "count": 3,
                "last_failure_at": 9_999_999_999.0,  # not stale yet
            },
        },
    }

    clear_halt_state(state)

    tool = SimpleNamespace(name="terminal")
    await guard.after_tool_callback(
        tool=tool,
        args={"cmd": "curl example.com"},
        tool_response={"error": "503"},
        tool_context=_ctx(state),
    )

    # One post-clear failure → count should be 1, not 4. Halt must stay clear.
    assert not state.get(HALT_REASON_STATE_KEY), (
        "Failure streak was not actually reset — halt re-tripped on first "
        "post-acknowledgment failure"
    )


@pytest.mark.asyncio
async def test_no_progress_guard_does_not_re_halt_after_clear():
    guard = NoProgressGuard(window=3)
    state: dict[str, Any] = {
        HALT_REASON_STATE_KEY: "no progress detected …",
        NP_STREAK_KEY: 3,
        NP_LAST_TEXT_KEY: "I am stuck",
    }

    clear_halt_state(state)

    from google.adk.models import LlmResponse
    from google.genai.types import Content, Part

    response = LlmResponse(
        content=Content(role="model", parts=[Part(text="I am stuck")])
    )
    await guard.after_model_callback(
        callback_context=_ctx(state),
        llm_response=response,
    )

    # Without the reset, previous == normalized so streak = 4 ≥ window → halt.
    # With the reset, last_text was None so streak = 1, no halt.
    assert not state.get(HALT_REASON_STATE_KEY), (
        "No-progress streak was not actually reset — halt re-tripped on "
        "first post-acknowledgment response"
    )


# =============================================================================
# on_session_start_callback wires clear_halt_state on every turn
# =============================================================================


@pytest.mark.asyncio
async def test_on_session_start_clears_halt_on_subsequent_invocation():
    """The bug being fixed: on_session_start_callback's first-invocation
    guard (SESSION_STARTED_STATE_KEY) was returning early before any
    halt-clearing could happen on later turns. The clear must run on
    every invocation, not just the first."""
    from app.conversation.session_start import (
        SESSION_STARTED_STATE_KEY,
        on_session_start_callback,
    )

    # Session has already started (prior turns). Halt fired on a prior
    # turn. User is now sending a new message — this invocation is what
    # should unbrick.
    state: dict[str, Any] = {
        SESSION_STARTED_STATE_KEY: True,
        HALT_REASON_STATE_KEY: "tool loop from a prior turn",
        RF_STREAK_KEY: {"terminal:abc": {"count": 3, "last_failure_at": 0.0}},
    }
    ctx = SimpleNamespace(user_id="u1", state=state)

    # Stub the env/sandbox/compaction side-effects — orthogonal to halt clearing.
    with (
        patch(
            "app.conversation.session_start._ensure_environment",
            new=AsyncMock(return_value=SimpleNamespace()),
        ),
        patch(
            "app.conversation.session_start._refresh_sandbox_auth",
            new=AsyncMock(return_value=None),
        ),
        patch("app.conversation.session_start.set_active_environment"),
        patch("app.conversation.session_start._bind_compaction_ctx_from"),
    ):
        await on_session_start_callback(ctx)

    assert not state.get(HALT_REASON_STATE_KEY), (
        "on_session_start_callback must clear halt_reason on every "
        "invocation, not just the first — otherwise halt latches forever"
    )
    assert not state.get(RF_STREAK_KEY), (
        "Streak counter must also clear, else next failure re-trips halt"
    )
    # First-invocation guard still works: SESSION_STARTED_STATE_KEY stays True.
    assert state.get(SESSION_STARTED_STATE_KEY) is True


# Local copy of the signature helper so this test doesn't import a private
# symbol that may move; the algorithm is small and stable enough to duplicate.
def _signature(tool_name: str, args: dict[str, Any]) -> str:
    import hashlib
    import json

    canonical = json.dumps(
        args,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{tool_name}:{digest}"
