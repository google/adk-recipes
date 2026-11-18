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

"""One hero image per product, chosen in code from the classifier's
inventory (``Classification.photos``: the products each photo shows
whole), with no model call. The hero is the product's first image: the
drawing's card, the styling plan and the judge get it. In order:

1. worn: a whole photo of the product worn, from the front, that lists it
   among its items (the best rank first);
2. crop: the largest (pixel area) of its boxes (``cut.box_items``) in
   every photo facing front that shows it whole and not covered, on a tie
   the look photo's, then upload order; never one of its own flat photos,
   a turned or a back photo (``crop_sources``). A crop whose long side is
   under ``MIN_HERO_SIDE`` px or short side under ``MIN_HERO_SHORT_SIDE``
   px gives way to a flat photo (3); a product the box calls cannot find
   goes on;
3. flat: a whole flat photo of it from the front that lists it among its
   items (the best rank first);
4. fallback: its best front upload, else its best upload
   (``outfit.best_upload``).

A product the look adds (``added``) has no upload: it can only get a
crop, also from a turned look photo and also covered. A look item in an
uploaded product's place (``conflicts``) is added or not by the look
conflict rule (``resolved``).
"""

from collections.abc import Sequence
from typing import cast

from genmedia4commerce.core import media
from genmedia4commerce.core.settings import LookConflict, get_settings
from genmedia4commerce.core.types import (
    SINGLE_SLOT,
    Classification,
    Photo,
    PhotoItem,
    Product,
    ProductRef,
)
from genmedia4commerce.workflows.details import Detail
from genmedia4commerce.workflows.tryon.outfit import best_upload

__all__ = [
    "MIN_HERO_SHORT_SIDE",
    "MIN_HERO_SIDE",
    "added",
    "conflict_message",
    "conflicts",
    "crop_sources",
    "pick",
    "product_photo",
    "resolved",
    "worn",
]

# A crop whose long side is under this (shoes, a cap, sunglasses cut from a
# full-body photo) is no hero when the product has a flat front upload.
MIN_HERO_SIDE = 400
# Nor one whose short side is under this (a bag hanging edge-on).
MIN_HERO_SHORT_SIDE = 150


def resolved(look_conflict: str | None) -> LookConflict:
    """``look_conflict``, else ``GENMEDIA_LOOK_CONFLICT``."""
    return cast(LookConflict, look_conflict or get_settings().look_conflict)


def _look_items(c: Classification) -> list[PhotoItem]:
    """The items of no uploaded product that the look photo shows (without
    a look photo, any photo), each at its first appearance."""
    look = c.look_photo
    photos = c.photos if look is None else [c.photos[look]]
    uploaded = {p.id for p in c.products}
    first: dict[str, PhotoItem] = {}
    for photo in photos:
        for item in photo.items:
            if item.product not in uploaded:
                first.setdefault(item.product, item)
    return list(first.values())


def _in_place_of(item: PhotoItem, p: Product) -> bool:
    """The look item ``item`` would take the uploaded ``p``'s place."""
    if item.category in SINGLE_SLOT:
        return item.category == p.category
    return item.instead_of == p.id


def conflicts(c: Classification) -> list[tuple[PhotoItem, Product]]:
    """Each look item (``_look_items``) in an uploaded product's place,
    with that product: one of its ``SINGLE_SLOT`` category, else the one
    the classifier says it would replace (``PhotoItem.instead_of``)."""
    out = []
    for item in _look_items(c):
        rivals = [p for p in c.products if _in_place_of(item, p)]
        out += [(item, rivals[0])] if rivals else []
    return out


def conflict_message(conflict: dict[str, str]) -> str:
    """One line on a ``Classification.look_conflicts`` entry."""
    look, upload = conflict["look_name"], conflict["upload_name"]
    if conflict["kept"] == "look":
        return f"Used the look's {look} instead of the uploaded {upload}"
    return f"Kept the uploaded {upload} and left out the look's {look}"


def added(c: Classification, look_conflict: str | None = None) -> list[Product]:
    """The products the look adds: its items (``_look_items``) but, with
    the rule "upload" (``resolved``), those in an uploaded product's place
    (``conflicts``), each with its key ("g7") as id and no image yet."""
    out = set()
    if resolved(look_conflict) == "upload":
        out = {item.product for item, _ in conflicts(c)}
    return [
        Product(i.product, i.category, i.name, [])
        for i in _look_items(c)
        if i.product not in out
    ]


