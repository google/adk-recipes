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
"""The one answer to "is this widget shipping this turn?"

Two places in the turn need that answer, at different times and for different
reasons:

*Before the model speaks*, the contract resolver needs it to decide what the
reply must look like. Getting it wrong here means instructing the model to say
"the chart on screen shows the trend" on a turn where the chart is suppressed.

*After the model speaks*, the flush needs it to decide what to render.

Those two answers must be the same answer, so the predicate lives here and
both callers import it. If it were duplicated, the drift would not show up as
an exception -- it would show up as a reply pointing at a widget that never
arrived, which is precisely the failure the whole recipe exists to avoid.

Two gates sit outside that symmetry, for two different reasons, and both are
documented in ``blocking_reason``: the resolver runs before rendering, so it
cannot know whether the converter will produce components, and a host's
rejection is a verdict on a widget already handed over. Neither can fail
before flush time.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .spec import StagedWidgetSpec
from .state import is_dirty, is_suppressed, register_payload, was_emitted

# Why a widget did not ship. Stable strings: tests assert on them and logs
# are read by humans debugging a missing widget.
NOT_STAGED = "not staged this turn"
ALREADY_EMITTED = "already emitted"
SUPPRESSED = "suppressed for this turn"
EMPTY_REGISTER = "register empty"
NOTHING_RENDERED = "converter produced no components"
RENDER_FAILED = "host rejected the widget"
EMITTED = "emitted"


def blocking_reason(
    state: Mapping[str, Any], spec: StagedWidgetSpec
) -> str | None:
    """The first gate this widget fails, or ``None`` if it clears them all.

    Order matters for the log message, not the outcome: a suppressed widget
    that was never staged reads better as "not staged".

    Covers the four gates that are decidable from state alone. Two more can
    only fail during the flush, for two different reasons:
    ``NOTHING_RENDERED`` needs the converter to have run, and
    ``RENDER_FAILED`` is the host's verdict on a widget already handed over.
    So a ``None`` here means "nothing in state stops this", not "this is
    guaranteed to ship".
    """
    if not is_dirty(state, spec):
        return NOT_STAGED
    if was_emitted(state, spec):
        return ALREADY_EMITTED
    if is_suppressed(state, spec):
        return SUPPRESSED
    if not register_payload(state, spec):
        return EMPTY_REGISTER
    return None


def is_live(state: Mapping[str, Any], spec: StagedWidgetSpec) -> bool:
    """Whether this widget is on course to ship this turn."""
    return blocking_reason(state, spec) is None
