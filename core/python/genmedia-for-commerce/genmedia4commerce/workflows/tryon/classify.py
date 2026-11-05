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

"""One fast classifier call over the product photos.

One row per photo (category, description, side, worn, close-up, group,
the products it shows whole), so the answer stays short, with low
thinking: try-on is slow enough. The judge model, not Flash: Flash took
26 s at the median (up to 71 s) on the same requests where the judge
model took 7 s (up to 29 s); Flash-Lite made a full-look photo its own
product (an item nobody uploaded). The groups become the products. A close-up of a part of a product (a logo, a
buckle, the fabric) is one of its detail photos (``Product.details``,
with the side of the part it frames), never one of its refs; a belt is
part of the bottom it is on. A photo of a person head to feet in a whole
outfit has the category ``full_body_look``, not a product category: a
reference, never one of a product's photos, whatever its group: the first
from the front (else the first turned one, a three-quarter pose) is the
look photo (``Classification.look_photo``), which ``reference`` cuts; the
first from the back is the back photo of the product only when there is
one product (its back view). Each photo also lists its items, the products
shown whole in it (``Classification.photos``), each in the group of the
uploaded product it is or in a new group of its own (with the uploaded
product it would replace, ``PhotoItem.instead_of``): ``hero`` picks each
product's image and the products the look adds from them. The prompt text
and schema are in ``classify_prompt``. A garment also
gets where it starts and ends (``Product.span``, which frames it alone:
``framing``); without a valid one its category frames it (warned).
"""

import logging
import string
from collections import Counter
from collections.abc import Sequence
from typing import cast

from genmedia4commerce.core import degraded, gen
from genmedia4commerce.core.errors import GenerationError
from genmedia4commerce.core.gen import TextParams
from genmedia4commerce.core.models import get_models
from genmedia4commerce.core.types import (
    FRAMING_BY_CATEGORY,
    GARMENTS,
    Category,
    Classification,
    Landmark,
    Photo,
    PhotoItem,
    Presentation,
    Product,
    ProductRef,
    View,
)
from genmedia4commerce.workflows.tryon import classify_prompt
from genmedia4commerce.workflows.tryon.classify_prompt import (
    CATEGORIES,
    FULL_BODY_LOOK,
    LANDMARKS,
    SCHEMA,
    VIEWS,
)

__all__ = ["classify_products", "routed"]

logger = logging.getLogger(__name__)


def _rows(raw, n: int) -> list[dict | None]:
    """One row per photo, ``None`` where the answer has none."""
    rows = raw.get("photos") if isinstance(raw, dict) else None
    rows = [r if isinstance(r, dict) and r else None for r in (rows or [])]
    rows = (rows + [None] * n)[:n]
    if not any(rows):
        raise GenerationError("classifier returned no photos")
    return rows


def _spans(raw) -> dict[str, tuple[Landmark, Landmark]]:
    """Each garment group's (start, end) by group key ("g3"); an entry
    with an unknown landmark, or ending above its start, is left out."""
    entries = raw.get("garments") if isinstance(raw, dict) else None
    spans: dict[str, tuple[Landmark, Landmark]] = {}
    for entry in entries if isinstance(entries, list) else []:
        g = entry.get("g") if isinstance(entry, dict) else None
        if isinstance(g, bool) or not isinstance(g, int):
            continue
        span = (entry.get("starts"), entry.get("ends"))
        if all(x in LANDMARKS for x in span):
            if LANDMARKS.index(span[0]) <= LANDMARKS.index(span[1]):
                spans.setdefault(f"g{g}", cast(tuple[Landmark, Landmark], span))
    return spans


def _set_span(product: Product, span: tuple[Landmark, Landmark] | None) -> None:
    """A garment's span; without a valid one it is framed for its
    category (``FRAMING_BY_CATEGORY``), warned."""
    if product.category not in GARMENTS:
        return
    if span is None:
        logger.warning(f"classifier gave garment {product.id} no valid span")
        degraded.report("classifier gave a garment no valid start and end")
    product.span = span


def _group(row: dict, index: int) -> str:
    g = row.get("g")
    ok = isinstance(g, int) and not isinstance(g, bool)
    return f"g{g}" if ok else f"photo{index}"  # no group: alone


def _product(letter: str, members: list[tuple[int, dict]], images) -> Product:
    """One product from its photos (upload order): the most common
    category, the description of its first photo of that category. Its
    close-ups are its detail photos, each with the side of the part it
    frames (a group of close-ups only keeps them as refs)."""
    counts: Counter[Category] = Counter(
        r["c"] for _, r in members if r.get("c") in CATEGORIES
    )
    category: Category = counts.most_common(1)[0][0] if counts else "top"
    if not counts:
        logger.warning("classifier gave a group no valid category; 'top'")
        degraded.report("classifier gave a product no valid category")
    description = next(
        (str(r.get("d") or "") for _, r in members if r.get("c") == category),
        "",
    )
    shots = [(i, r) for i, r in members if r.get("x") is not True]
    zooms = [(i, r) for i, r in members if r.get("x") is True]
    if not shots:
        shots, zooms = zooms, []
    refs, details = _refs(shots, images), _refs(zooms, images)
    return Product(letter, category, description, refs, details)


