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

"""Catalogue items as the Studio clients show them, and older refs.

An item for a client is ``{label, ref, thumb}`` (a spin set has ``refs``
and ``thumbs``, one per view). ``thumb`` is a same-origin URL of ``GET
/api/catalog/image/<id>/<n>`` for the web app, or a data URL for the MCP
App panel (``inline``), whose iframe loads nothing else from this server.

Studios saved before the local catalogue may hold ``gallery:`` refs or
images of the old public asset bucket; ``legacy`` maps those it can to
``catalog:`` refs. ``upgrade_state`` upgrades a saved studio on read.
"""

from __future__ import annotations

import base64
import logging
from typing import Any
from urllib.parse import quote

from genmedia4commerce.core import catalog
from genmedia4commerce.core.errors import GenMediaError
from genmedia4commerce.studio import patches as rules

__all__ = ["app_item", "gallery", "legacy", "thumb", "upgrade_state"]

logger = logging.getLogger(__name__)

_GALLERY = "gallery:"
#: Gallery folders and the id prefix their items got in the catalogue.
_GALLERY_KINDS = {"models": "m_", "products": "p_"}


def thumb(ref: str, *, inline: bool) -> str:
    """The thumbnail of a ``catalog:`` ref for a client (see module doc)."""
    item_id, n = catalog.parse_ref(ref)
    if inline:
        data = catalog.thumbnail(item_id, n)
        return "data:image/webp;base64," + base64.b64encode(data).decode()
    return f"/api/catalog/image/{quote(item_id)}/{n}?w={catalog.THUMB_SIDE}"


def app_item(row: dict[str, Any], *, inline: bool) -> dict[str, str]:
    """``{label, ref, thumb}`` of a catalogue row (its first image)."""
    ref = catalog.ref(row["id"])
    return {
        "label": row["title"],
        "ref": ref,
        "thumb": thumb(ref, inline=inline),
    }


def _spin_set(row: dict[str, Any], *, inline: bool) -> dict[str, Any]:
    refs = [catalog.ref(row["id"], n) for n in range(row["images"])]
    thumbs = [thumb(r, inline=inline) for r in refs]
    return {
        "label": row["title"],
        "refs": refs,
        "thumb": thumbs[0],
        "thumbs": thumbs,
    }


def gallery(*, inline: bool) -> dict[str, list[dict[str, Any]]]:
    """The preset ``models`` and the ``spin`` sets (``refs`` and
    ``thumbs``: every view, in order)."""
    return {
        "models": [app_item(r, inline=inline) for r in catalog.models()],
        "spin": [_spin_set(r, inline=inline) for r in catalog.spin_sets()],
    }


def _from_gallery(ref: str) -> str | None:
    kind, _, rest = ref.removeprefix(_GALLERY).partition("/")
    item_id = rest.split("/", 1)[0]
    prefix = _GALLERY_KINDS.get(kind)
    if not (prefix and item_id):
        return None
    if prefix + item_id not in catalog.get_catalog().by_id:
        return None
    return catalog.ref(prefix + item_id)


def legacy(ref: str) -> str:
    """``ref``, or the ``catalog:`` ref of the same image when ``ref`` is a
    ``gallery:`` preset (models and try-on products) or an image of the old
    asset bucket the catalogue knows (its ``source_image``). Never raises:
    a ref it cannot map comes back unchanged."""
    if not isinstance(ref, str) or not ref.startswith((_GALLERY, "gs://")):
        return ref
    try:
        if ref.startswith(_GALLERY):
            mapped = _from_gallery(ref)
        else:
            mapped = catalog.source_ref(ref)
    except GenMediaError as exc:  # no catalogue: leave the ref
        logger.warning("Could not map %s to the catalogue: %s", ref, exc)
        return ref
    return mapped or ref


def upgrade_state(st: dict[str, Any]) -> None:
    """Upgrade a studio saved by an older version, in place: the retired
    "Animate an image" start (``patches.upgrade``) and the item refs
    mapped with ``legacy``."""
    rules.upgrade(st)
    for field in (*rules.SLOT_FIELDS, *rules.LIST_FIELDS):
        value = st.get(rules.state_key(field))
        items = value if isinstance(value, list) else [value]
        for item in items:
            if isinstance(item, dict) and isinstance(item.get("ref"), str):
                item["ref"] = legacy(item["ref"])
