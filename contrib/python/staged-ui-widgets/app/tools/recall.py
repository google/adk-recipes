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
""" "Bring those back up" -- re-showing a widget without rebuilding it.

The tool with no inline equivalent whatsoever. Every other tool here computes
something and stages the result; this one computes nothing. It flips four
flags -- dirty, emitted and suppress for the flush, revived for the contract
resolver -- and the flush re-emits a payload that has been sitting in session
state for however many turns.

Worth having as a real tool rather than a framework demo, because the
alternative is a product bug. Without it, "show me those shoes again" is
answered by running the search a second time -- and a search re-run against a
profile the shopper has since edited returns *different* shoes than the ones
they asked to see again. The register holds the actual carousel they are
talking about.
"""

from __future__ import annotations

from typing import Any

from google.adk.tools import ToolContext

from ..staging import revive_widget
from ..staging.spec import all_specs
from ..staging.state import register_payload

# Shopper words mapped to widget names. A model reaching for this tool has
# paraphrased whatever the shopper said, so match on substrings of the
# request rather than demanding an exact token: "the shoe cards" and
# "products" both mean the carousel.
_ALIASES: tuple[tuple[str, str], ...] = (
    ("compar", "comparison"),
    ("table", "comparison"),
    ("side by side", "comparison"),
    ("side-by-side", "comparison"),
    ("pick", "picks"),
    ("product", "picks"),
    ("card", "picks"),
    ("carousel", "picks"),
    ("recommend", "picks"),
    ("shoe", "picks"),
    ("order", "order"),
    ("deliver", "order"),
    ("track", "order"),
    ("shipment", "order"),
    ("spend", "spend"),
    ("spent", "spend"),
    ("chart", "spend"),
    ("budget", "spend"),
)

# How each widget reads in a sentence the model might say back.
_LABELS: dict[str, str] = {
    "picks": "product cards",
    "comparison": "comparison table",
    "order": "order timeline",
    "spend": "spending chart",
}


def show_again(
    what: str,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Re-shows a visual the shopper has already been given.

    Use this when the shopper asks to see something again -- "bring those
    shoes back up", "show me that comparison", "what was my delivery date
    again". It redisplays the exact visual they saw before, so prefer it over
    re-running a search: a fresh search can return different products than
    the ones they are asking about.

    Args:
      what: Which visual to bring back. One of: picks (the product cards),
        comparison (the side-by-side table), order (the delivery timeline),
        spend (the spending chart).

    Returns:
      Which visual was re-shown, or -- when there is nothing stored for it --
      the list of visuals that can be re-shown, so you can offer one of those
      instead of implying something appeared.
    """
    name = _resolve(what)
    if name is None:
        return {
            "status": "rejected",
            "widget": None,
            "available": _available(tool_context),
            "summary": (
                f"{what!r} is not something that can be re-shown. Options "
                "are: picks, comparison, order, spend."
            ),
        }

    if not revive_widget(tool_context.state, name):
        available = _available(tool_context)
        offer = (
            "Nothing has been shown yet this conversation."
            if not available
            else "Can re-show: " + ", ".join(_LABELS[n] for n in available)
        )
        return {
            "status": "empty",
            "widget": None,
            "available": available,
            "summary": (
                f"No {_LABELS[name]} has been shown yet, so there is nothing "
                f"to bring back. {offer}"
            ),
        }

    return {
        "status": "ok",
        "widget": name,
        "available": _available(tool_context),
        # States the fact and stops. No item list and no figures, because the
        # shopper has already seen this payload -- and no instruction about
        # how to phrase the reply either: reviving sets the revived flag, the
        # contract resolver turns that into the ACKNOWLEDGE contract, and the
        # instruction lands at the tail of the system prompt where it carries
        # far more weight than a sentence buried in a tool result.
        "summary": f"Re-showing the {_LABELS[name]} from earlier, unchanged.",
    }


def _resolve(what: str) -> str | None:
    """The widget name behind a shopper's or model's phrasing."""
    cleaned = what.strip().lower()
    if not cleaned:
        return None
    for fragment, name in _ALIASES:
        if fragment in cleaned:
            return name
    return None


def _available(tool_context: ToolContext) -> list[str]:
    """Widget names with a payload in the register, in emission order.

    Returned on every path, including success: the model's next turn is often
    "and the comparison too", and this saves it guessing whether that exists.
    """
    return [
        spec.name
        for spec in all_specs()
        if register_payload(tool_context.state, spec)
    ]
