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
"""The converter registry: semantic type -> converter.

This is the dispatch table that keeps the model out of the render path. A
tool names a semantic type; the registry decides what that type looks like.
Two properties matter more than the table itself:

*Never drop.* An unregistered type falls back to ``generic_converter``, so a
widget degrades into a plain card instead of vanishing. Silent loss is the
worst failure mode -- the agent believes it showed something the user never
saw.

That fallback is for a type this table has never heard of. It is *not* cover
for a misspelled ``semantic_type`` in ``WIDGET_SPECS``, which would quietly
downgrade a real chart to a generic card; ``staging.lifecycle`` checks every
declared widget against this table at import and refuses to load if one is
missing.

*Never crash the turn.* A converter that raises is logged and yields no
widget. A broken card is a cosmetic bug; an exception escaping an
``after_agent_callback`` costs the user their whole response.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from .components import Surface
from .converters import (
    comparison_to_a2ui,
    generic_converter,
    order_timeline_to_a2ui,
    product_picks_to_a2ui,
    spend_trend_to_a2ui,
)

logger = logging.getLogger(__name__)

# A converter takes a semantic payload and returns A2UI components in any
# order. Ordering and reachability are the Surface's job, not the
# converter's.
Converter = Callable[[Mapping[str, Any]], list[dict[str, Any]]]

CONVERTERS: dict[str, Converter] = {
    "product_picks": product_picks_to_a2ui,
    "product_comparison": comparison_to_a2ui,
    "order_timeline": order_timeline_to_a2ui,
    "spend_trend": spend_trend_to_a2ui,
}


def get_converters(
    overrides: Mapping[str, Converter] | None = None,
) -> dict[str, Converter]:
    """The shared table with per-surface overrides layered on top.

    Real deployments render the same semantic type differently per channel --
    a phone, a kiosk, an agent-to-agent peer. Overriding one entry beats
    forking the table, because a type absent from the overrides keeps
    working.
    """
    return {**CONVERTERS, **(overrides or {})}


def resolve_converter(
    semantic_type: str,
    overrides: Mapping[str, Converter] | None = None,
) -> Converter:
    """The converter for a type, or the generic fallback."""
    converters = get_converters(overrides)
    converter = converters.get(semantic_type)
    if converter is not None:
        return converter
    logger.info(
        "no converter for semantic type %r; using generic fallback",
        semantic_type,
    )
    return generic_converter(semantic_type)


def build_a2ui_messages(
    semantic_type: str,
    payload: Mapping[str, Any],
    *,
    surface_id: str,
    catalog_id: str,
    overrides: Mapping[str, Converter] | None = None,
) -> list[dict[str, Any]]:
    """Converts a payload to A2UI messages, or ``[]`` if it renders empty.

    Fails open: a converter that raises is logged and produces nothing.
    """
    converter = resolve_converter(semantic_type, overrides)
    try:
        components = converter(payload)
    except Exception:
        logger.exception(
            "converter for %r raised; emitting no widget", semantic_type
        )
        return []

    if not components:
        return []

    surface = Surface(surface_id, catalog_id)
    try:
        surface.add_all(components)
    except ValueError:
        # Duplicate component id -- a bug in the converter, not the data.
        logger.exception(
            "converter for %r produced duplicate component ids", semantic_type
        )
        return []
    return surface.messages()


def build_widget(
    semantic_type: str,
    payload: Mapping[str, Any],
    *,
    surface_id: str,
    catalog_id: str,
    overrides: Mapping[str, Converter] | None = None,
) -> dict[str, Any] | None:
    """The ready-to-send ``UiWidget`` payload, or ``None`` if nothing built.

    ``None`` is a normal outcome, not an error: an empty register, an empty
    series, or a converter that declined all produce it. The caller treats
    it as "no widget this turn".
    """
    messages = build_a2ui_messages(
        semantic_type,
        payload,
        surface_id=surface_id,
        catalog_id=catalog_id,
        overrides=overrides,
    )
    if not messages:
        return None
    return {
        "type": semantic_type,
        "surfaceId": surface_id,
        "a2ui": messages,
    }
