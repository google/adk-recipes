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

"""Deterministic tests for ``app.conversation.iteration_budget.IterationBudget``.

``IterationBudget`` is a per-agent iteration counter (``max_total`` cap,
``used`` / ``remaining`` accessors) with these properties:

* Exceptions, not booleans. Callers should not be able to silently ignore an
  exceeded budget — they must catch a typed exception. Hence
  ``record_iteration()`` raises ``IterationLimitExceeded`` instead of
  returning ``False``.
* A per-iteration tool-call cap. Prevents one pathological turn from
  blowing through the whole budget in tool calls.
* A halt mechanism. ``halt(reason)`` sets a sticky flag; both
  ``record_iteration`` and ``record_tool_call`` raise ``AgentHalted`` from
  that point forward.
* Single-event-loop assumption (no threading), so no locking is needed.

This file is the spec for the iteration-budget implementation.
"""

from __future__ import annotations

import pytest

# =============================================================================
# Module surface
# =============================================================================


class TestModuleSurface:
    def test_exports_class_and_exceptions(self) -> None:
        from app.conversation import iteration_budget as mod

        for name in (
            "IterationBudget",
            "IterationLimitExceeded",
            "ToolCallLimitExceeded",
            "AgentHalted",
        ):
            assert hasattr(mod, name), (
                f"app.conversation.iteration_budget must export {name}"
            )

    def test_exceptions_inherit_from_distinct_base(self) -> None:
        """The three budget exceptions must be catchable individually.

        A caller that wants to handle 'budget exhausted' differently from
        'halted by guardrail' must be able to distinguish them.
        """
        from app.conversation.iteration_budget import (
            AgentHalted,
            IterationLimitExceeded,
            ToolCallLimitExceeded,
        )

        assert IterationLimitExceeded is not ToolCallLimitExceeded
        assert IterationLimitExceeded is not AgentHalted
        assert ToolCallLimitExceeded is not AgentHalted


# =============================================================================
# Constructor validation
# =============================================================================


class TestConstructor:
    def test_accepts_positive_max_iterations(self) -> None:
        from app.conversation.iteration_budget import IterationBudget

        budget = IterationBudget(max_iterations=10)
        assert budget.iterations_used == 0
        assert budget.remaining_iterations == 10
        assert budget.tool_calls_this_iteration == 0

    def test_default_max_tool_calls_per_iteration_is_fifty(self) -> None:
        from app.conversation.iteration_budget import (
            IterationBudget,
            ToolCallLimitExceeded,
        )

        budget = IterationBudget(max_iterations=5)
        budget.record_iteration()
        for _ in range(50):
            budget.record_tool_call()
        with pytest.raises(ToolCallLimitExceeded):
            budget.record_tool_call()

    def test_zero_max_iterations_rejected(self) -> None:
        from app.conversation.iteration_budget import IterationBudget

        with pytest.raises(ValueError):
            IterationBudget(max_iterations=0)

    def test_negative_max_iterations_rejected(self) -> None:
        from app.conversation.iteration_budget import IterationBudget

        with pytest.raises(ValueError):
            IterationBudget(max_iterations=-1)

    def test_zero_max_tool_calls_rejected(self) -> None:
        from app.conversation.iteration_budget import IterationBudget

        with pytest.raises(ValueError):
            IterationBudget(max_iterations=10, max_tool_calls_per_iteration=0)

    def test_negative_max_tool_calls_rejected(self) -> None:
        from app.conversation.iteration_budget import IterationBudget

        with pytest.raises(ValueError):
            IterationBudget(max_iterations=10, max_tool_calls_per_iteration=-1)

    def test_constructor_is_keyword_only(self) -> None:
        """``max_iterations`` is keyword-only to prevent confusion between the
        two limits at call sites."""
        from app.conversation.iteration_budget import IterationBudget

        with pytest.raises(TypeError):
            IterationBudget(10)


# =============================================================================
# record_iteration semantics
# =============================================================================


