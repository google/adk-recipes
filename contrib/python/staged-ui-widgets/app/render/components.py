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
"""Builders for A2UI v0.9 basic-catalog components, plus a flat assembler.

An A2UI surface is not a nested tree. It is a *flat list* of components that
reference each other by string id, and the spec requires that the component
with id ``root`` come first and that every parent appear before its children
so a streaming client can render incrementally.

Getting that ordering right by hand is fiddly, and a component that ends up
unreachable from ``root`` is rejected outright. ``Surface`` removes the
problem: add components in any order, and ``messages()`` emits them in
breadth-first order from ``root``.
"""

from __future__ import annotations

from collections import deque
from typing import Any

# The spec requires the entry-point component to use exactly this id.
ROOT_ID = "root"

# The wire ``version`` field every message carries. Deliberately not the same
# string as ``catalog.py``'s ``A2UI_VERSION``, and named apart from it so the
# difference is not read as a typo: the SDK selects schemas by ``"0.9"``, while
# its validator rejects any message whose ``version`` is not exactly ``"v0.9"``.
A2UI_MESSAGE_VERSION = "v0.9"

# Fields that hold a single component id.
_SINGLE_REF_FIELDS = ("child",)

# Fields that hold a list of component ids.
_LIST_REF_FIELDS = ("children",)


def text(
    cid: str,
    content: str,
    *,
    variant: str = "body",
    weight: float | None = None,
) -> dict[str, Any]:
    """A run of text. ``variant`` is one of h1-h5, caption, body."""
    component: dict[str, Any] = {
        "id": cid,
        "component": "Text",
        "text": content,
        "variant": variant,
    }
    if weight is not None:
        component["weight"] = weight
    return component


def card(cid: str, *, child: str) -> dict[str, Any]:
    """A card wrapping exactly one child. Wrap groups in a Column or Row."""
    return {"id": cid, "component": "Card", "child": child}


def column(
    cid: str,
    *,
    children: list[str],
    justify: str | None = None,
    align: str | None = None,
    weight: float | None = None,
) -> dict[str, Any]:
    """Vertical layout container."""
    return _container("Column", cid, children, justify, align, weight)


def row(
    cid: str,
    *,
    children: list[str],
    justify: str | None = None,
    align: str | None = None,
    weight: float | None = None,
) -> dict[str, Any]:
    """Horizontal layout container."""
    return _container("Row", cid, children, justify, align, weight)


def _container(
    kind: str,
    cid: str,
    children: list[str],
    justify: str | None,
    align: str | None,
    weight: float | None,
) -> dict[str, Any]:
    component: dict[str, Any] = {
        "id": cid,
        "component": kind,
        "children": list(children),
    }
    if justify is not None:
        component["justify"] = justify
    if align is not None:
        component["align"] = align
    if weight is not None:
        component["weight"] = weight
    return component


def item_list(
    cid: str,
    *,
    children: list[str],
    direction: str = "vertical",
    align: str | None = None,
) -> dict[str, Any]:
    """A list of items. ``direction="horizontal"`` gives a carousel."""
    component: dict[str, Any] = {
        "id": cid,
        "component": "List",
        "children": list(children),
        "direction": direction,
    }
    if align is not None:
        component["align"] = align
    return component


def image(
    cid: str,
    url: str,
    *,
    description: str | None = None,
    fit: str | None = None,
    variant: str = "mediumFeature",
) -> dict[str, Any]:
    """An image. ``url`` accepts a ``data:`` URI, which is how the spend
    chart ships a server-rendered SVG with no asset hosting."""
    component: dict[str, Any] = {
        "id": cid,
        "component": "Image",
        "url": url,
        "variant": variant,
    }
    if description is not None:
        component["description"] = description
    if fit is not None:
        component["fit"] = fit
    return component


def divider(cid: str, *, axis: str = "horizontal") -> dict[str, Any]:
    """A rule between sections."""
    return {"id": cid, "component": "Divider", "axis": axis}


def icon(cid: str, name: str) -> dict[str, Any]:
    """A named icon. ``name`` must be one of the catalog's icon names --
    a typo is caught by the schema validation in the test suite."""
    return {"id": cid, "component": "Icon", "name": name}


def references(component: dict[str, Any]) -> list[str]:
    """Returns the component ids this component points at, in order."""
    refs: list[str] = []
    for field in _SINGLE_REF_FIELDS:
        value = component.get(field)
        if isinstance(value, str):
            refs.append(value)
    for field in _LIST_REF_FIELDS:
        value = component.get(field)
        if isinstance(value, list):
            refs.extend(item for item in value if isinstance(item, str))
    return refs


class Surface:
    """Collects components and emits a spec-ordered A2UI message pair.

    Add components in whatever order is convenient for the converter. The
    ordering invariant the spec requires is restored on the way out.
    """

    def __init__(self, surface_id: str, catalog_id: str) -> None:
        self.surface_id = surface_id
        self.catalog_id = catalog_id
        self._components: dict[str, dict[str, Any]] = {}

    def add(self, component: dict[str, Any]) -> str:
        """Adds a component and returns its id, so calls can nest inline."""
        cid = component["id"]
        if cid in self._components:
            raise ValueError(f"duplicate component id: {cid}")
        self._components[cid] = component
        return cid

    def add_all(self, components: list[dict[str, Any]]) -> list[str]:
        """Adds several components and returns their ids in order."""
        return [self.add(component) for component in components]

    def __len__(self) -> int:
        return len(self._components)

    def ordered_components(self) -> list[dict[str, Any]]:
        """Components breadth-first from ``root``.

        Anything unreachable from ``root`` is dropped rather than emitted:
        the spec rejects orphans, so a half-built widget would fail
        validation on the client instead of here.
        """
        if ROOT_ID not in self._components:
            return []

        ordered: list[dict[str, Any]] = []
        seen: set[str] = {ROOT_ID}
        queue: deque[str] = deque([ROOT_ID])
        while queue:
            cid = queue.popleft()
            component = self._components[cid]
            ordered.append(component)
            for ref in references(component):
                if ref in self._components and ref not in seen:
                    seen.add(ref)
                    queue.append(ref)
        return ordered

    def messages(self) -> list[dict[str, Any]]:
        """The ``createSurface`` + ``updateComponents`` pair for this surface.

        Returns an empty list when there is nothing renderable, which the
        registry treats as "this widget produced no output".
        """
        components = self.ordered_components()
        if not components:
            return []
        return [
            {
                "version": A2UI_MESSAGE_VERSION,
                "createSurface": {
                    "surfaceId": self.surface_id,
                    "catalogId": self.catalog_id,
                },
            },
            {
                "version": A2UI_MESSAGE_VERSION,
                "updateComponents": {
                    "surfaceId": self.surface_id,
                    "components": components,
                },
            },
        ]
