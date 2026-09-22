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
"""The render layer, checked against the real A2UI schema.

``A2uiValidator`` comes from the a2ui SDK and enforces what a host actually
enforces: schema conformance, unique ids, a ``root`` component, no dangling
references, no cycles, no orphans. Asserting our own idea of valid A2UI would
prove nothing, so every converter goes through the published validator here.

The rest of the file covers the two properties the registry promises -- never
drop a widget, never crash a turn -- because both are only observable in
tests. In production they look like a widget that quietly did not appear.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

import pytest

from app.catalog import catalog_id, validator
from app.render.chart_svg import spend_trend_svg, svg_data_uri
from app.render.components import (
    ROOT_ID,
    Surface,
    column,
    references,
    text,
)
from app.render.converters import _cell_text, money
from app.render.placeholder_svg import product_tile_uri, tile_color
from app.render.registry import (
    CONVERTERS,
    build_widget,
    resolve_converter,
)

PICKS_PAYLOAD: dict[str, Any] = {
    "headline": "Because you liked the Aera runners",
    "items": [
        {
            "name": "Cirrus Trail 3",
            "brand": "Aera",
            "price": 148.0,
            "image_url": "data:image/svg+xml;base64,PHN2Zy8+",
            "reasons": ["Matches your size 9.5", "Trail, like your last two"],
        },
        {
            "name": "Loft Merino Crew",
            "brand": "Northbank",
            "price": 89.5,
            "reasons": ["Under your $200 ceiling"],
        },
        # No brand, no image, no reasons: every optional branch skipped.
        {"name": "Traverse Daypack 18L", "price": 120},
    ],
}

COMPARISON_PAYLOAD: dict[str, Any] = {
    "attributes": ["Price", "Weight", "Waterproof"],
    "money_attributes": ["Price"],
    "items": [
        {
            "name": "Cirrus Trail 3",
            "best_value": True,
            "values": {"Price": 148.0, "Weight": "9.8 oz", "Waterproof": True},
        },
        {
            "name": "Ridgeline GT",
            "values": {
                "Price": 189.0,
                "Weight": "11.2 oz",
                "Waterproof": False,
            },
        },
        # Weight missing entirely -- the cell must show a dash, not vanish.
        {"name": "Fell Runner Lite", "values": {"Price": 132.5}},
    ],
}

TIMELINE_PAYLOAD: dict[str, Any] = {
    "headline": "Order ORD-4417",
    "steps": [
        {"state": "done", "label": "Ordered", "detail": "Aug 11"},
        {"state": "done", "label": "Packed", "detail": "Aug 12"},
        {
            "state": "current",
            "label": "In transit",
            "detail": "Arriving Aug 19",
        },
        {"state": "upcoming", "label": "Delivered"},
    ],
}

SPEND_PAYLOAD: dict[str, Any] = {
    "headline": "Spending, last six months",
    "points": [
        {"month": "Mar", "amount": 212.0},
        {"month": "Apr", "amount": 98.5},
        {"month": "May", "amount": 341.0},
        {"month": "Jun", "amount": 156.0},
        {"month": "Jul", "amount": 275.25},
        {"month": "Aug", "amount": 189.0},
    ],
    "note": "Averaging $212 a month",
}

ALL_PAYLOADS: dict[str, dict[str, Any]] = {
    "product_picks": PICKS_PAYLOAD,
    "product_comparison": COMPARISON_PAYLOAD,
    "order_timeline": TIMELINE_PAYLOAD,
    "spend_trend": SPEND_PAYLOAD,
}

# Payloads that are structurally fine but describe nothing to show. Each must
# render to None -- "no widget" rather than an empty frame.
NOTHING_TO_SHOW: dict[str, dict[str, Any]] = {
    "product_picks": {"headline": "Nothing", "items": []},
    "product_comparison": {"items": [{"name": "x"}], "attributes": []},
    "order_timeline": {"headline": "Nothing", "steps": []},
    "spend_trend": {"headline": "Nothing", "points": []},
}


def render(
    semantic_type: str,
    payload: Mapping[str, Any],
    **kwargs: Any,
) -> dict[str, Any] | None:
    return build_widget(
        semantic_type,
        payload,
        surface_id=f"sfc-{semantic_type}",
        catalog_id=catalog_id(),
        **kwargs,
    )


def components_of(widget: Mapping[str, Any]) -> list[dict[str, Any]]:
    return widget["a2ui"][1]["updateComponents"]["components"]


def texts_of(widget: Mapping[str, Any]) -> dict[str, str]:
    """Component id -> rendered text, for asserting on content."""
    return {
        c["id"]: c["text"]
        for c in components_of(widget)
        if c["component"] == "Text"
    }


# --- the schema contract ----------------------------------------------------


@pytest.mark.parametrize("semantic_type", sorted(ALL_PAYLOADS))
def test_converter_output_is_valid_a2ui(semantic_type: str) -> None:
    """Every converter produces a surface the published validator accepts."""
    widget = render(semantic_type, ALL_PAYLOADS[semantic_type])
    assert widget is not None
    validator().validate(widget["a2ui"])


def test_the_schema_check_has_teeth() -> None:
    """Guards the guard.

    Every test above leans on ``validator()`` rejecting bad A2UI. If a future
    SDK bump made validation permissive, those tests would keep passing while
    checking nothing. So: assert it still rejects an unknown component and an
    icon name outside the catalog.
    """
    surface = Surface("sfc", catalog_id())
    surface.add({"id": "a", "component": "Blender", "text": "nope"})
    surface.add(column(ROOT_ID, children=["a"]))
    with pytest.raises(ValueError, match="Unknown component"):
        validator().validate(surface.messages())

    bogus_icon = Surface("sfc", catalog_id())
    bogus_icon.add({"id": "a", "component": "Icon", "name": "notARealIcon"})
    bogus_icon.add(column(ROOT_ID, children=["a"]))
    # Matched on the bad name, not on bare ``ValueError``: this payload draws
    # two complaints from the validator, and only one of them is the icon
    # catalogue doing its job. Without the match, a bump that stopped checking
    # icon names would still raise on the other and the test would pass.
    with pytest.raises(ValueError, match="notARealIcon"):
        validator().validate(bogus_icon.messages())


def test_generic_fallback_output_is_valid_a2ui() -> None:
    """An unregistered type degrades to a valid card, not to nothing."""
    widget = render(
        "loyalty_status",
        {
            "headline": "ignored by the fallback",
            "tier": "Gold",
            "points": 4180,
            "perks": ["Free returns", "Early access"],
            "renews": True,
        },
    )
    assert widget is not None
    validator().validate(widget["a2ui"])
    rendered = texts_of(widget)
    assert rendered["generic-title"] == "Loyalty status"
    # Keys are humanised and values summarised; the headline is dropped
    # because the card is already titled.
    assert "Tier: Gold" in rendered.values()
    assert "Perks: 2 items" in rendered.values()
    assert "Renews: Yes" in rendered.values()


def test_messages_are_create_then_update() -> None:
    """Two messages, in the order a host applies them."""
    widget = render("product_picks", PICKS_PAYLOAD)
    assert widget is not None
    create, update = widget["a2ui"]
    assert create["createSurface"]["surfaceId"] == "sfc-product_picks"
    assert create["createSurface"]["catalogId"] == catalog_id()
    assert update["updateComponents"]["surfaceId"] == "sfc-product_picks"
    assert widget["type"] == "product_picks"
    assert widget["surfaceId"] == "sfc-product_picks"


def test_root_comes_first_and_parents_precede_children() -> None:
    """The ordering rule A2UI hosts depend on, asserted directly."""
    widget = render("order_timeline", TIMELINE_PAYLOAD)
    assert widget is not None
    components = components_of(widget)
    assert components[0]["id"] == ROOT_ID

    position = {c["id"]: i for i, c in enumerate(components)}
    for parent in components:
        for child_id in references(parent):
            assert position[parent["id"]] < position[child_id], (
                f"{parent['id']} must precede its child {child_id}"
            )


# --- what the converters actually decide ------------------------------------


def test_comparison_transposes_rows_into_columns() -> None:
    """Three items and three attributes become four columns, labels first.

    The payload is row-oriented and the table is column-oriented. This
    transposition is the converter's real work -- and exactly what a model
    asked to lay out a table gets subtly wrong.
    """
    widget = render("product_comparison", COMPARISON_PAYLOAD)
    assert widget is not None
    table = next(c for c in components_of(widget) if c["id"] == "cmp-table")
    assert table["children"] == [
        "cmp-labels",
        "cmp-item-0",
        "cmp-item-1",
        "cmp-item-2",
    ]


def test_comparison_cell_formatting() -> None:
    """Money keeps its currency, booleans read as words, gaps show a dash."""
    widget = render("product_comparison", COMPARISON_PAYLOAD)
    assert widget is not None
    rendered = texts_of(widget)

    assert rendered["cmp-item-0-value-0"] == "$148"  # whole amount, no cents
    assert rendered["cmp-item-2-value-0"] == "$132.50"  # cents kept
    assert rendered["cmp-item-0-value-2"] == "Yes"
    assert rendered["cmp-item-1-value-2"] == "No"
    assert rendered["cmp-item-2-value-1"] == "—"  # missing Weight
    assert rendered["cmp-item-0-flag"] == "Best value"
    assert "cmp-item-1-flag" not in rendered


def test_a_large_whole_cell_is_never_scientific_notation() -> None:
    """A big whole spec number must stay readable at any magnitude.

    ``g`` defaults to six significant digits, which silently turns 1000000
    into ``1e+06`` -- correct, and unreadable in a product comparison. The
    trap in the obvious fix is that raising the precision only *moves* the
    cliff, so this pins an int past any precision worth naming as well as the
    seven-figure one. Whole floats must still lose their trailing zero, which
    is the only reason ``g`` is in this function.

    Scoped to whole values, as the converter's own comment is: a *fractional*
    float above ``,.10g``'s cliff still renders as an exponent. No comparison
    cell this recipe can produce is one -- every numeric attribute in
    ``products.json`` is a whole price -- so closing that too would be
    speculative formatting nobody can reach. Naming the test for what it
    actually pins is the point of the round that added it.
    """
    widget = render(
        "product_comparison",
        {
            "attributes": ["Abrasion cycles", "Fibre metres", "Weight grams"],
            "money_attributes": [],
            "items": [
                {
                    "name": "Cirrus Trail 3",
                    "values": {
                        "Abrasion cycles": 1000000,
                        "Fibre metres": 12345678901,
                        "Weight grams": 278.0,
                    },
                }
            ],
        },
    )
    assert widget is not None
    rendered = texts_of(widget)

    assert rendered["cmp-item-0-value-0"] == "1,000,000"
    assert rendered["cmp-item-0-value-1"] == "12,345,678,901"
    assert rendered["cmp-item-0-value-2"] == "278"


def test_cell_precision_survives_a_whole_float_and_a_fractional_one() -> None:
    """The two float shapes ``g`` was kept for, at both ends of the scale.

    A whole float loses its zero however large it is; a fractional one keeps
    its digits. Asserted directly on ``_cell_text`` because a comparison
    payload cannot show the boundary as clearly.
    """
    assert _cell_text(278.0) == "278"
    assert _cell_text(1e11) == "100,000,000,000"
    assert _cell_text(132.55) == "132.55"
    assert _cell_text(9.8) == "9.8"
    # Not a number, not a crash: the cell falls through to ``str``.
    assert _cell_text("11.2 oz") == "11.2 oz"
    assert _cell_text(None) == "—"


def test_timeline_icons_follow_step_state() -> None:
    """State picks the icon; an unknown state degrades instead of raising."""
    widget = render(
        "order_timeline",
        {
            "steps": [
                {"state": "done", "label": "Ordered"},
                {"state": "current", "label": "In transit"},
                {"state": "problem", "label": "Address needs attention"},
                {"state": "cancelled", "label": "Cancelled"},
                {"state": "invented-upstream", "label": "Unknown"},
            ]
        },
    )
    assert widget is not None
    icons = [
        c["name"] for c in components_of(widget) if c["component"] == "Icon"
    ]
    assert icons == ["check", "refresh", "warning", "close", "moreHoriz"]
    # The icon names are catalog-constrained, so validating proves the
    # fallback picked a real one rather than a plausible-looking string.
    validator().validate(widget["a2ui"])


def test_picks_omit_absent_fields_rather_than_rendering_blanks() -> None:
    """The third item has no brand, image, or chips, so those ids are absent."""
    widget = render("product_picks", PICKS_PAYLOAD)
    assert widget is not None
    ids = {c["id"] for c in components_of(widget)}

    assert {"pick-0-img", "pick-0-brand", "pick-0-chips"} <= ids
    assert {"pick-2-img", "pick-2-brand", "pick-2-chips"}.isdisjoint(ids)
    assert texts_of(widget)["pick-1-price"] == "$89.50"


def test_money_formatting() -> None:
    assert money(148.0) == "$148"
    assert money(89.5) == "$89.50"
    assert money(1234.5) == "$1,234.50"
    assert money(None) == ""
    assert money("free") == ""


# --- never drop, never crash ------------------------------------------------


@pytest.mark.parametrize("semantic_type", sorted(NOTHING_TO_SHOW))
def test_empty_series_renders_no_widget(semantic_type: str) -> None:
    """An empty payload is "no widget", not an empty frame."""
    assert render(semantic_type, NOTHING_TO_SHOW[semantic_type]) is None


def test_raising_converter_is_swallowed() -> None:
    """A converter bug must not cost the shopper their whole reply."""

    def boom(_payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        raise RuntimeError("intentional")

    assert (
        render(
            "product_picks",
            PICKS_PAYLOAD,
            overrides={"product_picks": boom},
        )
        is None
    )


def test_duplicate_component_ids_are_caught() -> None:
    """A converter emitting the same id twice yields nothing, not bad A2UI."""

    def duplicating(_payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        return [
            text("dup", "first"),
            text("dup", "second"),
            column(ROOT_ID, children=["dup"]),
        ]

    # A real payload, not ``{}``: the genuine product_picks converter returns
    # nothing for an empty payload, so with ``{}`` this assertion would also
    # hold on a build that ignored the override entirely and never ran
    # ``duplicating`` at all.
    assert (
        render(
            "product_picks",
            PICKS_PAYLOAD,
            overrides={"product_picks": duplicating},
        )
        is None
    )


def test_orphans_are_dropped_and_the_surface_stays_valid() -> None:
    """A component unreachable from root is dropped, not shipped.

    A2UI rejects orphans outright, so dropping one turns a converter slip into
    a missing element rather than a surface the host refuses whole.
    """

    def with_orphan(_payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        return [
            text("kept", "visible"),
            text("unreachable", "never referenced"),
            column(ROOT_ID, children=["kept"]),
        ]

    widget = render(
        "product_picks", {}, overrides={"product_picks": with_orphan}
    )
    assert widget is not None
    assert [c["id"] for c in components_of(widget)] == [ROOT_ID, "kept"]
    validator().validate(widget["a2ui"])


def test_unregistered_type_resolves_to_the_generic_converter() -> None:
    assert resolve_converter("product_picks") is CONVERTERS["product_picks"]
    assert (
        resolve_converter("never_registered").__name__
        == "generic_converter[never_registered]"
    )


def test_overrides_layer_over_the_shared_table() -> None:
    """One overridden type; the rest of the table keeps working."""

    def minimal(_payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        return [text("only", "override"), column(ROOT_ID, children=["only"])]

    widget = render(
        "product_picks", PICKS_PAYLOAD, overrides={"product_picks": minimal}
    )
    assert widget is not None
    assert texts_of(widget) == {"only": "override"}

    untouched = render(
        "spend_trend", SPEND_PAYLOAD, overrides={"product_picks": minimal}
    )
    assert untouched is not None
    # ``is not None`` alone cannot see the regression this test exists for.
    # An override table that *replaced* the shared one instead of layering over
    # it would send spend_trend through the generic fallback, which also
    # returns a widget -- a real chart silently degrading to a plain card. So
    # assert it is still the chart: none of the generic card's ids, and
    # identical to the render with no overrides in play at all.
    assert "generic-title" not in [c["id"] for c in components_of(untouched)]
    assert untouched == render("spend_trend", SPEND_PAYLOAD)


def test_surface_rejects_a_duplicate_id_at_add_time() -> None:
    surface = Surface("sfc", catalog_id())
    surface.add(text("a", "one"))
    with pytest.raises(ValueError):
        surface.add(text("a", "two"))


def test_surface_with_no_components_yields_no_messages() -> None:
    assert Surface("sfc", catalog_id()).messages() == []


# --- generated graphics -----------------------------------------------------


def test_chart_svg_is_byte_stable() -> None:
    """Same series, same bytes -- pinned to a literal, not to a second call.

    Coordinates are rounded to two decimals precisely so a chart does not
    churn between runs, which would make every diff of a recorded widget
    unreadable. Two calls in one process agree whatever the rounding does;
    the polyline below is what a scale or precision change breaks.
    """
    points = deepcopy(SPEND_PAYLOAD["points"])
    svg = spend_trend_svg(points)
    assert svg.startswith("<svg")
    assert (
        '<polyline points="10,82.64 102,120.84 194,39.22 286,101.49'
        ' 378,61.35 470,90.38"'
    ) in svg
    assert svg_data_uri(svg).startswith("data:image/svg+xml;base64,")
    # Rendering must not consume the series it was handed.
    assert points == SPEND_PAYLOAD["points"]


def test_chart_svg_of_an_empty_series_is_empty() -> None:
    assert spend_trend_svg([]) == ""
    assert svg_data_uri("") == ""


def test_placeholder_tiles_are_stable_and_distinct() -> None:
    """Tile colour is content-addressed, so it survives a restart.

    ``hash()`` would have been the obvious choice and is salted per process:
    the same product would change colour on every boot. Pinning the two
    colours to literals is what catches that -- two calls inside one process
    agree either way.
    """
    assert tile_color("SKU-1") == "#6B7F52"
    assert tile_color("SKU-2") == "#4E5A6B"
    assert product_tile_uri("SKU-1", "Cirrus Trail 3").startswith(
        "data:image/svg+xml;base64,"
    )
