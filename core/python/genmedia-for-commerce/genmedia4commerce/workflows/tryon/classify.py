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
uploaded product it is or in a new group of its own: ``hero`` picks each
product's image and the products the look adds from them. A garment also
gets where it starts and ends (``Product.span``, which frames it alone:
``framing``); without a valid one its category frames it (warned).
"""

import logging
import string
from collections import Counter
from collections.abc import Sequence
from typing import cast, get_args

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

__all__ = ["classify_products", "routed"]

logger = logging.getLogger(__name__)

_CATEGORIES = get_args(Category)
_VIEWS = get_args(View)
_LANDMARKS = get_args(Landmark)  # top to bottom
_FULL_BODY_LOOK = "full_body_look"  # a whole-outfit photo, never a product

_CATEGORY_RULES = """Categories:
- "top": shirts, t-shirts, blouses, sweaters, hoodies, turtlenecks, tank tops, vests, short jackets, cropped coats (any upper body garment that ends at or above the hips)
- "full_body_outer": long coats, trench coats, long cardigans, dusters, any outerwear that extends below the hips/to the knees or longer
- "bottom": pants, trousers, jeans, shorts, skirts (ONLY if no straps or bib — pure waist-down garments)
- "dress": dresses, jumpsuits, rompers, overalls, bib shorts, bib pants, cycling bibs, dungarees, any garment with shoulder straps or bib construction (any single garment that extends both above and below the waist)
- "footwear": any footwear (sneakers, heels, boots, sandals, slippers, cleats)
- "headwear": hats, caps, beanies, headbands
- "eyewear": glasses, sunglasses
- "jewelry_head": earrings and other jewellery worn on the ears or face
- "jewelry_neck": necklaces, pendants, chokers, scarves
- "jewelry_hand": rings, bracelets, watches
- "bag": handbags, shoulder bags, backpacks, totes, any carried bag"""

# Where each garment starts and ends (``Product.span``).
_GARMENT_RULES = (
    'GARMENTS: "garments" has one entry per group of photos whose category '
    "is "
    + ", ".join(f'"{g}"' for g in GARMENTS[:-1])
    + f' or "{GARMENTS[-1]}", none for any other: "g" its group, "starts" '
    'the highest point and "ends" the lowest point the garment reaches on a '
    "standing person, as its photos show it, each one of, from the top "
    f"down: {', '.join(_LANDMARKS)}. Shoulder straps and a bib start at the "
    "shoulders. For example: a t-shirt starts at the shoulders and ends at "
    "the hips; cycling bib shorts start at the shoulders and end at "
    "mid-thigh; trousers start at the waist and end at the ankles; a "
    "knee-length coat starts at the shoulders and ends at the knees."
)

_SYSTEM = """You are a product classifier for virtual try-on. You receive {n} product photo(s), labelled IMAGE_0 to IMAGE_{last}, in upload order. Look at all of them together: comparing photos of the same item is how you tell front from back.

Return one row per photo, in the same order:
- "c": the CATEGORY, one of: {categories}. "full_body_look" is a full-body look photo (see FULL-BODY LOOK below); every other value is the category of the product the photo shows.
- "d": a short generic NAME of the product: its type and cut in 1 to 4 words (e.g. "cycling bib shorts", "t-bar loafers", "sleeveless wrap coat"). No colour, material, pattern, logo, brand, text or hardware: the photos show those. Only when two different products would get the same name, add the one word that tells them apart. A "full_body_look" photo: "full model reference".
- "s": the SIDE the photo shows: "front", "back" or "other".
- "w": true when the product is photographed on a person and the photo focuses on it; false for a packshot, flat-lay, mannequin, or a person who is not the focus.
- "x": true when the photo shows no product in full: it frames one or more parts of the product (a logo, a buckle, a pocket, the waist and the hip, a sleeve and its cuff, the fabric) and leaves the rest of the product outside the frame. false when the whole product is inside the frame, worn or flat, from any side, even small; a part hidden by another garment still counts as inside the frame. false also for a close-up whose main subject is something worn with the product (another garment, an accessory).
- "g": the GROUP number. Photos of the same item (other angles, worn or flat, close-ups of its parts) share the group number, the category and the name. Number the groups 1, 2, 3, ... in order of first appearance. A "full_body_look" photo is a reference, not a product photo: its group is -1. Any other photo of a person wearing several uploaded items goes in the group of the item it focuses on. A belt is part of the bottom it is on (skirt, trousers), never a product of its own: its photos go in the group of that bottom.

FULL-BODY LOOK: a photo of a person seen from head to feet (head to shoes) wearing a whole outfit, several garments with the shoes included, is a "full_body_look". It is a styling reference that shows how the products are worn together, never a photo of one product, even when one of the uploaded products is in it. A photo cut at the knees, the thighs or the waist, a photo that focuses on one garment, a close-up, a flat lay and a packshot are never a "full_body_look". A "full_body_look" photo has the name "full model reference" and the group -1; its "s" is the side it shows ("front", "back" or "other"), "w" is true and "x" is false.

ITEMS: for every photo, "items" lists every product shown whole inside the frame, one entry per product: garments, shoes, bags, hats, glasses, jewellery and scarves. A part hidden by another garment still counts as inside the frame; a product cut off by the frame is never listed. For each item: "c" its category (a product category, never "full_body_look"), "d" its short name (the same rules as "d" above), "g" its group. "covered" is true when another garment hides most of the item (an inner layer under a jacket or coat, a shirt showing only its collar), else false. The group is the group of the uploaded product when the item is that product: the same item as in the other photos, even worn open instead of closed, seen from another side, or partly covered. Every part of an uploaded product is that product: its lining, fur, hood, collar, cuffs or belt are never items of their own. Any other item gets a new group number, after every group of the photos; the same item in two photos has the same group. A belt worn on a bottom is part of that bottom. For a "full_body_look" photo whose "s" is "back", "items" is [].

