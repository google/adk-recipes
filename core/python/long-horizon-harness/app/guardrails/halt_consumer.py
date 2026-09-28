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

"""``before_model_callback`` that short-circuits when a guard halted the session."""

from __future__ import annotations

from typing import Any

from google.adk.models import LlmResponse
from google.genai.types import Content, Part

HALT_REASON_STATE_KEY = "halt_reason"
HALT_HANDOFF_DELIVERED_STATE_KEY = "__halt_handoff_delivered__"


def halt_content(reason: str) -> Content:
    """Canonical ``[halted: <reason>]`` envelope shared by all halt paths."""
    return Content(role="model", parts=[Part(text=f"[halted: {reason}]")])


async def halt_consumer_callback(
    *,
    callback_context: Any,
    llm_request: Any,
) -> LlmResponse | None:
    reason = callback_context.state.get(HALT_REASON_STATE_KEY)
    if not reason:
        return None

    from app.conversation.graceful_halt import deliver_halt_or_envelope

    return deliver_halt_or_envelope(
        llm_request,
        callback_context.state,
        reason=reason,
        flag_key=HALT_HANDOFF_DELIVERED_STATE_KEY,
    )


def acknowledge_halt(state: Any) -> None:
    """Clear the halt signal so the next model call is not short-circuited.

    Owned by a wrapper that decides the user has acknowledged the halt
    (typically the user-turn boundary). ADK's ``State`` exposes no
    ``pop`` / ``del``, so we overwrite with ``None`` — readers use
    ``state.get(...)`` and ``if not reason`` already treats ``None`` as
    absent.
    """
    state[HALT_REASON_STATE_KEY] = None


def reset_halt_handoff(state: Any) -> None:
    """Clear the once-per-halt handoff flag at a user-turn boundary.

    ADK ``State`` has no ``pop``/``del``, so overwrite with ``None``;
    readers use ``state.get(...)`` which treats ``None`` as absent.
    """
    state[HALT_HANDOFF_DELIVERED_STATE_KEY] = None


def latch_halt(state: Any, reason: str) -> bool:
    """Set ``halt_reason`` only if no halt is already latched.

    Returns True when this call wrote the reason. Lets guards express
    "halt unless another guard beat me to it" without duplicating the
    check-and-set dance at every call site.
    """
    if state.get(HALT_REASON_STATE_KEY):
        return False
    state[HALT_REASON_STATE_KEY] = reason
    return True
