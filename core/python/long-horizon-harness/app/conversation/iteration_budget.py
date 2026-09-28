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

"""Per-session iteration + tool-call budget with halt signalling.

for ADK's single-event-loop model: no threading lock, no refund(), exception-
raising instead of bool returns, plus a per-iteration tool-call cap and a
sticky halt flag that ``IterationBudgetPlugin`` consults before each model
call.
"""

from __future__ import annotations


class IterationLimitExceeded(RuntimeError):
    """Raised when ``record_iteration()`` is called past ``max_iterations``."""


class ToolCallLimitExceeded(RuntimeError):
    """Raised when ``record_tool_call()`` exceeds the per-iteration cap."""


class AgentHalted(RuntimeError):
    """Raised by ``record_iteration`` / ``record_tool_call`` after ``halt()``."""


_DEFAULT_MAX_TOOL_CALLS_PER_ITERATION = 50


class IterationBudget:
    def __init__(
        self,
        *,
        max_iterations: int,
        max_tool_calls_per_iteration: int = _DEFAULT_MAX_TOOL_CALLS_PER_ITERATION,
    ) -> None:
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
        self._iterations_used = 0
        self._tool_calls_this_iteration = 0
        self._halted = False
        self._halt_reason: str | None = None

    @property
    def iterations_used(self) -> int:
        return self._iterations_used

    @property
    def remaining_iterations(self) -> int:
        return max(0, self._max_iterations - self._iterations_used)

    @property
    def tool_calls_this_iteration(self) -> int:
        return self._tool_calls_this_iteration

    @property
    def halted(self) -> bool:
        return self._halted

    @property
    def halt_reason(self) -> str | None:
        return self._halt_reason

    def record_iteration(self) -> None:
        if self._halted:
            raise AgentHalted(
                f"Agent halted: {self._halt_reason}"
                if self._halt_reason
                else "Agent halted"
            )
        if self._iterations_used >= self._max_iterations:
            raise IterationLimitExceeded(
                f"Iteration limit reached: {self._max_iterations}"
            )
        self._iterations_used += 1

    def record_tool_call(self) -> None:
        if self._halted:
            raise AgentHalted(
                f"Agent halted: {self._halt_reason}"
                if self._halt_reason
                else "Agent halted"
            )
        if (
            self._tool_calls_this_iteration
            >= self._max_tool_calls_per_iteration
        ):
            raise ToolCallLimitExceeded(
                "Tool-call limit reached for this iteration: "
                f"{self._max_tool_calls_per_iteration}"
            )
        self._tool_calls_this_iteration += 1

    def reset_iteration_counters(self) -> None:
        self._tool_calls_this_iteration = 0

    def halt(self, reason: str) -> None:
        self._halted = True
        self._halt_reason = (
            reason  # last-wins: latest reason is most diagnostic
        )

    def __repr__(self) -> str:
        halt_part = f", halted={self._halt_reason!r}" if self._halted else ""
        return (
            f"IterationBudget("
            f"iterations={self._iterations_used}/{self._max_iterations}, "
            f"tool_calls={self._tool_calls_this_iteration}/{self._max_tool_calls_per_iteration}"
            f"{halt_part})"
        )
