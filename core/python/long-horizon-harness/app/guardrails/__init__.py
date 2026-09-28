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

"""Tool guardrails for the ADK agent — two callback families.

**before_tool tool-guards (the risky-operation gate).** Each is an ADK
``before_tool_callback`` with the same contract:

    async def guard(*, tool, args, tool_context) -> dict | None

Return ``None`` to allow the tool; return a ``dict`` (carrying an ``error``
key) to block it — ADK surfaces that dict to the model as the tool result, so
the call never runs. They are registered as an *ordered* list in ``agent.py``'s
``before_tool_callback`` and run top-to-bottom; later guards only see what
earlier ones allowed:

* ``exfil_guard`` (Layer A) — blocks secret-bearing / upload-shaped calls to
  non-allowlisted hosts. The before_tool shell is the transferable part; the
  bulk of the file is exfil-detection heuristics.
* ``policies_guard`` (Layer C) — hard-deny + confirmation from tenant policy.
* ``permission_guard`` (Layer D) — the interactive allow/ask layer; runs last.

To write your own, copy the contract above: one ``async`` function with that
signature returning ``None`` or an ``{"error": ...}`` dict, added to the
``before_tool_callback`` list. Deep dives: ``docs/security-model.md`` (the
per-layer model) and ``docs/permission-model.md``.

**Halt guards** — three thin shapes wired into other ADK slots, all
publishing/reading one state key (``HALT_REASON_STATE_KEY``) so a single
user-visible halt cause surfaces regardless of which fired:

* ``RepeatedFailureGuard`` — ``after_tool_callback`` that halts when the
  SAME (tool_name, canonical args) keeps failing.
* ``NoProgressGuard`` — ``after_model_callback`` that halts when the model
  emits identical text ``window`` turns in a row.
* ``halt_consumer_callback`` — ``before_model_callback`` that short-
  circuits with an ``LlmResponse`` when ``halt_reason`` is set.
"""

from typing import Any

from app.guardrails.exfil_config import (
    EXFIL_OVERLAY_FILENAME,
    load_exfil_config,
)
from app.guardrails.exfil_guard import evaluate_exfil, exfil_guard
from app.guardrails.guardrails_plugin import GuardrailsPlugin
from app.guardrails.halt_consumer import (
    HALT_REASON_STATE_KEY,
    acknowledge_halt,
    halt_consumer_callback,
    reset_halt_handoff,
)
from app.guardrails.no_progress import NoProgressGuard
from app.guardrails.policies import (
    DEFAULT_POLICIES_FILENAME,
    load_policies,
    policies_guard,
)
from app.guardrails.policy_grants import (
    POLICY_GRANTS_STATE_KEY,
    policy_grant,
)
from app.guardrails.repeated_failure import (
    LAST_ERROR_STATE_KEY,
    RepeatedFailureGuard,
)


def clear_halt_state(state: Any) -> None:
    """Reset every halt-related signal at a user-turn boundary.

    Without this, ``halt_reason`` latches forever: a transient hiccup
    sets it, ``halt_consumer_callback`` short-circuits every subsequent
    model call, and the streak counters stay at-threshold so even
    clearing the reason alone would re-trip the halt on the next event.
    Called from ``on_session_start_callback`` on every invocation so
    each new user message gives the agent a fresh chance.
    """
    acknowledge_halt(state)
    reset_halt_handoff(state)
    RepeatedFailureGuard.reset(state)
    NoProgressGuard.reset(state)


__all__ = [
    "DEFAULT_POLICIES_FILENAME",
    "EXFIL_OVERLAY_FILENAME",
    "HALT_REASON_STATE_KEY",
    "LAST_ERROR_STATE_KEY",
    "POLICY_GRANTS_STATE_KEY",
    "GuardrailsPlugin",
    "NoProgressGuard",
    "RepeatedFailureGuard",
    "acknowledge_halt",
    "clear_halt_state",
    "evaluate_exfil",
    "exfil_guard",
    "halt_consumer_callback",
    "load_exfil_config",
    "load_policies",
    "policies_guard",
    "policy_grant",
    "reset_halt_handoff",
]
