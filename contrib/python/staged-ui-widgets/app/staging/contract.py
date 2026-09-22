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
"""Which reply contract this turn gets, read from staging state.

The join between the two halves of the recipe. ``presentation.py`` knows what
the contracts are and says nothing about staging; ``gates.py`` knows what is
shipping and says nothing about replies. This module asks the first question
of the second module and returns one answer.

It runs *before* the model speaks, from ``before_model_callback``, which is
the only point where the answer can still change what the model writes.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..presentation import (
    PresentationContract,
    PresentationRole,
    contract_for_roles,
)
from .gates import is_live
from .spec import StagedWidgetSpec, all_specs
from .state import was_revived


def live_specs(state: Mapping[str, Any]) -> list[StagedWidgetSpec]:
    """Widgets on course to ship this turn, in emission order."""
    return [spec for spec in all_specs() if is_live(state, spec)]


def role_for(
    state: Mapping[str, Any], spec: StagedWidgetSpec
) -> PresentationRole:
    """This widget's role for this turn, after the one dynamic refinement.

    A widget's declared role describes fresh data. Re-showing stored data is
    a different situation regardless of what the widget is -- the shopper has
    seen it and the model has already described it -- so a revived widget
    becomes a ``REPRISE`` whatever it declared.

    The refinement lives here rather than as a per-spec hook because all four
    widgets would implement it identically. A callable every spec sets to the
    same thing is ceremony, and an unused extension point rots: it is the same
    trap as a widget-specific branch nobody ever takes.
    """
    if was_revived(state, spec):
        return PresentationRole.REPRISE
    return spec.presentation_role


def resolve_contract(
    state: Mapping[str, Any],
) -> PresentationContract | None:
    """The single contract for this turn, or ``None`` for a text-only reply.

    ``None`` is the common case and the important one: on a turn with no live
    widget nothing is appended to the instruction, so the agent behaves
    exactly as it would without this layer.
    """
    return contract_for_roles(
        role_for(state, spec) for spec in live_specs(state)
    )
