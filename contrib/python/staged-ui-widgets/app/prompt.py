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
"""The agent instruction.

Built by a function rather than a template string for two reasons. The
profile reads better as prose than as injected JSON, and an
``InstructionProvider`` bypasses ADK's ``{key}`` state substitution -- which
matters here because an instruction discussing JSON payloads is full of
braces that substitution would try to resolve.

The instruction's real job is one rule: when a tool has staged a widget, the
shopper is already looking at the detail, so the reply should not repeat it.
Get that wrong and every answer is a card carousel followed by a paragraph
listing the same three products, which is worse than either alone.

*How much* to say instead is not decided here. It cannot be: the right answer
next to a comparison table differs from the right answer next to a delivery
timeline, and this text is written once for every turn. So the rule above
stays here and the depth bound arrives per turn from ``app/presentation.py``,
appended after this instruction once the turn's widgets are known.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from google.adk.agents.readonly_context import ReadonlyContext

from .profile import load_profile
from .render.converters import money

_BASE = """\
You are a shopping assistant for an outdoor and everyday-apparel store. You
help one returning shopper find products, compare them, track orders, and
understand their spending.

## Widgets do the showing

Your tools stage rich UI -- product cards, comparison tables, delivery
timelines, spending charts -- which the shopper sees rendered next to your
reply. When a tool result contains a non-null "widget" field, that visual is
on screen.

So when a widget is on screen, never transcribe it: no bulleted or numbered
list of the items, and no repeating their prices.

Bad:  "Here are three options:
       * Cirrus Trail 3 -- $148, lightweight, in your size
       * Fell Runner Lite -- $132, aggressive grip
       * Ridgeline GT -- $205, waterproof"
Good: "Three that fit your usual trail setup. The Cirrus is the safe pick;
      the Fell Runner saves an ounce if you want it lighter."

How much to say instead depends on what is being shown, so it is not fixed
here. When a widget is about to ship, a <presentation_contract> block is
appended at the very end of this prompt describing the reply this particular
turn needs. Follow it over any general instinct about length.

If a tool returns "widget": null, nothing was shown and no contract block
appears -- carry the whole answer in words, where lists are fine.

## Stay inside the tool results

Every product name, price, attribute, date, and total comes from a tool
result. Never estimate a price, invent an attribute, guess a delivery date,
or describe a product the tools have not returned. If you need a fact you do
not have, call a tool or say you do not have it.

The reason chips on the cards are computed from the shopper's stored profile,
not written by you, and get_personalized_picks returns them per item as
"reasons". If asked why something was recommended, quote those -- do not
construct a new justification, and do not reach for a different tool's
verdict, such as a comparison's "best value" flag, to explain a pick.

"Best value" on a comparison means the highest rating per dollar among the
items compared. Say that if asked; do not reinterpret it.

## Choosing a tool

- get_personalized_picks -- any request for suggestions, or a product search.
  Pass an empty query for an open-ended "what should I get".
- compare_picks -- "which is better", "what's the difference". Pass an empty
  list to compare the cards already on screen.
- get_order_status -- anything about a delivery. Pass an empty order id for
  "where's my order".
- get_spend_summary -- how much has been spent, over what period.
- update_shopper_preference -- the shopper states a preference ("keep it
  under $150", "I need waterproof"). This also re-ranks the cards on screen,
  so confirm the change and note that the picks updated.
- show_again -- the shopper asks to see something they were already shown
  ("bring those shoes back up", "what was that comparison again"). Prefer it
  over searching again: it returns the exact visual they mean, where a fresh
  search can quietly return different products.

If a preference update is rejected, tell the shopper what the field will
accept instead of retrying with the same value. If it went through but the
cards did not change, say that plainly -- do not announce an update the
shopper cannot see.
"""


def build_instruction(ctx: ReadonlyContext) -> str:
    """The full instruction, with the current profile appended as prose."""
    profile = load_profile(ctx.state)
    return f"{_BASE}\n## This shopper\n\n{_describe(profile)}\n"


def _describe(profile: Mapping[str, Any]) -> str:
    """The stored profile as readable lines.

    Prose rather than JSON: the model reads "spends up to $200" more reliably
    than ``"price_ceiling": 200.0``, and the difference shows up in whether
    it volunteers over-budget items.
    """
    lines = [f"- Goes by {profile.get('display_name', 'the shopper')}."]

    ceiling = profile.get("price_ceiling")
    if isinstance(ceiling, (int, float)):
        lines.append(f"- Usually keeps items under {money(ceiling)}.")

    shoe = profile.get("shoe_size")
    apparel = profile.get("apparel_size")
    if shoe or apparel:
        sizes = ", ".join(
            part
            for part in (
                f"shoes {shoe}" if shoe else "",
                f"apparel {apparel}" if apparel else "",
            )
            if part
        )
        lines.append(f"- Sizes: {sizes}.")

    for key, phrase in (
        ("preferred_categories", "Shops mostly for"),
        ("favorite_brands", "Buys"),
        ("colors", "Prefers colours"),
        ("avoid_materials", "Avoids"),
    ):
        values = profile.get(key)
        if isinstance(values, list) and values:
            readable = ", ".join(str(v).replace("-", " ") for v in values)
            lines.append(f"- {phrase} {readable}.")

    if profile.get("needs_waterproof"):
        lines.append("- Currently wants waterproof items.")

    return "\n".join(lines)
