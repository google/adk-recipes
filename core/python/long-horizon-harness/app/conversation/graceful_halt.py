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

"""Graceful budget/guardrail halt: one final text-only handoff turn.

Instead of short-circuiting the model with a bare `[halted: ...]`
response, the halt consumers call apply_graceful_halt to strip tools and
inject a handoff reminder, then return None so the model produces a
final text turn: what it did, what remains, what it recommends.

Modeled on opencode's approach — but adapted to ADK: opencode
disables tools at the harness layer; here we strip tools off the
per-turn LlmRequest and let the model run once more.
"""

from __future__ import annotations

from typing import Any

from google.adk.models import LlmRequest, LlmResponse
from google.genai import types

from app.guardrails.halt_consumer import halt_content

HANDOFF_MARKER = "<system-reminder>"

_HANDOFF_TEMPLATE = (
    "<system-reminder>\n"
    "STOP — this turn's budget has been reached ({reason}). Tools are now "
    "disabled; you cannot make any more tool calls this turn. Respond with "
    "text ONLY.\n\n"
    "Your reply MUST contain:\n"
    "1. A short summary of the work completed so far this turn.\n"
    "2. The remaining tasks that were not finished.\n"
    "3. Concrete recommendations for what to do next (the user can reply "
    "to continue in a fresh turn with a fresh budget).\n\n"
    "Do not apologize at length and do not attempt any tool call — it will "
    "be rejected.\n"
    "</system-reminder>"
)


def build_handoff_reminder(reason: str) -> str:
    return _HANDOFF_TEMPLATE.format(reason=reason)


def apply_graceful_halt(llm_request: LlmRequest, *, reason: str) -> None:
    """Strip tools and append the text-only handoff reminder to the tail."""
    llm_request.tools_dict = {}
    config = llm_request.config
    if getattr(config, "tools", None):
        config.tools = []
    if getattr(config, "tool_config", None) is not None:
        config.tool_config = None

    llm_request.contents.append(
        types.Content(
            role="user",
            parts=[types.Part(text=build_handoff_reminder(reason))],
        )
    )


def deliver_halt_or_envelope(
    llm_request: LlmRequest, state: Any, *, reason: str, flag_key: str
) -> LlmResponse | None:
    """First halted turn: run the graceful handoff (strip tools + inject the
    reminder) and return None so the model gets one final text turn. Later
    turns: return the bare ``[halted: <reason>]`` envelope. ``flag_key`` is the
    per-consumer state key tracking whether the handoff has already fired."""
    if not state.get(flag_key):
        apply_graceful_halt(llm_request, reason=reason)
        state[flag_key] = True
        return None
    return LlmResponse(content=halt_content(reason))


__all__ = [
    "HANDOFF_MARKER",
    "apply_graceful_halt",
    "build_handoff_reminder",
    "deliver_halt_or_envelope",
]
