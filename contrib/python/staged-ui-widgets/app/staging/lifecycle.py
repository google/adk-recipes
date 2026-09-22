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
"""The flush: one pass at the end of the turn that decides what ships.

ADK lets a tool call ``render_ui_widget`` directly, so the obvious question
is why a staging layer exists at all. Three verified behaviours of ADK 2.7
answer it.

**Duplicate widget ids survive parallel tool calls.** ``render_ui_widget``
rejects a duplicate id (``agents/context.py:1010``), but each function call
gets its own ``ToolContext`` (``flows/llm_flows/functions.py:1228``), so the
check only ever sees one call's widgets. When the model calls two tools in
parallel and both render the same id,
``merge_parallel_function_response_events``
(``flows/llm_flows/functions.py:1526``) concatenates the two lists into one
without re-checking ids (``:1545-1562``) and the duplicate ships. Flushing
once, from one context, is where that check actually bites.

**Emission order follows the model, not the design.** Inline rendering emits
in whatever order the model happened to call the tools. Walking
``WIDGET_SPECS`` makes the order a property of the code.

**A tool that is not called cannot render.** Reviving a widget staged three
turns ago has no inline equivalent -- the flush reads the register, so the
producing tool need not run again.

And the trap that makes this file worth reading twice: emitting a widget from
a callback requires a state write. ``render_ui_widget`` mutates event
*actions*, and ``base_agent`` (``agents/base_agent.py:564-582``) only
produces an event when the callback returned content or
``state.has_delta()``. A callback that renders and writes nothing returns
``None``, no event is created, and the widget vanishes with no error
anywhere. ``mark_emitted`` is the write that prevents it.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from ..catalog import catalog_id
from ..render.registry import CONVERTERS, Converter, build_widget
from .gates import (
    ALREADY_EMITTED,
    EMITTED,
    EMPTY_REGISTER,
    NOT_STAGED,
    NOTHING_RENDERED,
    RENDER_FAILED,
    SUPPRESSED,
    blocking_reason,
)
from .spec import all_specs
from .state import mark_emitted, register_payload

logger = logging.getLogger(__name__)

# The gates and their reason strings live in ``gates.py`` because the contract
# resolver needs the same predicate before the model speaks. Re-exported here
# so the flush's vocabulary can still be imported from the module that uses
# it.
__all__ = [
    "ALREADY_EMITTED",
    "EMITTED",
    "EMPTY_REGISTER",
    "NOTHING_RENDERED",
    "NOT_STAGED",
    "RENDER_FAILED",
    "SUPPRESSED",
    "WIDGET_PROVIDER",
    "EmissionOutcome",
    "WidgetContext",
    "blocked_emissions",
    "emit_staged_widgets",
    "log_flush",
]

# The ``UiWidget.provider`` value. ADK treats provider as an opaque routing
# key: ``events/ui_widget.py`` documents 'mcp' as the one known value and
# defines no A2UI renderer on this channel, so this string is a contract
# between this agent and whatever host renders its surfaces. The test suite
# validates the payload against the published A2UI schema, which is the part
# a host actually depends on.
#
# Worth knowing before reaching for ``adk web``: its bundled UI *does* ship a
# full A2UI renderer (``a2ui-surface``, ``a2ui-card`` and the rest), but it
# feeds that renderer from ``<a2ui-json>`` blocks found in model text and
# from content parts carrying an ``a2ui`` field. It reads
# ``actions.render_ui_widgets`` nowhere, for any provider. So the agent runs
# fine under ``adk web`` and every widget stays invisible there -- see the
# README's Requirements note.
WIDGET_PROVIDER = "a2ui"

# A spec names its converter with a bare string, and ``resolve_converter``
# deliberately falls back to a generic card for an unknown type -- which is
# right for a type invented at runtime, and wrong for a typo in
# ``WIDGET_SPECS``. Without this check, misspelling ``spend_trend`` costs the
# shopper the chart and logs one INFO line. Checked here because this module
# is the one that would degrade, and because it already imports both sides.
_UNCOVERED = sorted(
    spec.semantic_type
    for spec in all_specs()
    if spec.semantic_type not in CONVERTERS
)
if _UNCOVERED:
    raise ValueError(
        "declared widgets have no converter: "
        + ", ".join(_UNCOVERED)
        + "; add one to render.registry.CONVERTERS or fix the semantic_type"
    )


@dataclass(frozen=True)
class EmissionOutcome:
    """What happened to one widget during one flush."""

    name: str
    emitted: bool
    reason: str


class WidgetContext(Protocol):
    """The slice of ADK's ``Context`` this module needs.

    Narrowing to two members keeps the flush testable with a stub -- no
    runner, no session service, no invocation context.
    """

    @property
    def state(self) -> Any: ...

    def render_ui_widget(self, ui_widget: Any) -> None: ...


def emit_staged_widgets(
    ctx: WidgetContext,
    *,
    overrides: Mapping[str, Converter] | None = None,
) -> list[EmissionOutcome]:
    """Renders every widget that clears all six gates.

    Call this from ``after_agent_callback``, after the model has finished
    speaking, so the widgets accompany the reply they belong to.

    Returns one outcome per declared widget, emitted or not, because "why is
    my widget missing" is the question this framework gets asked most and a
    silent skip is a bad answer.
    """
    # Imported lazily so the staging tests can exercise the gates without
    # pulling in ADK's event machinery.
    from google.adk.events.ui_widget import UiWidget

    state = ctx.state
    cid = catalog_id()
    outcomes: list[EmissionOutcome] = []

    for spec in all_specs():
        reason = blocking_reason(state, spec)
        if reason is not None:
            outcomes.append(EmissionOutcome(spec.name, False, reason))
            continue

        payload = register_payload(state, spec)
        widget = build_widget(
            spec.semantic_type,
            payload,
            surface_id=spec.surface_id,
            catalog_id=cid,
            overrides=overrides,
        )
        if widget is None:
            # build_widget already logged the cause. Leave the register
            # intact: the data may be fine and the converter at fault.
            outcomes.append(EmissionOutcome(spec.name, False, NOTHING_RENDERED))
            continue

        try:
            ctx.render_ui_widget(
                UiWidget(
                    id=spec.widget_id,
                    provider=WIDGET_PROVIDER,
                    payload=widget,
                )
            )
        except ValueError:
            # A duplicate widget id within one flush means two specs claim
            # the same id, which spec.py rejects at import. Defensive: one
            # bad widget must not cost the shopper the whole reply.
            logger.exception(
                "host rejected widget %s; continuing with the rest",
                spec.widget_id,
            )
            outcomes.append(EmissionOutcome(spec.name, False, RENDER_FAILED))
            continue

        # The write that turns a rendered widget into a delivered one.
        mark_emitted(state, spec)
        outcomes.append(EmissionOutcome(spec.name, True, EMITTED))

    return outcomes


def blocked_emissions(
    outcomes: list[EmissionOutcome],
) -> list[EmissionOutcome]:
    """Widgets a tool asked for that did not ship.

    Excludes the widgets nobody staged, which are the overwhelming majority
    of every flush and carry no signal. What remains is worth a log line: the
    model may have told the shopper about something they cannot see.
    """
    return [
        outcome
        for outcome in outcomes
        if not outcome.emitted and outcome.reason != NOT_STAGED
    ]


def log_flush(outcomes: list[EmissionOutcome]) -> None:
    """Logs the interesting half of a flush at a level worth reading."""
    emitted = [o.name for o in outcomes if o.emitted]
    if emitted:
        logger.info("emitted widgets: %s", ", ".join(emitted))
    for outcome in blocked_emissions(outcomes):
        logger.warning(
            "widget %s staged but not emitted: %s",
            outcome.name,
            outcome.reason,
        )