class TestRecordIteration:
    def test_increments_iterations_used(self) -> None:
        from app.conversation.iteration_budget import IterationBudget

        budget = IterationBudget(max_iterations=3)
        budget.record_iteration()
        assert budget.iterations_used == 1
        budget.record_iteration()
        assert budget.iterations_used == 2

    def test_remaining_iterations_decreases(self) -> None:
        from app.conversation.iteration_budget import IterationBudget

        budget = IterationBudget(max_iterations=5)
        assert budget.remaining_iterations == 5
        budget.record_iteration()
        assert budget.remaining_iterations == 4
        budget.record_iteration()
        assert budget.remaining_iterations == 3

    def test_records_up_to_limit_then_raises(self) -> None:
        from app.conversation.iteration_budget import (
            IterationBudget,
            IterationLimitExceeded,
        )

        budget = IterationBudget(max_iterations=3)
        budget.record_iteration()
        budget.record_iteration()
        budget.record_iteration()
        with pytest.raises(IterationLimitExceeded):
            budget.record_iteration()

    def test_exception_message_carries_limit_value(self) -> None:
        from app.conversation.iteration_budget import (
            IterationBudget,
            IterationLimitExceeded,
        )

        budget = IterationBudget(max_iterations=7)
        for _ in range(7):
            budget.record_iteration()
        with pytest.raises(IterationLimitExceeded) as excinfo:
            budget.record_iteration()
        assert "7" in str(excinfo.value)

    def test_iterations_used_does_not_advance_past_limit(self) -> None:
        """Once at the cap, a failed record_iteration must not bump the counter.

        Otherwise repeated callers would see ``iterations_used > max_iterations``,
        which would confuse downstream UI / logging.
        """
        from app.conversation.iteration_budget import (
            IterationBudget,
            IterationLimitExceeded,
        )

        budget = IterationBudget(max_iterations=2)
        budget.record_iteration()
        budget.record_iteration()
        for _ in range(5):
            with pytest.raises(IterationLimitExceeded):
                budget.record_iteration()
        assert budget.iterations_used == 2

    def test_remaining_iterations_clamps_at_zero(self) -> None:
        from app.conversation.iteration_budget import (
            IterationBudget,
            IterationLimitExceeded,
        )

        budget = IterationBudget(max_iterations=2)
        budget.record_iteration()
        budget.record_iteration()
        with pytest.raises(IterationLimitExceeded):
            budget.record_iteration()
        assert budget.remaining_iterations == 0


# =============================================================================
# record_tool_call semantics
# =============================================================================


class TestRecordToolCall:
    def test_increments_tool_calls_this_iteration(self) -> None:
        from app.conversation.iteration_budget import IterationBudget

        budget = IterationBudget(
            max_iterations=5, max_tool_calls_per_iteration=4
        )
        budget.record_iteration()
        budget.record_tool_call()
        assert budget.tool_calls_this_iteration == 1
        budget.record_tool_call()
        assert budget.tool_calls_this_iteration == 2

    def test_raises_when_per_iter_cap_hit(self) -> None:
        from app.conversation.iteration_budget import (
            IterationBudget,
            ToolCallLimitExceeded,
        )

        budget = IterationBudget(
            max_iterations=5, max_tool_calls_per_iteration=3
        )
        budget.record_iteration()
        budget.record_tool_call()
        budget.record_tool_call()
        budget.record_tool_call()
        with pytest.raises(ToolCallLimitExceeded):
            budget.record_tool_call()

    def test_exception_message_carries_per_iter_cap(self) -> None:
        from app.conversation.iteration_budget import (
            IterationBudget,
            ToolCallLimitExceeded,
        )

        budget = IterationBudget(
            max_iterations=5, max_tool_calls_per_iteration=4
        )
        budget.record_iteration()
        for _ in range(4):
            budget.record_tool_call()
        with pytest.raises(ToolCallLimitExceeded) as excinfo:
            budget.record_tool_call()
        assert "4" in str(excinfo.value)

    def test_tool_call_counter_does_not_advance_past_cap(self) -> None:
        from app.conversation.iteration_budget import (
            IterationBudget,
            ToolCallLimitExceeded,
        )

        budget = IterationBudget(
            max_iterations=5, max_tool_calls_per_iteration=2
        )
        budget.record_iteration()
        budget.record_tool_call()
        budget.record_tool_call()
        for _ in range(5):
            with pytest.raises(ToolCallLimitExceeded):
                budget.record_tool_call()
        assert budget.tool_calls_this_iteration == 2


# =============================================================================
# reset_iteration_counters semantics
# =============================================================================


