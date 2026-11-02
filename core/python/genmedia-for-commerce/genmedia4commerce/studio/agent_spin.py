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

"""The Studio agent's 360° Spin inputs.

A spin needs photos of one product from several sides. Catalogue products
have one photo each, so the catalogue's choices for a spin are its spin
sets (``catalog.spin_sets``: several views each), put on the page with all
their views as the app's spin picker does. One photo is still accepted (the
spin workflow takes 1 to 8), with ``ONE_PHOTO`` for the agent to pass on.
"""

from __future__ import annotations

import asyncio
from typing import Any

from genmedia4commerce.core import catalog
from genmedia4commerce.studio import media as sm
from genmedia4commerce.studio import patches as rules
from genmedia4commerce.studio import turn
from genmedia4commerce.studio.tool_order import setup_tool

__all__ = ["NOT_FOR_SPIN", "ONE_PHOTO", "choose_spin_set", "spin_set_names"]

#: What ``use_image`` adds when the spin has a single photo.
ONE_PHOTO = (
    "Only one photo of the product: the spin will invent its sides and "
    "back. Tell the user; several photos from different sides, or a spin "
    "set, work best."
)
#: What ``find_product`` answers on the 360° Spin page.
NOT_FOR_SPIN = (
    "Catalogue products have one photo each, too few for a 360° spin: use "
    "choose_spin_set, or ask the user to upload photos of the product from "
    "several sides. For a try-on product, open the try-on page first."
)


async def spin_set_names() -> list[str]:
    """The names of the catalogue's spin sets."""
    sets = await asyncio.to_thread(catalog.spin_sets)
    return [s["title"] for s in sets]


def _match(sets: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    """The first spin set whose name has every word of ``name``."""
    words = name.lower().split()
    return next(
        (
            s
            for s in sets
            if words and all(w in s["title"].lower() for w in words)
        ),
        None,
    )


@setup_tool
async def choose_spin_set(name: str) -> dict[str, Any]:
    """Put every photo of a catalogue spin set (one product photographed from
    several sides) on the 360° Spin page, replacing its photos, and show
    that page. The catalogue's only choice for a spin.

    Args:
        name: The spin set's name, or words from it, e.g. "hiking shoe".
    """
    sets = await asyncio.to_thread(catalog.spin_sets)
    row = _match(sets, name)
    if row is None:
        return {
            "error": f"No spin set matches {name!r}",
            "spin_sets": [s["title"] for s in sets],
        }
    views = min(row["images"], rules.MAX_ITEMS["spin"])
    copies = await asyncio.gather(
        *(
            asyncio.to_thread(sm.to_gcs, catalog.ref(row["id"], n))
            for n in range(views)
        )
    )
    items = [
        {"label": f"{row['title']} {i + 1}", "ref": uri}
        for i, (uri, _desc) in enumerate(copies)
    ]
    st = turn.current().st
    st["spin"] = items
    turn.patch("set", {"field": "spin", "items": items})
    if st.get("capability") != "spin":
        st["capability"] = "spin"
        turn.patch("capability", "spin")
    return {"spin": items, "description": copies[0][1] if copies else ""}
