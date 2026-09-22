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
"""The staging API a tool calls: put a payload on the board, don't draw it.

A tool's job ends at "here is what should be shown". It writes a payload to
the widget's register and raises a dirty flag; the flush at the end of the
turn decides what actually goes out.

Every function here takes a plain ``MutableMapping`` rather than a
``Context``. ADK's ``State`` satisfies it, and so does ``{}`` -- which is why
the staging tests need no ADK runner at all.

Two scopes carry the whole mechanism:

*Registers are session-scoped.* A payload staged three turns ago is still
readable, which is what makes ``revive_widget`` free -- no recomputation, no
second trip through the ranking.

*Dirty, suppress, and revived flags are ``temp:``-scoped.* ADK applies temp
deltas only
to the transient per-invocation session copy and trims them from the
persisted event, so they cannot survive into the next turn. That is verified
behaviour, not an assumption -- but the emitted flag, not temp expiry, is
what actually prevents a repeat emission. See ``lifecycle``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, MutableMapping
from typing import Any

from .spec import StagedWidgetSpec, spec_for

logger = logging.getLogger(__name__)

State = MutableMapping[str, Any]


def stage_widget(state: State, name: str, payload: Mapping[str, Any]) -> None:
    """Stages a payload for this turn's flush.

    Clears the emitted flag, because a new payload is new information: the
    shopper should see it even if an older payload for the same widget
    already went out earlier in the conversation. Clears the revived and
    suppress flags too, for the reasons in the comments below.
    """
    spec = spec_for(name)
    state[spec.register_key] = dict(payload)
    state[spec.dirty_key] = True
    # New data supersedes whatever was shown before.
    state[spec.emitted_key] = False
    # Fresh data, so this is not a reprise even if an earlier tool in the same
    # turn revived the widget first. Cleared rather than left alone: the
    # revived flag decides how the model is told to talk about the widget, and
    # new data deserves a full description.
    state[spec.revived_key] = False
    # Same reason, for the veto: an earlier tool in this turn may have decided
    # a carousel would be noise, and staging is a later, more specific decision
    # that there is something worth seeing. Left set, it would veto the fresh
    # payload and the flush would report ``suppressed for this turn`` for a
    # widget the shopper did just get new data for. The last explicit decision
    # wins -- suppressing *after* staging still suppresses, which is the order
    # the no-op refresh in ``tools/picks.py`` uses.
    state[spec.suppress_key] = False


def revive_widget(state: State, name: str) -> bool:
    """Re-shows the payload already in the register, without recomputing it.

    This is the case a tool rendering inline cannot serve: the shopper says
    "show me those picks again" two turns later, and the tool that produced
    them is not being called. The payload is still in session state, so
    reviving costs nothing.

    Returns ``False`` when the register is empty -- there is nothing to
    revive, and the caller should say so rather than imply a widget appeared.
    """
    spec = spec_for(name)
    if not state.get(spec.register_key):
        return False
    state[spec.dirty_key] = True
    state[spec.emitted_key] = False
    # Cleared for the same reason ``stage_widget`` clears it: the shopper
    # asking to see something again is a later and more specific decision
    # than an earlier tool's judgement that it would be noise. Left set, the
    # flush would answer "bring those back up" with ``suppressed for this
    # turn`` and no carousel.
    state[spec.suppress_key] = False
    # Otherwise a revival and a fresh staging leave identical state, and the
    # model would describe the widget from scratch a second time. Read by the
    # contract resolver, not by the flush -- both paths emit the same way.
    state[spec.revived_key] = True
    return True


def suppress_widget(state: State, name: str) -> None:
    """Vetoes this widget for this turn.

    The payload stays in the register, so a later turn can still revive it.
    Used when a widget would be redundant -- the shopper asked a narrow
    follow-up question and a full carousel would be noise.
    """
    spec = spec_for(name)
    state[spec.suppress_key] = True


def clear_staged(state: State, name: str) -> None:
    """Empties the register, so nothing can be revived.

    Distinct from ``suppress_widget``: this discards the data rather than
    hiding it for a turn.
    """
    spec = spec_for(name)
    state[spec.register_key] = {}
    state[spec.dirty_key] = False
    state[spec.emitted_key] = False
    state[spec.revived_key] = False


def register_payload(
    state: Mapping[str, Any], spec: StagedWidgetSpec
) -> dict[str, Any]:
    """The staged payload, or ``{}`` when nothing usable is stored.

    A non-mapping in the register means something else wrote the key. Return
    empty rather than raising -- gate 4 then declines to emit, and the turn
    survives.
    """
    payload = state.get(spec.register_key)
    if isinstance(payload, Mapping):
        return dict(payload)
    if payload is not None:
        logger.warning(
            "register %s holds %s, not a mapping; ignoring",
            spec.register_key,
            type(payload).__name__,
        )
    return {}


def is_dirty(state: Mapping[str, Any], spec: StagedWidgetSpec) -> bool:
    """Whether a tool staged or revived this widget during this turn."""
    return bool(state.get(spec.dirty_key))


def is_suppressed(state: Mapping[str, Any], spec: StagedWidgetSpec) -> bool:
    """Whether this widget is vetoed for this turn."""
    return bool(state.get(spec.suppress_key))


def was_revived(state: Mapping[str, Any], spec: StagedWidgetSpec) -> bool:
    """Whether this turn re-showed stored data rather than staging new data."""
    return bool(state.get(spec.revived_key))


def was_emitted(state: Mapping[str, Any], spec: StagedWidgetSpec) -> bool:
    """Whether the payload currently in the register has already been sent."""
    return bool(state.get(spec.emitted_key))


def mark_emitted(state: State, spec: StagedWidgetSpec) -> None:
    """Records that the widget went out.

    This single write does two jobs. It is the dedupe record that stops the
    next turn resending an unchanged widget, and -- because it is a
    session-scoped state delta -- it is what makes ADK produce an event at
    all. Without it, ``render_ui_widget`` alone mutates only event *actions*,
    ``state.has_delta()`` stays false, and ``base_agent`` returns ``None``
    instead of an event, discarding the widget silently.
    """
    state[spec.emitted_key] = True