class TestResetIterationCounters:
    def test_zeroes_per_iteration_tool_calls(self) -> None:
        from app.conversation.iteration_budget import IterationBudget

        budget = IterationBudget(
            max_iterations=5, max_tool_calls_per_iteration=3
        )
        budget.record_iteration()
        budget.record_tool_call()
        budget.record_tool_call()
        assert budget.tool_calls_this_iteration == 2
        budget.reset_iteration_counters()
        assert budget.tool_calls_this_iteration == 0

    def test_does_not_zero_iterations_used(self) -> None:
        """Iteration boundary resets per-iter counters, NOT the total counter."""
        from app.conversation.iteration_budget import IterationBudget

        budget = IterationBudget(
            max_iterations=5, max_tool_calls_per_iteration=3
        )
        budget.record_iteration()
        budget.record_iteration()
        assert budget.iterations_used == 2
        budget.reset_iteration_counters()
        assert budget.iterations_used == 2
        assert budget.remaining_iterations == 3

    def test_after_reset_tool_calls_can_run_again(self) -> None:
        from app.conversation.iteration_budget import IterationBudget

        budget = IterationBudget(
            max_iterations=5, max_tool_calls_per_iteration=2
        )
        budget.record_iteration()
        budget.record_tool_call()
        budget.record_tool_call()
        budget.reset_iteration_counters()
        # Fresh per-iter counter should let us record up to the cap again
        budget.record_tool_call()
        budget.record_tool_call()
        assert budget.tool_calls_this_iteration == 2

    def test_reset_on_fresh_budget_is_noop(self) -> None:
        from app.conversation.iteration_budget import IterationBudget

        budget = IterationBudget(max_iterations=5)
        budget.reset_iteration_counters()
        assert budget.iterations_used == 0
        assert budget.tool_calls_this_iteration == 0
        assert budget.remaining_iterations == 5


# =============================================================================
# Halt contract
# =============================================================================


class TestHalt:
    def test_halt_sets_flag_and_reason(self) -> None:
        from app.conversation.iteration_budget import IterationBudget

        budget = IterationBudget(max_iterations=5)
        assert budget.halted is False
        assert budget.halt_reason is None

        budget.halt("guardrail triggered: repeated_tool_failure")
        assert budget.halted is True
        assert (
            budget.halt_reason == "guardrail triggered: repeated_tool_failure"
        )

    def test_record_iteration_raises_after_halt(self) -> None:
        from app.conversation.iteration_budget import (
            AgentHalted,
            IterationBudget,
        )

        budget = IterationBudget(max_iterations=5)
        budget.halt("manual stop")
        with pytest.raises(AgentHalted):
            budget.record_iteration()

    def test_record_tool_call_raises_after_halt(self) -> None:
        from app.conversation.iteration_budget import (
            AgentHalted,
            IterationBudget,
        )

        budget = IterationBudget(max_iterations=5)
        budget.record_iteration()
        budget.halt("manual stop")
        with pytest.raises(AgentHalted):
            budget.record_tool_call()

    def test_halt_exception_carries_reason(self) -> None:
        from app.conversation.iteration_budget import (
            AgentHalted,
            IterationBudget,
        )

        budget = IterationBudget(max_iterations=5)
        budget.halt("no_progress_for_3_iterations")
        with pytest.raises(AgentHalted) as excinfo:
            budget.record_iteration()
        assert "no_progress_for_3_iterations" in str(excinfo.value)

    def test_halt_after_some_iterations_preserves_used_count(self) -> None:
        from app.conversation.iteration_budget import IterationBudget

        budget = IterationBudget(max_iterations=10)
        budget.record_iteration()
        budget.record_iteration()
        budget.halt("test")
        # Halt does not unwind history; iterations_used must reflect what
        # actually ran.
        assert budget.iterations_used == 2

    def test_halt_is_sticky_across_resets(self) -> None:
        """``reset_iteration_counters`` is an iteration-boundary operation;
        it must NOT clear a halt flag.

        A guardrail that halted the agent must stay halted until the loop
        exits, not until the next iteration starts.
        """
        from app.conversation.iteration_budget import (
            AgentHalted,
            IterationBudget,
        )

        budget = IterationBudget(max_iterations=5)
        budget.halt("permanent stop")
        budget.reset_iteration_counters()
        assert budget.halted is True
        with pytest.raises(AgentHalted):
            budget.record_iteration()

    def test_double_halt_preserves_some_reason(self) -> None:
        """A second halt() must not erase the halt entirely — the agent stays
        halted and a non-empty reason is preserved. Spec doesn't mandate
        first-wins vs last-wins; either is fine."""
        from app.conversation.iteration_budget import IterationBudget

        budget = IterationBudget(max_iterations=5)
        budget.halt("first reason")
        budget.halt("second reason")
        assert budget.halted is True
        assert budget.halt_reason in ("first reason", "second reason")