{garment_rules}

STRICT RULE: The entire product must be visible in the final photo. If a garment has ANY component that extends above the waist (shoulder straps, bib front, suspenders, crossed back straps), it MUST be classified as "dress" — even if the lower portion looks like shorts or pants. This includes cycling bib shorts, ski bib pants, fishing waders, overalls, dungarees, and any garment with built-in straps.

You must also consider the LENGTH of the garment:
- A short jacket (waist-length or hip-length) is a "top"
- A long coat or trench coat that extends below the hips/to the knees or longer is a "full_body_outer"

{category_rules}

View classification for each image (worn photos too: a person facing the camera shows the front of what they wear):
- "front": the front of the product squarely facing the camera, its left and right halves equally visible. For footwear, a side (lateral) or 3/4 view showing the shoe's main design counts as "front". For a zoom photo ("x" true): the part it frames is on the front of the product.
- "back": any view of the back of the product, straight or turned, even slightly. For footwear, the heel view. For a zoom photo: the part it frames is on the back of the product.
- "other": a front turned away from the camera, from a three-quarter to a side view (except footwear); a top-down or sole-only view; several products in one image; or not a valid product photo. For a zoom photo: the part it frames is on a side of the product, or has no side (a fabric swatch).

How to determine front vs back — use these cues in order of reliability:
1. **Closures and hardware**: buttons, main zippers, snaps, front clasps → FRONT. Back zippers (e.g. dresses) → BACK.
2. **Pockets**: front hip pockets, chest pockets → FRONT. Back patch pockets (e.g. jeans, cycling jerseys) → BACK.
3. **Garment construction and cut**: the FRONT of a garment typically has MORE fabric coverage and structure on the torso (wider panels, fuller coverage). The BACK typically has LESS coverage or thinner construction (racerback straps, cutaway shoulders, thinner panels, open-back designs). For example: vests have a full front panel but a thinner back; tank tops have wider straps in front but a racerback behind; cycling bibs have a bib panel covering the front abdomen while the back has a mesh racerback with a chamois pad in the seat area.
4. **Neckline shape**: deeper V-neck, scoop neck, or decorative neckline → usually FRONT. Higher, simpler neckline → usually BACK.
5. **Tags and labels**: small fabric tags at the inner neckline are typically at the BACK of the garment.

Return JSON only."""

_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "photos": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "c": {
                        "type": "STRING",
                        "enum": [*_CATEGORIES, _FULL_BODY_LOOK],
                    },
                    "d": {"type": "STRING"},
                    "s": {"type": "STRING", "enum": list(_VIEWS)},
                    "w": {"type": "BOOLEAN"},
                    "x": {"type": "BOOLEAN"},
                    "g": {"type": "INTEGER"},
                    "items": {
                        "type": "ARRAY",
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                "c": {
                                    "type": "STRING",
                                    "enum": list(_CATEGORIES),
                                },
                                "d": {"type": "STRING"},
                                "g": {"type": "INTEGER"},
                                "covered": {"type": "BOOLEAN"},
                            },
                            "required": ["c", "d", "g", "covered"],
                        },
                    },
                },
                "required": ["c", "d", "s", "w", "x", "g", "items"],
            },
        },
        "garments": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "g": {"type": "INTEGER"},
                    "starts": {"type": "STRING", "enum": list(_LANDMARKS)},
                    "ends": {"type": "STRING", "enum": list(_LANDMARKS)},
                },
                "required": ["g", "starts", "ends"],
                "propertyOrdering": ["g", "starts", "ends"],
            },
        },
    },
    "required": ["photos", "garments"],
    "propertyOrdering": ["photos", "garments"],
}


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
        if all(x in _LANDMARKS for x in span):
            if _LANDMARKS.index(span[0]) <= _LANDMARKS.index(span[1]):
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
        r["c"] for _, r in members if r.get("c") in _CATEGORIES
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
        view: View = row["s"] if row.get("s") in _VIEWS else "other"
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


def _system(n: int) -> str:
    return _SYSTEM.format(
        n=n,
        last=n - 1,
        categories=", ".join([*_CATEGORIES, _FULL_BODY_LOOK]),
        category_rules=_CATEGORY_RULES,
        garment_rules=_GARMENT_RULES,
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
    it is (``ids``: group -> product id), else by its group; invalid ones
    are skipped."""
    items = []
    for item in row.get("items") or []:
        g = item.get("g") if isinstance(item, dict) else None
        if isinstance(g, bool) or not isinstance(g, int) or g < 0:
            continue
        if item.get("c") in _CATEGORIES:
            name, key = str(item.get("d") or ""), f"g{g}"
            covered = item.get("covered") is True
            product = ids.get(key, key)
            items.append(PhotoItem(item["c"], name, product, covered))
    return items


def _photo(row: dict | None, product: str | None, ids) -> Photo:
    """A row as a ``Photo`` of ``product`` (None: a look or skipped
    photo)."""
    row = row or {}
    view: View = row["s"] if row.get("s") in _VIEWS else "other"
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
        elif row.get("c") != _FULL_BODY_LOOK:
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
        system=_system(len(photos)),
        schema=_SCHEMA,
        params=TextParams(model=get_models().judge, thinking="LOW"),
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
