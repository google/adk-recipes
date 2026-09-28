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

"""Tool-guardrail callback tests.

Pins three callback-shaped guardrail components in ``app.guardrails``:

* ``RepeatedFailureGuard`` — ``after_tool_callback`` that sets
  ``session.state["halt_reason"]`` when the SAME (tool_name, canonical
  args, error class) fails ``threshold`` times in a row. Success on the
  same signature resets the streak. Different args do NOT count toward
  the same streak.

* ``NoProgressGuard`` — ``after_model_callback`` that sets
  ``halt_reason`` when the last ``window`` LLM responses are textually
  identical (whitespace-normalized). Differing responses reset the
  streak.

* ``halt_consumer_callback`` — ``before_model_callback`` that
  short-circuits with an ``LlmResponse`` if ``halt_reason`` is already
  present in state, so the model is never called for a halted session.

These tests assert on CODE BEHAVIOR — return values, state mutations,
and call counts on stubs. They never assert on LLM-generated text. The
LLM-side recovery behavior lives in
``tests/eval/evalsets/guardrail_recovery.evalset.json``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from google.adk.models import LlmResponse
from google.genai.types import Content, Part


def _fake_context(state: dict[str, Any] | None = None) -> SimpleNamespace:
    """Duck-typed CallbackContext/ToolContext with a mutable ``.state`` dict.

    ADK's real Context exposes ``.state`` as a ``State`` object that
    behaves like a mapping. A plain dict satisfies the production code's
    ``ctx.state["halt_reason"] = ...`` / ``ctx.state.get("halt_reason")``
    contract without spinning up a full InvocationContext.
    """
    return SimpleNamespace(state=state if state is not None else {})


def _model_response(text: str) -> LlmResponse:
    return LlmResponse(content=Content(role="model", parts=[Part(text=text)]))


# =============================================================================
# Module surface
# =============================================================================


def test_module_exposes_three_guardrail_callbacks():
    from app.guardrails import (
        NoProgressGuard,
        RepeatedFailureGuard,
        halt_consumer_callback,
    )

    assert callable(RepeatedFailureGuard)
    assert callable(NoProgressGuard)
    assert callable(halt_consumer_callback)


def test_halt_reason_state_key_is_documented_constant():
    """Production code and tests must agree on the state key spelling.

    Exposing it as a module constant prevents silent drift between the
    setter (guards) and the reader (halt_consumer_callback).
    """
    from app.guardrails import HALT_REASON_STATE_KEY

    assert HALT_REASON_STATE_KEY == "halt_reason"


def test_last_error_state_key_is_documented_constant():
    """The system_prompt volatile tier reads ``state["last_error"]`` —
    RepeatedFailureGuard writes the same key. A module-level constant
    keeps the setter and reader from drifting apart."""
    from app.guardrails import LAST_ERROR_STATE_KEY

    assert LAST_ERROR_STATE_KEY == "last_error"


# =============================================================================
# RepeatedFailureGuard
# =============================================================================


class TestRepeatedFailureGuard:
    def test_keyword_only_threshold_with_default(self):
        from app.guardrails import RepeatedFailureGuard

        # Default keeps test suites honest about the production threshold.
        guard = RepeatedFailureGuard()
        assert guard.threshold == 3

        custom = RepeatedFailureGuard(threshold=5)
        assert custom.threshold == 5

    def test_threshold_must_be_positional_keyword_only(self):
        from app.guardrails import RepeatedFailureGuard

        with pytest.raises(TypeError):
            RepeatedFailureGuard(3)

    def test_threshold_must_be_at_least_two(self):
        """A threshold of 1 would halt on the very first failure, which is
        indistinguishable from no retry budget at all."""
        from app.guardrails import RepeatedFailureGuard

        with pytest.raises(ValueError):
            RepeatedFailureGuard(threshold=1)
        with pytest.raises(ValueError):
            RepeatedFailureGuard(threshold=0)

    @pytest.mark.asyncio
    async def test_first_failure_does_not_halt(self):
        from app.guardrails import (
            HALT_REASON_STATE_KEY,
            RepeatedFailureGuard,
        )

        guard = RepeatedFailureGuard(threshold=3)
        ctx = _fake_context()

        await guard.after_tool_callback(
            tool=SimpleNamespace(name="web_search"),
            args={"query": "boom"},
            tool_response={"error": "upstream 500"},
            tool_context=ctx,
        )

        assert HALT_REASON_STATE_KEY not in ctx.state

    @pytest.mark.asyncio
    async def test_n_identical_failures_set_halt_reason(self):
        from app.guardrails import (
            HALT_REASON_STATE_KEY,
            RepeatedFailureGuard,
        )

        guard = RepeatedFailureGuard(threshold=3)
        ctx = _fake_context()

        for _ in range(3):
            await guard.after_tool_callback(
                tool=SimpleNamespace(name="web_search"),
                args={"query": "boom"},
                tool_response={"error": "upstream 500"},
                tool_context=ctx,
            )

        halt = ctx.state[HALT_REASON_STATE_KEY]
        # Halt reason must name the offending tool so downstream
        # consumers (halt_consumer_callback message, eval inspectors)
        # can surface a useful message.
        assert "web_search" in str(halt)

    @pytest.mark.asyncio
    async def test_threshold_minus_one_failures_do_not_halt(self):
        from app.guardrails import (
            HALT_REASON_STATE_KEY,
            RepeatedFailureGuard,
        )

        guard = RepeatedFailureGuard(threshold=4)
        ctx = _fake_context()

        for _ in range(3):
            await guard.after_tool_callback(
                tool=SimpleNamespace(name="web_search"),
                args={"query": "boom"},
                tool_response={"error": "upstream 500"},
                tool_context=ctx,
            )

        assert HALT_REASON_STATE_KEY not in ctx.state

    @pytest.mark.asyncio
    async def test_different_args_do_not_count_toward_same_streak(self):
        """The point of canonical-args matching is to catch a loop where
        the model retries the EXACT same call. Different args means the
        model is exploring, not stuck — must not halt.
        """
        from app.guardrails import (
            HALT_REASON_STATE_KEY,
            RepeatedFailureGuard,
        )

        guard = RepeatedFailureGuard(threshold=3)
        ctx = _fake_context()

        for i in range(5):
            await guard.after_tool_callback(
                tool=SimpleNamespace(name="terminal"),
                args={"command": f"ls /tmp/path-{i}"},
                tool_response={"exit_code": 1, "stderr": "not found"},
                tool_context=ctx,
            )

        assert HALT_REASON_STATE_KEY not in ctx.state

    @pytest.mark.asyncio
    async def test_canonical_args_match_ignores_key_order(self):
        """``{"a": 1, "b": 2}`` and ``{"b": 2, "a": 1}`` must hash to the
        same signature — otherwise the streak silently resets on every
        differently-ordered call.
        """
        from app.guardrails import (
            HALT_REASON_STATE_KEY,
            RepeatedFailureGuard,
        )

        guard = RepeatedFailureGuard(threshold=2)
        ctx = _fake_context()

        await guard.after_tool_callback(
            tool=SimpleNamespace(name="web_search"),
            args={"a": 1, "b": 2, "c": 3},
            tool_response={"error": "boom"},
            tool_context=ctx,
        )
        await guard.after_tool_callback(
            tool=SimpleNamespace(name="web_search"),
            args={"c": 3, "b": 2, "a": 1},
            tool_response={"error": "boom"},
            tool_context=ctx,
        )

        assert HALT_REASON_STATE_KEY in ctx.state

    @pytest.mark.asyncio
    async def test_success_between_failures_resets_streak(self):
        from app.guardrails import (
            HALT_REASON_STATE_KEY,
            RepeatedFailureGuard,
        )

        guard = RepeatedFailureGuard(threshold=3)
        ctx = _fake_context()

        for _ in range(2):
            await guard.after_tool_callback(
                tool=SimpleNamespace(name="web_search"),
                args={"query": "boom"},
                tool_response={"error": "upstream 500"},
                tool_context=ctx,
            )
        await guard.after_tool_callback(
            tool=SimpleNamespace(name="web_search"),
            args={"query": "boom"},
            tool_response={"results": ["ok"]},
            tool_context=ctx,
        )
        for _ in range(2):
            await guard.after_tool_callback(
                tool=SimpleNamespace(name="web_search"),
                args={"query": "boom"},
                tool_response={"error": "upstream 500"},
                tool_context=ctx,
            )

        # 2 fail + 1 ok + 2 fail = streak of 2 at the end, below
        # threshold=3.
        assert HALT_REASON_STATE_KEY not in ctx.state

    @pytest.mark.asyncio
    async def test_different_tools_track_independent_streaks(self):
        from app.guardrails import (
            HALT_REASON_STATE_KEY,
            RepeatedFailureGuard,
        )

        guard = RepeatedFailureGuard(threshold=3)
        ctx = _fake_context()

        # 2 failures of web_search + 2 failures of terminal — neither
        # has hit the threshold on its own.
        for _ in range(2):
            await guard.after_tool_callback(
                tool=SimpleNamespace(name="web_search"),
                args={"query": "boom"},
                tool_response={"error": "boom"},
                tool_context=ctx,
            )
        for _ in range(2):
            await guard.after_tool_callback(
                tool=SimpleNamespace(name="terminal"),
                args={"command": "false"},
                tool_response={"exit_code": 1},
                tool_context=ctx,
            )

        assert HALT_REASON_STATE_KEY not in ctx.state

    @pytest.mark.asyncio
    async def test_failure_classification_terminal_exit_code(self):
        """A terminal result with non-zero exit_code is a failure for
        streak purposes; exit_code 0 is success.
        """
        from app.guardrails import (
            HALT_REASON_STATE_KEY,
            RepeatedFailureGuard,
        )

        guard = RepeatedFailureGuard(threshold=2)
        ctx = _fake_context()

        await guard.after_tool_callback(
            tool=SimpleNamespace(name="terminal"),
            args={"command": "false"},
            tool_response={"exit_code": 1},
            tool_context=ctx,
        )
        await guard.after_tool_callback(
            tool=SimpleNamespace(name="terminal"),
            args={"command": "false"},
            tool_response={"exit_code": 0},
            tool_context=ctx,
        )
        await guard.after_tool_callback(
            tool=SimpleNamespace(name="terminal"),
            args={"command": "false"},
            tool_response={"exit_code": 1},
            tool_context=ctx,
        )

        # Streak: fail, reset by success, fail. End-state streak = 1.
        assert HALT_REASON_STATE_KEY not in ctx.state

    @pytest.mark.asyncio
    async def test_failure_classification_generic_error_key(self):
        """A response dict containing an ``"error"`` key counts as a
        failure regardless of tool name."""
        from app.guardrails import (
            HALT_REASON_STATE_KEY,
            RepeatedFailureGuard,
        )

        guard = RepeatedFailureGuard(threshold=2)
        ctx = _fake_context()

        for _ in range(2):
            await guard.after_tool_callback(
                tool=SimpleNamespace(name="custom_tool"),
                args={"x": 1},
                tool_response={"error": "anything"},
                tool_context=ctx,
            )

        assert HALT_REASON_STATE_KEY in ctx.state

    @pytest.mark.asyncio
    async def test_returns_none_so_adk_uses_real_tool_response(self):
        """ADK's after_tool_callback semantics: returning ``None`` keeps
        the original tool result; returning a value REPLACES it.

        The guardrail must never replace the result — it only observes
        and sets halt_reason. Otherwise it would silently mask tool
        output the model needs to see.
        """
        from app.guardrails import RepeatedFailureGuard

        guard = RepeatedFailureGuard(threshold=2)
        ctx = _fake_context()

        result = await guard.after_tool_callback(
            tool=SimpleNamespace(name="web_search"),
            args={"query": "boom"},
            tool_response={"error": "boom"},
            tool_context=ctx,
        )
        assert result is None

        # Even on the call that triggers the halt — the response still
        # passes through unmodified.
        result_at_halt = await guard.after_tool_callback(
            tool=SimpleNamespace(name="web_search"),
            args={"query": "boom"},
            tool_response={"error": "boom"},
            tool_context=ctx,
        )
        assert result_at_halt is None

    @pytest.mark.asyncio
    async def test_stale_failures_decay_and_do_not_count_toward_halt(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """A failure from ten minutes ago must not stack with one happening
        now — the model has moved on and the old streak is no longer
        evidence of a stuck loop.
        """
        from app.guardrails import (
            HALT_REASON_STATE_KEY,
            RepeatedFailureGuard,
            repeated_failure,
        )

        guard = RepeatedFailureGuard(threshold=3)
        ctx = _fake_context()

        clock = {"now": 1_000_000.0}
        monkeypatch.setattr(repeated_failure.time, "time", lambda: clock["now"])

        # Two failures at t=0.
        for _ in range(2):
            await guard.after_tool_callback(
                tool=SimpleNamespace(name="web_search"),
                args={"query": "boom"},
                tool_response={"error": "upstream 500"},
                tool_context=ctx,
            )

        # 11 minutes later — past the 10-minute decay window — a third
        # identical failure happens. The old two have expired, so this
        # is treated as a fresh streak of 1 and must not halt.
        clock["now"] += 11 * 60
        await guard.after_tool_callback(
            tool=SimpleNamespace(name="web_search"),
            args={"query": "boom"},
            tool_response={"error": "upstream 500"},
            tool_context=ctx,
        )

        assert HALT_REASON_STATE_KEY not in ctx.state

    @pytest.mark.asyncio
    async def test_does_not_overwrite_existing_halt_reason(self):
        """If another guard already set a halt_reason this turn, this
        guard must not clobber it — the FIRST halt reason wins so the
        user sees the proximate cause, not the later cascade."""
        from app.guardrails import (
            HALT_REASON_STATE_KEY,
            RepeatedFailureGuard,
        )

        guard = RepeatedFailureGuard(threshold=2)
        ctx = _fake_context({HALT_REASON_STATE_KEY: "pre-existing"})

        for _ in range(3):
            await guard.after_tool_callback(
                tool=SimpleNamespace(name="web_search"),
                args={"query": "boom"},
                tool_response={"error": "boom"},
                tool_context=ctx,
            )

        assert ctx.state[HALT_REASON_STATE_KEY] == "pre-existing"

    @pytest.mark.asyncio
    async def test_failure_mirrors_last_error_to_state(self):
        """Every failure must mirror the error string to
        ``state["last_error"]`` so the system_prompt volatile tier can
        surface the most recent failure to the model on its next turn."""
        from app.guardrails import (
            LAST_ERROR_STATE_KEY,
            RepeatedFailureGuard,
        )

        guard = RepeatedFailureGuard(threshold=3)
        ctx = _fake_context()

        await guard.after_tool_callback(
            tool=SimpleNamespace(name="terminal"),
            args={"command": "foo"},
            tool_response={"error": "command not found: foo"},
            tool_context=ctx,
        )

        assert ctx.state[LAST_ERROR_STATE_KEY] == "command not found: foo"

    @pytest.mark.asyncio
    async def test_success_clears_last_error_from_state(self):
        """A successful tool call must clear ``state["last_error"]`` —
        otherwise the volatile tier keeps showing a stale failure long
        after the model recovered from it. ADK's State has no .pop/del,
        so "cleared" means set to None — readers must use .get()."""
        from app.guardrails import (
            LAST_ERROR_STATE_KEY,
            RepeatedFailureGuard,
        )

        guard = RepeatedFailureGuard(threshold=3)
        ctx = _fake_context()

        await guard.after_tool_callback(
            tool=SimpleNamespace(name="terminal"),
            args={"command": "foo"},
            tool_response={"error": "command not found: foo"},
            tool_context=ctx,
        )
        assert LAST_ERROR_STATE_KEY in ctx.state

        await guard.after_tool_callback(
            tool=SimpleNamespace(name="terminal"),
            args={"command": "foo"},
            tool_response={"exit_code": 0, "stdout": "ok"},
            tool_context=ctx,
        )

        assert ctx.state.get(LAST_ERROR_STATE_KEY) is None

    @pytest.mark.asyncio
    async def test_last_error_mirror_uses_exit_code_failures_too(self):
        """A non-zero exit_code is a failure for streak purposes — the
        mirror must surface SOMETHING informative even when there's no
        ``error`` key, so the model sees the failure in its next prompt."""
        from app.guardrails import (
            LAST_ERROR_STATE_KEY,
            RepeatedFailureGuard,
        )

        guard = RepeatedFailureGuard(threshold=3)
        ctx = _fake_context()

        await guard.after_tool_callback(
            tool=SimpleNamespace(name="terminal"),
            args={"command": "false"},
            tool_response={"exit_code": 1, "stderr": "boom"},
            tool_context=ctx,
        )

        assert LAST_ERROR_STATE_KEY in ctx.state
        assert ctx.state[LAST_ERROR_STATE_KEY], "must be a non-empty string"


# =============================================================================
# NoProgressGuard
# =============================================================================


class TestNoProgressGuard:
    def test_keyword_only_window_with_default(self):
        from app.guardrails import NoProgressGuard

        guard = NoProgressGuard()
        assert guard.window == 5

        custom = NoProgressGuard(window=3)
        assert custom.window == 3

    def test_window_must_be_keyword_only(self):
        from app.guardrails import NoProgressGuard

        with pytest.raises(TypeError):
            NoProgressGuard(3)

    def test_window_must_be_at_least_two(self):
        from app.guardrails import NoProgressGuard

        with pytest.raises(ValueError):
            NoProgressGuard(window=1)
        with pytest.raises(ValueError):
            NoProgressGuard(window=0)

    @pytest.mark.asyncio
    async def test_first_few_responses_do_not_halt(self):
        from app.guardrails import HALT_REASON_STATE_KEY, NoProgressGuard

        guard = NoProgressGuard(window=5)
        ctx = _fake_context()

        for _ in range(4):
            await guard.after_model_callback(
                callback_context=ctx,
                llm_response=_model_response("same response"),
            )

        assert HALT_REASON_STATE_KEY not in ctx.state

    @pytest.mark.asyncio
    async def test_window_identical_responses_set_halt_reason(self):
        from app.guardrails import HALT_REASON_STATE_KEY, NoProgressGuard

        guard = NoProgressGuard(window=3)
        ctx = _fake_context()

        for _ in range(3):
            await guard.after_model_callback(
                callback_context=ctx,
                llm_response=_model_response("I am thinking about it"),
            )

        halt = ctx.state[HALT_REASON_STATE_KEY]
        # Reason must mention no-progress so a human triaging the halt
        # can distinguish it from a tool-failure halt.
        assert (
            "no progress" in str(halt).lower()
            or "no_progress" in str(halt).lower()
        )

    @pytest.mark.asyncio
    async def test_differing_responses_do_not_halt(self):
        from app.guardrails import HALT_REASON_STATE_KEY, NoProgressGuard

        guard = NoProgressGuard(window=3)
        ctx = _fake_context()

        for i in range(10):
            await guard.after_model_callback(
                callback_context=ctx,
                llm_response=_model_response(f"distinct response {i}"),
            )

        assert HALT_REASON_STATE_KEY not in ctx.state

    @pytest.mark.asyncio
    async def test_whitespace_only_differences_count_as_identical(self):
        """Models often produce drift like a trailing newline or extra
        space without making real progress. Normalize whitespace so the
        guard catches this loop."""
        from app.guardrails import HALT_REASON_STATE_KEY, NoProgressGuard

        guard = NoProgressGuard(window=3)
        ctx = _fake_context()

        await guard.after_model_callback(
            callback_context=ctx,
            llm_response=_model_response("thinking about it"),
        )
        await guard.after_model_callback(
            callback_context=ctx,
            llm_response=_model_response("thinking  about it\n"),
        )
        await guard.after_model_callback(
            callback_context=ctx,
            llm_response=_model_response("  thinking about it  "),
        )

        assert HALT_REASON_STATE_KEY in ctx.state

    @pytest.mark.asyncio
    async def test_streak_resets_after_different_response(self):
        from app.guardrails import HALT_REASON_STATE_KEY, NoProgressGuard

        guard = NoProgressGuard(window=3)
        ctx = _fake_context()

        await guard.after_model_callback(
            callback_context=ctx, llm_response=_model_response("A")
        )
        await guard.after_model_callback(
            callback_context=ctx, llm_response=_model_response("A")
        )
        await guard.after_model_callback(
            callback_context=ctx, llm_response=_model_response("B")
        )
        await guard.after_model_callback(
            callback_context=ctx, llm_response=_model_response("A")
        )
        await guard.after_model_callback(
            callback_context=ctx, llm_response=_model_response("A")
        )

        assert HALT_REASON_STATE_KEY not in ctx.state

    @pytest.mark.asyncio
    async def test_response_with_no_text_does_not_advance_streak(self):
        """A response that's all function-calls (no text parts) has no
        comparable surface — it shouldn't be folded into the same-text
        streak.
        """
        from app.guardrails import HALT_REASON_STATE_KEY, NoProgressGuard

        guard = NoProgressGuard(window=3)
        ctx = _fake_context()

        await guard.after_model_callback(
            callback_context=ctx, llm_response=_model_response("same")
        )
        await guard.after_model_callback(
            callback_context=ctx,
            llm_response=LlmResponse(content=Content(role="model", parts=[])),
        )
        await guard.after_model_callback(
            callback_context=ctx, llm_response=_model_response("same")
        )

        assert HALT_REASON_STATE_KEY not in ctx.state

    @pytest.mark.asyncio
    async def test_returns_none_so_adk_keeps_real_llm_response(self):
        """``after_model_callback`` returning ``None`` keeps the model's
        actual response. Returning an ``LlmResponse`` would override it
        — never the guard's job."""
        from app.guardrails import NoProgressGuard

        guard = NoProgressGuard(window=2)
        ctx = _fake_context()

        result = await guard.after_model_callback(
            callback_context=ctx, llm_response=_model_response("same")
        )
        assert result is None

        result_at_halt = await guard.after_model_callback(
            callback_context=ctx, llm_response=_model_response("same")
        )
        assert result_at_halt is None

    @pytest.mark.asyncio
    async def test_does_not_overwrite_existing_halt_reason(self):
        from app.guardrails import HALT_REASON_STATE_KEY, NoProgressGuard

        guard = NoProgressGuard(window=2)
        ctx = _fake_context({HALT_REASON_STATE_KEY: "pre-existing"})

        for _ in range(3):
            await guard.after_model_callback(
                callback_context=ctx, llm_response=_model_response("same")
            )

        assert ctx.state[HALT_REASON_STATE_KEY] == "pre-existing"

    @pytest.mark.asyncio
    async def test_per_context_state_isolated(self):
        """Two ``CallbackContext`` instances (e.g. two sessions sharing a
        guard instance) must keep independent streaks. Otherwise a busy
        session could trip another session's halt."""
        from app.guardrails import HALT_REASON_STATE_KEY, NoProgressGuard

        guard = NoProgressGuard(window=3)
        ctx_a = _fake_context()
        ctx_b = _fake_context()

        for _ in range(3):
            await guard.after_model_callback(
                callback_context=ctx_a, llm_response=_model_response("same")
            )

        await guard.after_model_callback(
            callback_context=ctx_b, llm_response=_model_response("anything")
        )

        assert HALT_REASON_STATE_KEY in ctx_a.state
        assert HALT_REASON_STATE_KEY not in ctx_b.state