# =============================================================================
# Realistic loop scenario
# =============================================================================


class TestLoopScenario:
    def test_three_iterations_with_tool_calls_each(self) -> None:
        """Walk through a realistic loop: iteration → tool calls → reset → repeat.

        Pins the intended choreography so a future change to the API surface
        (e.g., merging record_iteration and reset_iteration_counters) gets
        caught.
        """
        from app.conversation.iteration_budget import IterationBudget

        budget = IterationBudget(
            max_iterations=3, max_tool_calls_per_iteration=4
        )

        # Iteration 1: 2 tool calls
        budget.record_iteration()
        budget.record_tool_call()
        budget.record_tool_call()
        assert budget.iterations_used == 1
        assert budget.tool_calls_this_iteration == 2
        budget.reset_iteration_counters()

        # Iteration 2: 4 tool calls (at cap, not over)
        budget.record_iteration()
        for _ in range(4):
            budget.record_tool_call()
        assert budget.iterations_used == 2
        assert budget.tool_calls_this_iteration == 4
        budget.reset_iteration_counters()

        # Iteration 3: 1 tool call
        budget.record_iteration()
        budget.record_tool_call()
        assert budget.iterations_used == 3
        assert budget.remaining_iterations == 0
        assert budget.tool_calls_this_iteration == 1


# =============================================================================
# Server-restart isolation
# =============================================================================
#
# Why budgets live in session.state:
#
# The IterationBudgetPlugin originally held budgets in an instance
# dict keyed by (user_id, session_id). With InMemorySessionService that dict
# was the only halt-state store, but it also leaked: a long-running process
# could accumulate halted sessions that never reset. Worse, a process restart
# combined with a persistent SessionService (DatabaseSessionService etc.)
# could leave the session "halted" with no way to clear it — the new plugin
# rebuilt from scratch but the old halt reason was gone with the old dict.
#
# Fix: the plugin's budget is now a thin in-memory view over session.state.
# Counters and halt flags are JSON-primitive; IterationBudget itself is
# rebuilt from those primitives on each callback. A fresh plugin instance
# pointed at a fresh session.state therefore sees a fresh, un-halted budget.


