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

"""The working copy of one agent turn.

The agent starts each turn from the saved studio (``studio.state``). Its
tools edit the turn's working copy and record patches, which go back to the
app in the ``studio_agent`` result and are applied to the saved state at the
end of the turn (``state.save_patched``), so what the user changed in the
panel meanwhile is kept.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from typing import Any

__all__ = ["AgentTurn", "Scope", "current", "end", "patch", "start"]


@dataclass
class AgentTurn:
    """What the agent did in one turn: its reply and the app patches."""

    reply: str = ""
    patches: list[dict[str, Any]] = field(default_factory=list)
    #: The ``updated_at`` of the state saved at the end of the turn.
    saved_at: float = 0.0


@dataclass
class Scope:
    """The working copy of one agent turn."""

    studio_id: str
    st: dict[str, Any]
    #: The latest generation (``last_results``), read once per turn.
    results: dict[str, Any] = field(default_factory=dict)
    out: AgentTurn = field(default_factory=AgentTurn)
    #: A ``generate`` call is checking whether it may start a job.
    starting: bool = False
    #: How many setup tools (``tool_order.setup_tool``) are running.
    setting_up: int = 0


_SCOPE: contextvars.ContextVar[Scope] = contextvars.ContextVar("studio_turn")


def current() -> Scope:
    """The turn being run."""
    return _SCOPE.get()


def start(scope: Scope) -> contextvars.Token:
    """Make ``scope`` the current turn; pass the token to ``end``."""
    return _SCOPE.set(scope)


def end(token: contextvars.Token) -> None:
    """Close the current turn."""
    _SCOPE.reset(token)


def patch(op: str, value: Any) -> None:
    """Record a patch for the app."""
    current().out.patches.append({op: value})