# =============================================================================
# halt_consumer_callback
# =============================================================================


class TestHaltConsumerCallback:
    @pytest.mark.asyncio
    async def test_no_halt_reason_returns_none(self):
        """``before_model_callback`` returning ``None`` lets ADK proceed
        with the real model call. That's the happy path."""
        from app.guardrails import halt_consumer_callback

        ctx = _fake_context()
        result = await halt_consumer_callback(
            callback_context=ctx, llm_request=SimpleNamespace()
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_halt_reason_short_circuits_with_llm_response(self):
        """Once the graceful handoff has been delivered and ``halt_reason``
        is still set, the callback must return a complete ``LlmResponse``
        so the runner skips the model entirely.
        """
        from app.guardrails.halt_consumer import (
            HALT_HANDOFF_DELIVERED_STATE_KEY,
            HALT_REASON_STATE_KEY,
            halt_consumer_callback,
        )

        ctx = _fake_context(
            {
                HALT_REASON_STATE_KEY: "web_search failed 3 times",
                HALT_HANDOFF_DELIVERED_STATE_KEY: True,
            }
        )
        result = await halt_consumer_callback(
            callback_context=ctx, llm_request=SimpleNamespace()
        )

        assert isinstance(result, LlmResponse)
        assert result.content is not None
        assert result.content.parts
        text = "\n".join(p.text for p in result.content.parts if p.text)
        # Verbatim halt_reason must appear so the user/eval can see
        # exactly why the run halted.
        assert "web_search failed 3 times" in text

    @pytest.mark.asyncio
    async def test_halt_response_role_is_model(self):
        """ADK's content must be ``model`` for an assistant-style turn —
        ``system`` is reserved and would break the role contract."""
        from app.guardrails.halt_consumer import (
            HALT_HANDOFF_DELIVERED_STATE_KEY,
            HALT_REASON_STATE_KEY,
            halt_consumer_callback,
        )

        ctx = _fake_context(
            {
                HALT_REASON_STATE_KEY: "stuck",
                HALT_HANDOFF_DELIVERED_STATE_KEY: True,
            }
        )
        result = await halt_consumer_callback(
            callback_context=ctx, llm_request=SimpleNamespace()
        )

        assert result is not None
        assert result.content is not None
        assert result.content.role == "model"

    @pytest.mark.asyncio
    async def test_empty_string_halt_reason_does_not_short_circuit(self):
        """An empty/None halt_reason must NOT trip the guard — only an
        actual reason string halts. This prevents an accidental clear-
        then-not-clear from looking like a halt."""
        from app.guardrails import (
            HALT_REASON_STATE_KEY,
            halt_consumer_callback,
        )

        ctx_empty = _fake_context({HALT_REASON_STATE_KEY: ""})
        assert (
            await halt_consumer_callback(
                callback_context=ctx_empty, llm_request=SimpleNamespace()
            )
            is None
        )

        ctx_none = _fake_context({HALT_REASON_STATE_KEY: None})
        assert (
            await halt_consumer_callback(
                callback_context=ctx_none, llm_request=SimpleNamespace()
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_does_not_clear_halt_reason_on_short_circuit(self):
        """The halt_reason must remain in state after the callback runs
        — downstream consumers (logging, the next turn's halt-aware
        prompt) may need to read it. Don't clear it."""
        from app.guardrails.halt_consumer import (
            HALT_HANDOFF_DELIVERED_STATE_KEY,
            HALT_REASON_STATE_KEY,
            halt_consumer_callback,
        )

        ctx = _fake_context(
            {
                HALT_REASON_STATE_KEY: "stuck",
                HALT_HANDOFF_DELIVERED_STATE_KEY: True,
                "other": 42,
            }
        )
        await halt_consumer_callback(
            callback_context=ctx, llm_request=SimpleNamespace()
        )

        assert ctx.state[HALT_REASON_STATE_KEY] == "stuck"
        assert ctx.state["other"] == 42


# =============================================================================
# Integration: guard + consumer compose
# =============================================================================


class TestGuardAndConsumerCompose:
    """The three components are designed to share state via the same
    ``HALT_REASON_STATE_KEY``. Pin that integration so a future rename
    of either side breaks loudly."""

    @pytest.mark.asyncio
    async def test_repeated_failure_then_consumer_halts(self):
        from google.adk.models import LlmRequest

        from app.guardrails import (
            RepeatedFailureGuard,
            halt_consumer_callback,
        )

        guard = RepeatedFailureGuard(threshold=3)
        ctx = _fake_context()

        for _ in range(3):
            await guard.after_tool_callback(
                tool=SimpleNamespace(name="web_search"),
                args={"query": "boom"},
                tool_response={"error": "boom"},
                tool_context=ctx,
            )

        # First halt turn: graceful handoff (model runs once, no short-circuit).
        first = await halt_consumer_callback(
            callback_context=ctx, llm_request=LlmRequest()
        )
        assert first is None
        # Second halt turn: bare short-circuit.
        second = await halt_consumer_callback(
            callback_context=ctx, llm_request=LlmRequest()
        )
        assert isinstance(second, LlmResponse)

    @pytest.mark.asyncio
    async def test_no_progress_then_consumer_halts(self):
        from google.adk.models import LlmRequest

        from app.guardrails import NoProgressGuard, halt_consumer_callback

        guard = NoProgressGuard(window=2)
        ctx = _fake_context()

        for _ in range(2):
            await guard.after_model_callback(
                callback_context=ctx, llm_response=_model_response("same")
            )

        first = await halt_consumer_callback(
            callback_context=ctx, llm_request=LlmRequest()
        )
        assert first is None
        second = await halt_consumer_callback(
            callback_context=ctx, llm_request=LlmRequest()
        )
        assert isinstance(second, LlmResponse)
