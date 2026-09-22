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
"""Editing the shopper profile -- and the widget refresh that follows.

This tool is where the staging design earns its keep. Changing a price
ceiling should visibly re-rank the cards the shopper is looking at, but the
model is not calling the tool that built those cards. Because the pick
payload and its original query are in session state, this tool can re-rank
and re-stage them itself, and the flush emits the updated carousel alongside
the confirmation. There is no way to express that by rendering inline from
whichever tool the model happened to call.

The write itself goes through ``profile.update_preference``, which coerces
and refuses. A preference store that accepts anything a model offers is a
ranking that silently stops working.
"""

from __future__ import annotations

from typing import Any

from google.adk.tools import ToolContext

from ..profile import EDITABLE_FIELDS, PreferenceError, update_preference
from .picks import restage_picks


def update_shopper_preference(
    field: str,
    value: str,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Updates one stored shopper preference.

    Preferences persist across sessions. If a pick carousel is staged, it is
    re-ranked against the new preference in the same turn. Whether the
    shopper will actually see a difference is in the summary -- follow it,
    rather than assuming the cards updated.

    Args:
      field: Which preference to set. One of: shoe_size, apparel_size,
        price_ceiling, favorite_brands, preferred_categories, colors,
        avoid_materials, needs_waterproof.
      value: The new value. Lists accept a comma-separated string;
        needs_waterproof accepts yes or no; price_ceiling accepts a number.

    Returns:
      What was stored, and how many pick cards were refreshed as a result.
    """
    try:
        label, stored = update_preference(tool_context.state, field, value)
    except PreferenceError as exc:
        return {
            "status": "rejected",
            "widget": None,
            "editable_fields": sorted(EDITABLE_FIELDS),
            "summary": str(exc),
        }

    refresh = restage_picks(tool_context)

    summary = f"Set {label} to {_readable(stored)}."
    if refresh.changed:
        summary += f" Re-ranked {refresh.cards} pick cards against it."
    elif refresh.suppressed:
        summary += (
            " The cards already on screen are unchanged by it -- say so "
            "rather than implying they updated."
        )
    elif refresh.cards:
        # Identical re-rank, but the carousel has not gone out yet, so it
        # still ships. Telling the model the cards are "unchanged" here would
        # have it deny an update beside cards the shopper is seeing for the
        # first time -- the same misdescription as the suppressed case, aimed
        # the other way. "Going out with this reply" rather than "staged this
        # turn", because a revived carousel was staged several turns ago and
        # only the delivery is happening now.
        #
        # A statement, with no directive attached: an earlier call in the same
        # turn may have genuinely re-ranked these cards, and this branch cannot
        # tell. "Do not call them an update" would then contradict that call's
        # own "Re-ranked 3 pick cards against it". What is true either way is
        # that the arriving cards account for this change, and how much to say
        # about them is the presentation contract's job, not this summary's.
        summary += (
            f" The {refresh.cards} cards going out with this reply already "
            "reflect it."
        )

    return {
        "status": "ok",
        # A carousel goes out whenever one is staged and nothing held it back,
        # which includes the re-rank that came out identical in the turn that
        # built it. Keyed off ``changed`` alone this said "no widget" while a
        # widget was on its way, and the presentation contract -- which reads
        # state, not this result -- was telling the model to write for one at
        # the same time. Two instructions, opposite directions, one turn.
        "widget": (
            "picks" if refresh.cards and not refresh.suppressed else None
        ),
        "field": field,
        "stored_value": stored,
        # Cards *this change* altered, so ``0`` covers three cases: nothing
        # staged, a suppressed resend, and an identical re-rank that ships
        # anyway. The summary is what distinguishes them, because the
        # difference is a sentence, not a number.
        "picks_refreshed": refresh.cards if refresh.changed else 0,
        "summary": summary,
    }


def _readable(value: Any) -> str:
    """Renders a stored value the way someone would say it back."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)
