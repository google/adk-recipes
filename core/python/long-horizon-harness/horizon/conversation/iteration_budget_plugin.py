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

"""IterationBudgetPlugin — runtime iteration & tool-call enforcement.

ADK's CLI runtime (`agents-cli run`, playground, `/run_sse`) constructs
the Runner inside its FastAPI server and calls `runner.run_async()`
directly. The only ADK-blessed injection point for cross-cutting
per-invocation enforcement is `BasePlugin` attached via
`App(plugins=[...])`, so all budget accounting lives here.

Per-session state lives entirely in `session.state` (JSON primitives) so
budget + halt status travel with the session, not the plugin instance.
The `IterationBudget` typed object is rebuilt on each callback from the
stored primitives. This keeps a process restart from silently losing a
guardrail-set halt and prevents the plugin from accumulating per-session
budget objects with no GC path.
"""

from __future__ import annotations

from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.adk.models import LlmRequest, LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.genai import types

from horizon.conversation.iteration_budget import (
    AgentHalted,
    IterationBudget,
    IterationLimitExceeded,
    ToolCallLimitExceeded,
)

_BUDGET_EXCEPTIONS = (
    IterationLimitExceeded,
    ToolCallLimitExceeded,
    AgentHalted,
)

# Mirrored into session.state so the system_prompt volatile tier can read it
# without reaching into this plugin's internals.
ITERATION_STATE_KEY = "iteration"
_TOOL_CALLS_STATE_KEY = "_iteration_budget_tool_calls"
_HALTED_STATE_KEY = "_iteration_budget_halted"
_HALT_REASON_STATE_KEY = "_iteration_budget_halt_reason"
_HANDOFF_DELIVERED_STATE_KEY = "_iteration_budget_handoff_delivered"

_DEFAULT_MAX_ITERATIONS = 50
_DEFAULT_MAX_TOOL_CALLS_PER_ITERATION = 50


class IterationBudgetPlugin(BasePlugin):
    """Enforces per-session iteration + per-iteration tool-call caps.

    Attach via `App(plugins=[IterationBudgetPlugin(...)])`. ADK's
    `PluginManager` will invoke the callbacks for every `runner.run_async`.
    """

    def __init__(
        self,
        *,
        max_iterations: int = _DEFAULT_MAX_ITERATIONS,
        max_tool_calls_per_iteration: int = _DEFAULT_MAX_TOOL_CALLS_PER_ITERATION,
    ) -> None:
        super().__init__(name="iteration_budget")
        if max_iterations <= 0:
            raise ValueError(
                f"max_iterations must be positive, got {max_iterations}"
            )
        if max_tool_calls_per_iteration <= 0:
            raise ValueError(
                "max_tool_calls_per_iteration must be positive, got "
                f"{max_tool_calls_per_iteration}"
            )
        self._max_iterations = max_iterations
        self._max_tool_calls_per_iteration = max_tool_calls_per_iteration

    def _hydrate_budget(self, state: dict[str, Any]) -> IterationBudget:
        budget = IterationBudget(
            max_iterations=self._max_iterations,
            max_tool_calls_per_iteration=self._max_tool_calls_per_iteration,
        )
        budget._iterations_used = int(state.get(ITERATION_STATE_KEY, 0))
        budget._tool_calls_this_iteration = int(
            state.get(_TOOL_CALLS_STATE_KEY, 0)
        )
        if state.get(_HALTED_STATE_KEY):
            budget._halted = True
            budget._halt_reason = state.get(_HALT_REASON_STATE_KEY)
        return budget

    def _persist_budget(
        self, state: dict[str, Any], budget: IterationBudget
    ) -> None:
        state[ITERATION_STATE_KEY] = budget.iterations_used
        state[_TOOL_CALLS_STATE_KEY] = budget.tool_calls_this_iteration
        state[_HALTED_STATE_KEY] = budget.halted
        state[_HALT_REASON_STATE_KEY] = budget.halt_reason

    def _budget_for(self, ctx: InvocationContext) -> IterationBudget:
        # Returns a live view bound to session.state. Mutations on the
        # returned budget must be followed by `_persist_budget` to take
        # effect; callers inside this module always do so.
        return _StateBoundBudget(
            plugin=self,
            state=ctx.session.state,
            budget=self._hydrate_budget(ctx.session.state),
        )

    async def before_run_callback(
        self, *, invocation_context: InvocationContext
    ) -> types.Content | None:
        # ADK 2.0's Runner discards before_run_callback return values, so this
        # callback's job is state mutation only. The actual short-circuit
        # happens in before_model_callback below, which is still honored.
        state = invocation_context.session.state
        budget = self._hydrate_budget(state)
        if budget.halted:
            return None
        try:
            budget.record_iteration()
        except _BUDGET_EXCEPTIONS as exc:
            budget.halt(str(exc))
            self._persist_budget(state, budget)
            return None
        budget.reset_iteration_counters()
        state[_HANDOFF_DELIVERED_STATE_KEY] = None
        self._persist_budget(state, budget)
        return None

    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> LlmResponse | None:
        state = callback_context.state
        budget = self._hydrate_budget(state)
        if not budget.halted:
            return None

        reason = budget.halt_reason or "agent halted"
        from horizon.conversation.graceful_halt import deliver_halt_or_envelope

        return deliver_halt_or_envelope(
            llm_request,
            state,
            reason=reason,
            flag_key=_HANDOFF_DELIVERED_STATE_KEY,
        )

    async def on_event_callback(
        self, *, invocation_context: InvocationContext, event: Event
    ) -> Event | None:
        # Never replace the event with a synthetic halt: the calls are
        # already dispatched upstream of this hook, so swapping the event
        # orphans their function_response events and ADK raises "No function
        # call event found" next turn. before_model_callback surfaces the
        # halt envelope instead, once budget.halted is True.
        state = invocation_context.session.state
        budget = self._hydrate_budget(state)
        for _ in event.get_function_calls():
            try:
                budget.record_tool_call()
            except _BUDGET_EXCEPTIONS as exc:
                if not budget.halted:
                    budget.halt(str(exc))
        self._persist_budget(state, budget)
        return None


class _StateBoundBudget(IterationBudget):
    """IterationBudget view that auto-persists mutations into session.state.

    Tests reach in via ``plugin._budget_for(ctx)`` to call ``.halt(...)``
    on the budget directly; without auto-persist, those mutations would
    not survive the call and the next ``before_run_callback`` would
    rehydrate from stale state.
    """

    def __init__(
        self,
        *,
        plugin: IterationBudgetPlugin,
        state: dict[str, Any],
        budget: IterationBudget,
    ) -> None:
        super().__init__(
            max_iterations=budget._max_iterations,
            max_tool_calls_per_iteration=budget._max_tool_calls_per_iteration,
        )
        self._iterations_used = budget._iterations_used
        self._tool_calls_this_iteration = budget._tool_calls_this_iteration
        self._halted = budget._halted
        self._halt_reason = budget._halt_reason
        self.__state = state
        self.__plugin = plugin

    def _flush(self) -> None:
        self.__plugin._persist_budget(self.__state, self)

    def record_iteration(self) -> None:
        super().record_iteration()
        self._flush()

    def record_tool_call(self) -> None:
        super().record_tool_call()
        self._flush()

    def reset_iteration_counters(self) -> None:
        super().reset_iteration_counters()
        self._flush()

    def halt(self, reason: str) -> None:
        super().halt(reason)
        self._flush()