def _shows(photo: Photo, p: Product, *, uncovered: bool = False) -> bool:
    """``photo`` lists ``p`` among its items (with ``uncovered``: not
    covered by another garment)."""
    return any(
        item.product == p.id and not (uncovered and item.covered)
        for item in photo.items
    )


def _whole(
    p: Product, c: Classification, images: Sequence[bytes], *, is_worn: bool
) -> ProductRef | None:
    """``p``'s best whole upload from the front, worn or flat: one of its
    own photos that is no zoom photo and lists it among its items."""
    whole = {
        id(images[i])
        for i, photo in enumerate(c.photos)
        if photo.product == p.id and not photo.zoom and _shows(photo, p)
    }
    return next(
        (
            r
            for r in p.refs_for("front")
            if r.source == "upload"
            and id(r.image) in whole
            and (r.presentation == "worn") is is_worn
        ),
        None,
    )


def worn(
    p: Product, c: Classification, images: Sequence[bytes]
) -> ProductRef | None:
    """``p``'s best whole worn upload from the front (tier 1); None
    without one. ``images``: the photos of ``c.photos``, in order."""
    return _whole(p, c, images, is_worn=True)


def crop_sources(p: Product, c: Classification) -> list[int]:
    """Every photo ``p``'s crop may be cut from, the look photo first, then
    upload order: each that faces front and shows ``p`` whole, not covered;
    never one of ``p``'s own flat photos. A product the look adds: also the
    look photo turned and covered. An uploaded product with none of these
    and no flat front photo: the turned look photo when it shows it whole,
    not covered (better than a partial photo). [] without one."""
    is_added = all(u.id != p.id for u in c.products)
    sources = []
    for i in sorted(range(len(c.photos)), key=lambda k: k != c.look_photo):
        photo = c.photos[i]
        facing = photo.view == "front" or (is_added and i == c.look_photo)
        if not facing or (photo.product == p.id and not photo.worn):
            continue
        if _shows(photo, p, uncovered=not is_added):
            sources.append(i)
    if sources or c.look_photo is None or is_added or _has_flat(p, c):
        return sources
    look = c.photos[c.look_photo]
    return [c.look_photo] if _shows(look, p, uncovered=True) else []


def _has_flat(p: Product, c: Classification) -> bool:
    """``p`` has a whole flat upload from the front (its tier 3)."""
    return any(
        photo.product == p.id
        and not photo.worn
        and not photo.zoom
        and photo.view == "front"
        and _shows(photo, p)
        for photo in c.photos
    )


def pick(
    p: Product,
    crop: ProductRef | None,
    c: Classification,
    images: Sequence[bytes],
) -> tuple[str, ProductRef] | None:
    """``p``'s hero and its tier ("worn", "crop", "flat", "fallback"), with
    ``crop`` its crop (None: none was cut); None for a product with neither
    an upload nor a crop."""
    if (ref := worn(p, c, images)) is not None:
        return "worn", ref
    flat = _whole(p, c, images, is_worn=False)
    if crop is not None and (flat is None or _big(crop)):
        return "crop", crop
    if flat is not None:
        return "flat", flat
    if any(r.source == "upload" for r in p.refs):
        return "fallback", best_upload(p)
    return None


def _big(crop: ProductRef) -> bool:
    """``crop`` is large enough to beat a flat upload: its long side at
    least ``MIN_HERO_SIDE`` px, its short side ``MIN_HERO_SHORT_SIDE``."""
    width, height = media.image_size(crop.image)
    short, long = sorted((width, height))
    return long >= MIN_HERO_SIDE and short >= MIN_HERO_SHORT_SIDE


def product_photo(p: Product) -> list[Detail]:
    """Shoes' first whole front upload, as a close-up photo (the kind
    ``closeups.PHOTO``) on their card, when their hero is a crop of the
    look photo: the crop shows how they are worn, the photo what they are
    (a strap, a toe cap or lettering the look hides); [] otherwise."""
    if p.category != "footwear":
        return []
    if not p.refs or p.refs[0].source != "model_crop":
        return []
    front = [r for r in p.refs if r.source == "upload" and r.view == "front"]
    if not front:
        return []
    image = front[0].image
    return [Detail("photo", "", "front", "", image, name="product photo")]