def _refs(members: list[tuple[int, dict]], images) -> list[ProductRef]:
    """Each photo with its side, worn or flat; within the same side,
    earlier photos rank first."""
    refs: list[ProductRef] = []
    ranks: Counter[View] = Counter()
    for index, row in members:
        view: View = row["s"] if row.get("s") in VIEWS else "other"
        ranks[view] += 1
        presentation: Presentation = "worn" if row.get("w") is True else "flat"
        refs.append(ProductRef(images[index], view, ranks[view], presentation))
    return refs


def _add_back(product: Product, images, index: int, row: dict) -> None:
    """The back look photo as the back photo of the single product, for
    its back view; never when it has no front photo (it would be its
    main photo)."""
    if product.refs_for("front"):
        rank = len(product.refs_for("back")) + 1
        presentation: Presentation = "worn" if row.get("w") is True else "flat"
        product.refs.append(
            ProductRef(images[index], "back", rank, presentation)
        )


def routed(
    products: list[Product],
    look_photo: int | None = None,
    photos: list[Photo] | None = None,
) -> Classification:
    """``products`` routed: one is a single product framed for its
    category (a garment's FRAMING text: from its span), several (or none
    yet: a lone look photo, which ``reference`` cuts into products) an
    outfit, full body."""
    photos = photos or []
    if len(products) == 1:
        framing = FRAMING_BY_CATEGORY[products[0].category]
        return Classification("single", products, framing, look_photo, photos)
    return Classification("outfit", products, "full_body", look_photo, photos)


def _whole(row: dict, ids: dict[str, str]) -> list[PhotoItem]:
    """The products a row shows whole, each named by the uploaded product
    it is (``ids``: group -> product id), else by its group with the
    uploaded product it would replace, if any; invalid ones are skipped."""
    items = []
    for item in row.get("items") or []:
        g = item.get("g") if isinstance(item, dict) else None
        if isinstance(g, bool) or not isinstance(g, int) or g < 0:
            continue
        if item.get("c") in CATEGORIES:
            name, key = str(item.get("d") or ""), f"g{g}"
            covered = item.get("covered") is True
            named = f"g{item.get('instead_of')}"
            instead = None if key in ids else ids.get(named)
            product = ids.get(key, key)
            items.append(PhotoItem(item["c"], name, product, covered, instead))
    return items


def _photo(row: dict | None, product: str | None, ids) -> Photo:
    """A row as a ``Photo`` of ``product`` (None: a look or skipped
    photo)."""
    row = row or {}
    view: View = row["s"] if row.get("s") in VIEWS else "other"
    worn, zoom = row.get("w") is True, row.get("x") is True
    return Photo(product, view, worn, zoom, _whole(row, ids))


def _sorted(rows: list[dict | None]):
    """The rows by kind: product groups (group -> its photos), skipped
    photos, front, turned and back look photos."""
    groups: dict[str, list[tuple[int, dict]]] = {}
    missing: list[int] = []
    looks: list[int] = []
    turned: list[int] = []
    backs: list[tuple[int, dict]] = []
    for index, row in enumerate(rows):
        if row is None:
            missing.append(index)
        elif row.get("c") != FULL_BODY_LOOK:
            groups.setdefault(_group(row, index), []).append((index, row))
        elif row.get("s") == "back":  # a look photo: never a product's
            backs.append((index, row))
        else:  # the look photo: a front one, else a turned one
            (looks if row.get("s") == "front" else turned).append(index)
    return groups, missing, looks + turned, backs


def classify_products(
    images: Sequence[bytes], reference: bytes | None = None
) -> Classification:
    """Group the uploads into products, find their detail photos, the
    products each photo shows whole and the look photo, and route single vs
    outfit (no product: only look photos). ``reference``, the reference
    model photo, goes last: it gets a row and items like an uploaded look
    photo and is the look photo, never a product's photo; it is left out
    when the uploads hold no product (a lone look photo: the uploaded look
    is the outfit to try on)."""
    photos = [*images, *([] if reference is None else [reference])]
    parts: list[gen.Part] = []
    for i, image in enumerate(photos):
        parts += [f"IMAGE_{i}:", image]
    raw = gen.generate_json(
        parts,
        system=classify_prompt.system(len(photos)),
        schema=SCHEMA,
        params=TextParams(
            model=get_models().judge,
            thinking="LOW",
            media_resolution=gen.MEDIUM_RES,
        ),
    )
    rows = _rows(raw, len(photos))
    groups, missing, looks, backs = _sorted(rows[: len(images)])
    products = [
        _product(string.ascii_uppercase[k], members, images)
        for k, members in enumerate(groups.values())
    ]
    spans = _spans(raw)
    for key, product in zip(groups, products, strict=True):
        _set_span(product, spans.get(key))
    for index in missing:  # never a product of its own
        degraded.report("classifier skipped a photo")
        if not products:
            logger.warning(f"classifier skipped photo {index}; dropped")
            continue
        logger.warning(f"classifier skipped photo {index}; added as 'other'")
        refs = products[0].refs
        refs.append(ProductRef(images[index], "other", len(refs) + 1))
    if len(products) == 1 and backs:
        _add_back(products[0], images, *backs[0])
    look = next(iter(looks), None)
    if reference is not None and products:
        look = len(images)
    else:
        rows = rows[: len(images)]
    ids = {key: p.id for key, p in zip(groups, products, strict=True)}
    owner = {i: ids[key] for key, members in groups.items() for i, _ in members}
    shown = [_photo(row, owner.get(i), ids) for i, row in enumerate(rows)]
    return routed(products, look, shown)