class TestPluginSessionStateIsBudgetSourceOfTruth:
    """Budget state lives in session.state, not in the plugin instance.

    The original ``IterationBudgetPlugin._budgets`` sidecar dict was the
    sole owner of per-session budget state. Two consequences fall out of
    that:

    1. Halt set by guardrails was held only in the plugin's in-memory
       dict — a process restart with a durable SessionService (Database/
       VertexAi) wiped the halt with no replacement; the resumed session
       silently proceeded past whatever halted it.
    2. A long-running process accumulated halted-session entries the
       plugin had no way to garbage-collect.

    Fix: the plugin treats ``session.state`` as the source of truth for
    iteration count and halt status. The ``IterationBudget`` typed
    object is rebuilt on each callback from those primitives, so the
    plugin instance carries no state of its own. Tests below assert
    against ``session.state`` (the durable substrate) so a fresh plugin
    instance pointed at the same state sees the same budget.

    A fresh plugin instance rebuilds its budget view from session.state, so
    there is no plugin-lifetime state to lose.
    """

    def _ctx(
        self,
        *,
        user_id: str = "alice",
        session_id: str = "s1",
        state: dict[str, object] | None = None,
    ):
        from types import SimpleNamespace

        state = state if state is not None else {}
        session = SimpleNamespace(id=session_id, state=state, user_id=user_id)
        return SimpleNamespace(user_id=user_id, session=session)

    def _cb_ctx(self, state: dict[str, object]):
        from types import SimpleNamespace

        return SimpleNamespace(state=state)

    async def _consume_bare_halt(self, plugin, state: dict[str, object]):
        """Drive past the one-shot graceful handoff and return the bare halt.

        The first before_model_callback on a halted budget runs the graceful
        handoff (mutates the request, returns None); the second returns the
        bare [halted: ...] LlmResponse.
        """
        from google.adk.models import LlmRequest

        await plugin.before_model_callback(
            callback_context=self._cb_ctx(state), llm_request=LlmRequest()
        )
        return await plugin.before_model_callback(
            callback_context=self._cb_ctx(state), llm_request=LlmRequest()
        )

    @pytest.mark.asyncio
    async def test_fresh_plugin_with_same_session_state_sees_existing_iteration_count(
        self,
    ):
        """A durable SessionService preserves ``session.state`` across
        process restart. A second plugin instance pointed at that state
        must see the iterations already used — not start from 0 and let
        the user blow past the original cap."""
        from app.conversation.iteration_budget_plugin import (
            IterationBudgetPlugin,
        )

        durable_state: dict[str, object] = {}
        plugin_a = IterationBudgetPlugin(max_iterations=10)
        ctx_a = self._ctx(state=durable_state)

        for _ in range(3):
            await plugin_a.before_run_callback(invocation_context=ctx_a)
        assert durable_state["iteration"] == 3

        # Simulate restart: new plugin instance, same durable state.
        plugin_b = IterationBudgetPlugin(max_iterations=10)
        ctx_b = self._ctx(state=durable_state)
        budget = plugin_b._budget_for(ctx_b)
        assert budget.iterations_used == 3

    @pytest.mark.asyncio
    async def test_fresh_plugin_with_fresh_state_sees_unhalted_budget(self):
        """A new logical session (fresh session.state) must always start
        un-halted, even if the same (user_id, session_id) keys were used
        by a previously halted session in this process."""
        from app.conversation.iteration_budget_plugin import (
            IterationBudgetPlugin,
        )

        plugin_a = IterationBudgetPlugin(max_iterations=3)
        ctx = self._ctx()

        for _ in range(3):
            await plugin_a.before_run_callback(invocation_context=ctx)
        await plugin_a.before_run_callback(invocation_context=ctx)
        response = await self._consume_bare_halt(plugin_a, ctx.session.state)
        assert response is not None, "sanity: budget exhausted should halt"

        plugin_b = IterationBudgetPlugin(max_iterations=3)
        ctx_after_restart = self._ctx()
        budget = plugin_b._budget_for(ctx_after_restart)
        assert budget.halted is False
        assert budget.iterations_used == 0

    @pytest.mark.asyncio
    async def test_halt_persists_through_session_state_across_plugin_instances(
        self,
    ):
        """If a guardrail halts the session, the halt must survive process
        restart on the same durable session — otherwise an agent halted
        for repeated-failure could be silently resumed by a restart."""
        from app.conversation.iteration_budget_plugin import (
            IterationBudgetPlugin,
        )

        durable_state: dict[str, object] = {}
        plugin_a = IterationBudgetPlugin(max_iterations=10)
        ctx_a = self._ctx(state=durable_state)

        await plugin_a.before_run_callback(invocation_context=ctx_a)
        plugin_a._budget_for(ctx_a).halt("guardrail tripped")

        plugin_b = IterationBudgetPlugin(max_iterations=10)
        ctx_b = self._ctx(state=durable_state)
        await plugin_b.before_run_callback(invocation_context=ctx_b)
        response = await self._consume_bare_halt(plugin_b, ctx_b.session.state)
        assert response is not None, "halt set in prior process must persist"

    @pytest.mark.asyncio
    async def test_within_a_run_plugin_sees_consistent_halt(self):
        """Within a single plugin instance, halt set via halt() must still
        block subsequent before_run callbacks — the session-state fix
        must not weaken intra-process halt enforcement."""
        from app.conversation.iteration_budget_plugin import (
            IterationBudgetPlugin,
        )

        plugin = IterationBudgetPlugin(max_iterations=10)
        ctx = self._ctx()

        await plugin.before_run_callback(invocation_context=ctx)
        plugin._budget_for(ctx).halt("guardrail tripped")

        await plugin.before_run_callback(invocation_context=ctx)
        response = await self._consume_bare_halt(plugin, ctx.session.state)
        assert response is not None, "halt must still short-circuit"

    @pytest.mark.asyncio
    async def test_plugin_does_not_retain_internal_budget_dict(self):
        """The plugin instance must not own a sidecar dict of budgets —
        a long-running process would otherwise leak one entry per
        (user_id, session_id) seen, with no GC path. ``session.state``
        is the only durable store."""
        from app.conversation.iteration_budget_plugin import (
            IterationBudgetPlugin,
        )

        plugin = IterationBudgetPlugin(max_iterations=10)
        # No internal accumulator; introspection guard for the contract.
        assert not hasattr(plugin, "_budgets"), (
            "plugin must not keep a per-session budget cache — state lives "
            "in session.state"
        )
