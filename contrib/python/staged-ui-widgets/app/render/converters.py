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
"""One converter per semantic type: payload in, A2UI components out.

Each converter is a pure function returning a flat list of components in any
order. The registry feeds that list through ``Surface``, which imposes the
ordering the spec requires and drops anything unreachable from ``root``.

Converters render; they never decide. "Best value" is a boolean the tool
computed, and reason chips are strings the tool computed. Keeping judgement
out of the render layer is what makes the widget trustworthy -- and what
makes these functions trivial to test.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .chart_svg import spend_trend_svg, svg_data_uri
from .components import (
    ROOT_ID,
    card,
    column,
    divider,
    icon,
    image,
    item_list,
    row,
    text,
)

# Cap on rows rendered by the generic fallback, so an unexpected payload
# degrades into something readable instead of a wall of text.
_GENERIC_MAX_ROWS = 8

# Icon per timeline step state. Unknown states fall back to a neutral mark
# rather than raising -- a new state added upstream should not break the UI.
_STEP_ICONS = {
    "done": "check",
    "current": "refresh",
    "upcoming": "moreHoriz",
    "cancelled": "close",
    "problem": "warning",
}
_STEP_ICON_FALLBACK = "moreHoriz"


def money(amount: Any) -> str:
    """Formats a price. Whole amounts lose the cents, as shoppers expect."""
    if not isinstance(amount, (int, float)):
        return ""
    if float(amount).is_integer():
        return f"${amount:,.0f}"
    return f"${amount:,.2f}"


def product_picks_to_a2ui(
    payload: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """A horizontal carousel of recommendation cards.

    Each card carries the reason chips the ranking produced, so the shopper
    sees *why* an item is there without the agent having to claim anything.
    """
    items = [i for i in payload.get("items", []) if isinstance(i, Mapping)]
    if not items:
        return []

    components: list[dict[str, Any]] = []
    card_ids: list[str] = []

    for index, item in enumerate(items):
        prefix = f"pick-{index}"
        cell_ids: list[str] = []

        image_url = item.get("image_url")
        if isinstance(image_url, str) and image_url:
            components.append(
                image(
                    f"{prefix}-img",
                    image_url,
                    description=str(item.get("name", "")),
                    variant="smallFeature",
                    fit="cover",
                )
            )
            cell_ids.append(f"{prefix}-img")

        brand = item.get("brand")
        if brand:
            components.append(
                text(f"{prefix}-brand", str(brand), variant="caption")
            )
            cell_ids.append(f"{prefix}-brand")

        components.append(
            text(f"{prefix}-name", str(item.get("name", "")), variant="h5")
        )
        cell_ids.append(f"{prefix}-name")

        price = money(item.get("price"))
        if price:
            components.append(text(f"{prefix}-price", price, variant="body"))
            cell_ids.append(f"{prefix}-price")

        reasons = [str(r) for r in item.get("reasons", []) if r]
        if reasons:
            chip_ids = []
            for chip_index, reason in enumerate(reasons):
                chip_id = f"{prefix}-chip-{chip_index}"
                components.append(text(chip_id, reason, variant="caption"))
                chip_ids.append(chip_id)
            components.append(
                row(f"{prefix}-chips", children=chip_ids, justify="start")
            )
            cell_ids.append(f"{prefix}-chips")

        components.append(column(f"{prefix}-body", children=cell_ids))
        components.append(card(prefix, child=f"{prefix}-body"))
        card_ids.append(prefix)

    components.append(
        item_list("picks-carousel", children=card_ids, direction="horizontal")
    )

    root_children = ["picks-carousel"]
    headline = _headline(payload, default="Picked for you")
    if headline:
        components.append(text("picks-headline", headline, variant="h3"))
        root_children.insert(0, "picks-headline")

    components.append(column(ROOT_ID, children=root_children))
    return components


def comparison_to_a2ui(
    payload: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """A side-by-side comparison table.

    The payload is row-oriented (a list of items) but the table is
    column-oriented -- one column per item, plus a leading column of
    attribute labels. That transposition is the converter's real work, and
    it is exactly the kind of reshaping a model gets subtly wrong.
    """
    items = [i for i in payload.get("items", []) if isinstance(i, Mapping)]
    attributes = [str(a) for a in payload.get("attributes", []) if a]
    if not items or not attributes:
        return []

    # The tool states which columns hold currency amounts; this converter
    # decides what an amount looks like. Without that split, either the tool
    # ships display strings or the table prints a price as a bare "148".
    money_attributes = {
        str(a) for a in payload.get("money_attributes", []) if a
    }

    components: list[dict[str, Any]] = []

    # Leading column: a spacer aligning with the item names, then labels.
    label_ids = ["cmp-label-spacer"]
    components.append(text("cmp-label-spacer", " ", variant="caption"))
    for attr_index, attribute in enumerate(attributes):
        label_id = f"cmp-label-{attr_index}"
        components.append(text(label_id, attribute, variant="caption"))
        label_ids.append(label_id)
    components.append(column("cmp-labels", children=label_ids, weight=1))

    column_ids = ["cmp-labels"]
    for item_index, item in enumerate(items):
        prefix = f"cmp-item-{item_index}"
        cell_ids: list[str] = []

        name_ids = [f"{prefix}-name"]
        components.append(
            text(f"{prefix}-name", str(item.get("name", "")), variant="h5")
        )
        if item.get("best_value"):
            components.append(
                text(f"{prefix}-flag", "Best value", variant="caption")
            )
            name_ids.append(f"{prefix}-flag")
        components.append(column(f"{prefix}-head", children=name_ids))
        cell_ids.append(f"{prefix}-head")

        values = item.get("values")
        values = values if isinstance(values, Mapping) else {}
        for attr_index, attribute in enumerate(attributes):
            cell_id = f"{prefix}-value-{attr_index}"
            raw = values.get(attribute)
            components.append(
                text(
                    cell_id,
                    _cell_text(raw, as_money=attribute in money_attributes),
                    variant="body",
                )
            )
            cell_ids.append(cell_id)

        components.append(column(prefix, children=cell_ids, weight=2))
        column_ids.append(prefix)

    components.append(row("cmp-table", children=column_ids, align="start"))

    root_children = ["cmp-table"]
    headline = _headline(payload, default="Comparing your picks")
    if headline:
        components.append(text("cmp-headline", headline, variant="h3"))
        root_children.insert(0, "cmp-headline")

    components.append(column(ROOT_ID, children=root_children))
    return components


def order_timeline_to_a2ui(
    payload: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """A fulfillment timeline.

    Step order carries meaning, so it comes straight from the payload. A
    model asked to narrate a timeline can reorder or invent stages; this
    cannot.
    """
    steps = [s for s in payload.get("steps", []) if isinstance(s, Mapping)]
    if not steps:
        return []

    components: list[dict[str, Any]] = []
    root_children: list[str] = []

    headline = _headline(payload, default="Order status")
    if headline:
        components.append(text("order-headline", headline, variant="h3"))
        root_children.append("order-headline")

    for index, step in enumerate(steps):
        prefix = f"step-{index}"
        state = str(step.get("state", "")).lower()

        components.append(
            icon(
                f"{prefix}-icon",
                _STEP_ICONS.get(state, _STEP_ICON_FALLBACK),
            )
        )

        body_ids = [f"{prefix}-label"]
        components.append(
            text(f"{prefix}-label", str(step.get("label", "")), variant="h5")
        )
        detail = step.get("detail")
        if detail:
            components.append(
                text(f"{prefix}-detail", str(detail), variant="caption")
            )
            body_ids.append(f"{prefix}-detail")
        components.append(column(f"{prefix}-body", children=body_ids))

        components.append(
            row(
                prefix,
                children=[f"{prefix}-icon", f"{prefix}-body"],
                align="center",
            )
        )
        root_children.append(prefix)

        if index < len(steps) - 1:
            components.append(divider(f"{prefix}-rule"))
            root_children.append(f"{prefix}-rule")

    components.append(column(ROOT_ID, children=root_children))
    return components


def spend_trend_to_a2ui(
    payload: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """A spend chart, rendered to SVG here and inlined as a ``data:`` URI."""
    points = [p for p in payload.get("points", []) if isinstance(p, Mapping)]
    uri = svg_data_uri(spend_trend_svg(points))
    if not uri:
        return []

    components: list[dict[str, Any]] = []
    root_children: list[str] = []

    headline = _headline(payload, default="Your spending")
    if headline:
        components.append(text("spend-headline", headline, variant="h3"))
        root_children.append("spend-headline")

    components.append(
        image(
            "spend-chart",
            uri,
            description=_chart_alt_text(points),
            variant="largeFeature",
            fit="contain",
        )
    )
    root_children.append("spend-chart")

    note = payload.get("note")
    if note:
        components.append(text("spend-note", str(note), variant="caption"))
        root_children.append("spend-note")

    components.append(column(ROOT_ID, children=root_children))
    return components


def generic_converter(semantic_type: str):
    """Builds a fallback converter for an unregistered semantic type.

    An unknown type degrades into a plain labelled card rather than
    vanishing. A widget that silently disappears is the worst outcome here:
    the agent believes it showed something the shopper never saw.
    """

    def convert(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        return _generic_components(semantic_type, payload)

    convert.__name__ = f"generic_converter[{semantic_type}]"
    return convert


def _generic_components(
    semantic_type: str,
    payload: Mapping[str, Any],
) -> list[dict[str, Any]]:
    components: list[dict[str, Any]] = [
        text("generic-title", _humanise(semantic_type), variant="h4")
    ]
    body_ids = ["generic-title"]

    rows = 0
    for key, value in payload.items():
        if rows >= _GENERIC_MAX_ROWS:
            break
        if key in ("headline", "title"):
            continue
        line_id = f"generic-row-{rows}"
        components.append(
            text(line_id, f"{_humanise(str(key))}: {_summarise(value)}")
        )
        body_ids.append(line_id)
        rows += 1

    components.append(column("generic-body", children=body_ids))
    components.append(card(ROOT_ID, child="generic-body"))
    return components


def _headline(payload: Mapping[str, Any], *, default: str) -> str:
    """Payload headline if present, else a sensible default."""
    headline = payload.get("headline")
    if isinstance(headline, str) and headline.strip():
        return headline.strip()
    return default


def _cell_text(value: Any, *, as_money: bool = False) -> str:
    """Renders one comparison cell. Missing data shows a dash, not blank."""
    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (int, float)):
        if as_money:
            return money(value)
        # ``g`` earns its place by dropping the trailing zero on a whole float
        # (278.0 -> "278"), but its exponent form makes a large spec number
        # unreadable in a product comparison: at ``g``'s default of six
        # significant digits 1000000 rendered as ``1e+06``, and raising the
        # precision only moves that cliff further out. So whole values format
        # exactly at any magnitude, and ``g`` is left the one job it is good
        # at. Integers go through ``,`` rather than ``,.0f`` because ``f``
        # routes through a float and overflows on a big enough int.
        if isinstance(value, int):
            return f"{value:,}"
        if value.is_integer():
            return f"{value:,.0f}"
        return f"{value:,.10g}"
    return str(value)


def _chart_alt_text(points: list[Mapping[str, Any]]) -> str:
    """Accessible description of the chart, built from the same series."""
    if not points:
        return "Spending chart"
    first = str(points[0].get("month", ""))
    last = str(points[-1].get("month", ""))
    span = f"{first} to {last}" if first and last else "the period shown"
    return f"Monthly spending from {span}"


def _summarise(value: Any) -> str:
    """Compact one-line rendering of an arbitrary value."""
    if isinstance(value, Mapping):
        return f"{len(value)} fields"
    if isinstance(value, (list, tuple, set)):
        return f"{len(value)} items"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value)


def _humanise(key: str) -> str:
    """``product_picks`` -> ``Product picks``."""
    cleaned = key.replace("_", " ").replace("-", " ").strip()
    return cleaned[:1].upper() + cleaned[1:] if cleaned else key
