# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Decide which events carry an agent's changed system instruction.

A system instruction can be very large, so repeating it on every event makes
review prompts huge. A changed instruction is shown once per turn: on the first
event the agent authors under it, and again each time it changes within the
turn, including a return to the baseline. The baseline is the instruction in
the agent's `AgentConfig`, which the review prompt already shows in the agent
block.

Tracking restarts from the baseline on every turn because the trajectory archive
stores each turn as its own row, so each turn must read on its own.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

SYSTEM_INSTRUCTION_KEY = "system_instruction"
"""`state_delta` key for a changed system instruction."""


class TurnInstructionTracker:
    """Shows each agent's changed system instruction once per turn.

    An instruction is shown on the first event the agent authors under it, and
    again whenever it differs from the last instruction shown for that agent.
    Until an instruction is shown, the agent's baseline counts as shown, so the
    baseline itself appears only when the agent returns to it after a change.
    Create one tracker per turn.
    """

    def __init__(self, baselines: Mapping[str, str | None]) -> None:
        """Initializes the tracker for one turn.

        Args:
            baselines: Baseline system instructions keyed by agent name, from
                each agent's `AgentConfig`. Read at call time rather than copied,
                so agents added during the turn are picked up.
        """
        self._baselines = baselines
        self._shown: dict[str, str] = {}

    def build_state_delta(
        self, agent: str | None, author: str | None, instruction: str | None
    ) -> dict[str, Any] | None:
        """Builds the `state_delta` for one event when it must show the instruction.

        Only events authored by `agent` carry the instruction. Events by `user`
        or another author return None without marking the instruction as shown,
        so `agent`'s next own event under that instruction still carries it.
        Returning a delta marks the instruction as shown, so call this once per
        event in event order.

        Args:
            agent: Agent that owns the instruction. None or empty returns None
                without updating state.
            author: Author of the event.
            instruction: Active instruction `agent` ran under for this event
                (including when equal to the baseline), or None if unknown.
                None returns None without updating state.

        Returns:
            `{SYSTEM_INSTRUCTION_KEY: instruction}` when the event must show the
            instruction, or None.
        """
        if not agent or instruction is None or author != agent:
            return None
        last_shown = self._shown.get(agent, self._baselines.get(agent))
        if instruction == last_shown:
            return None
        self._shown[agent] = instruction
        return {SYSTEM_INSTRUCTION_KEY: instruction}
