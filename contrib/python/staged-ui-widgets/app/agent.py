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
"""The agent: six tools that stage, four callbacks that bracket the turn.

``before_agent_callback`` makes sure a profile exists.
``after_agent_callback`` flushes whatever the tools staged. Nothing in
between renders anything.

The other two callbacks are about the *reply*, not the widgets, and they
bracket the model rather than the agent. ``before_model_callback`` appends the
presentation contract for whatever is about to ship, and
``after_model_callback`` puts a floor under a reply that came back empty. A
widget with no words beside it is a worse outcome than a plain answer, so the
turn is shaped from both sides.

### The trap this file exists to demonstrate

``after_agent_callback`` returning ``None`` normally means "I did not override
the reply", which is what a callback that only emits widgets wants to say. But
ADK creates an event for a callback only when it returned content **or** the
callback's state changed (``agents/base_agent.py:564-582``):

    if after_agent_callback_content:
        return Event(..., actions=callback_context._event_actions)
    if callback_context.state.has_delta():
        return Event(..., actions=callback_context._event_actions)
    return None

``render_ui_widget`` appends to ``_event_actions`` -- not to state. So a
callback that renders three widgets and writes nothing produces no event, no
widgets reach the client, and nothing raises. It looks exactly like a turn in
which no widget was staged.

``emit_staged_widgets`` writes an emitted flag per widget, which serves as the
dedupe record *and* the state delta that forces the event out.
``tests/unit/test_event_delivery.py`` locks the behaviour down with a
runner-level regression test, because the failure mode is silent and a future
refactor could reintroduce it without breaking anything visible.
"""

from __future__ import annotations

import logging
import os

from google.adk.agents import Agent
from google.adk.agents.callback_context import CallbackContext
from google.adk.apps import App
from google.adk.models import Gemini
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from .presentation import instruction_for
from .profile import PROFILE_KEY, load_profile, save_profile
from .prompt import build_instruction
from .staging import (
    emit_staged_widgets,
    live_specs,
    log_flush,
    resolve_contract,
)
from .tools import ALL_TOOLS

logger = logging.getLogger(__name__)

# Model default matches .env.example, so `adk run app` works from a fresh
# clone with only a credential set.
_DEFAULT_MODEL = "gemini-3.5-flash"


def seed_profile(callback_context: CallbackContext) -> None:
    """Ensures the shopper profile exists before the model runs.

    ``user:``-scoped state persists across sessions, so this writes only on a
    genuinely first visit. Writing unconditionally would overwrite
    preferences the shopper set last week -- and, because any state write
    forces an event, would also emit a pointless event on every single turn.
    """
    if PROFILE_KEY not in callback_context.state:
        save_profile(
            callback_context.state, load_profile(callback_context.state)
        )
        logger.info("seeded default shopper profile")


def apply_presentation_contract(
    callback_context: CallbackContext, llm_request: LlmRequest
) -> None:
    """Appends the reply contract for whatever is about to be shown.

    Runs before every model call in the turn, which is the point of it: on the
    first call nothing is staged yet, so nothing is appended and the agent
    behaves normally. By the call that produces the visible reply the tools
    have staged, the contract resolves, and the instruction arrives describing
    the widgets that are actually about to ship.

    ``append_instructions`` concatenates onto the *end* of the system
    instruction, which is where a model weights output-shaping directives most
    heavily -- the same reason the block is not folded into
    ``prompt.build_instruction``, where it would sit far above the
    conversation and compete with everything else.

    Returns ``None`` so the model call proceeds. Returning an ``LlmResponse``
    here would skip the model entirely.
    """
    contract = resolve_contract(callback_context.state)
    block = instruction_for(contract)
    if not block:
        return

    # A turn with two rounds of tool calls reaches this callback twice. The
    # instruction is idempotent in meaning but not in cost, and a duplicated
    # block reads to the model as emphasis it did not earn.
    existing = llm_request.config.system_instruction
    if isinstance(existing, str) and block in existing:
        return

    llm_request.append_instructions([block])
    logger.info("presentation contract: %s", contract)


def ensure_widget_companion(
    callback_context: CallbackContext, llm_response: LlmResponse
) -> LlmResponse | None:
    """Puts a floor under an empty reply when a widget is about to ship.

    The presentation contract tells the model to say less. Taken to its
    conclusion by a model having an off moment, "less" becomes nothing at all,
    and the shopper gets a carousel with no voice beside it -- which reads as a
    bug even though every widget is correct.

    Shaping cannot prevent that, only a floor can. This is deliberately not
    ``skip_summarization``: suppressing the model's reply outright would
    guarantee the bare-widget outcome instead of guarding against it.

    Returns ``None`` in every ordinary case, including the common one where
    the model wrote something. A returned response replaces the original
    wholesale -- ``base_llm_flow`` rebinds to whatever the callback hands back
    (``flows/llm_flows/base_llm_flow.py:1558-1565``) -- so it is built with
    ``model_copy`` to keep usage metadata and finish reason intact.

    No state write is needed here, unlike the flush: this is the model
    response event, which exists because the model responded.
    """
    # A streaming chunk, an error, or a live-mode turn marker. None of them is
    # the finished reply, and rewriting one would corrupt the stream.
    if llm_response.partial or llm_response.turn_complete:
        return None
    if llm_response.error_code:
        return None

    parts = (llm_response.content.parts or []) if llm_response.content else []
    # The model is calling tools, not replying. The reply comes on a later
    # model call in the same turn.
    if any(part.function_call for part in parts):
        return None
    if any((part.text or "").strip() for part in parts):
        return None

    live = live_specs(callback_context.state)
    if not live:
        return None

    companion = " ".join(spec.default_companion for spec in live)
    logger.info("empty reply beside a live widget; using default companion")
    return llm_response.model_copy(
        update={
            "content": types.Content(
                role="model", parts=[types.Part(text=companion)]
            )
        }
    )


def flush_widgets(callback_context: CallbackContext) -> None:
    """Emits every staged widget that clears the gates.

    Returns ``None`` so the model's reply stands unmodified. The event that
    carries the widgets exists because ``emit_staged_widgets`` wrote state --
    see this module's docstring.
    """
    outcomes = emit_staged_widgets(callback_context)
    log_flush(outcomes)


def create_agent() -> Agent:
    """Creates a fresh, isolated instance of the Agent."""
    return Agent(
        name="root_agent",
        model=Gemini(
            model=os.getenv("MODEL_NAME", _DEFAULT_MODEL),
            retry_options=types.HttpRetryOptions(attempts=3),
        ),
        instruction=build_instruction,
        tools=ALL_TOOLS,
        before_agent_callback=seed_profile,
        before_model_callback=apply_presentation_contract,
        after_model_callback=ensure_widget_companion,
        after_agent_callback=flush_widgets,
    )


root_agent = create_agent()

app = App(
    root_agent=root_agent,
    name="app",
)
