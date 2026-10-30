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

"""Reference images for the video model.

The try-on image is the main reference and is always sent first. Then its
back view (when there is one), the face crop and one photo per accessory or
pair of shoes (``OMNI_PHOTO_CATEGORIES``), the close-ups the animation may
zoom on. A garment gets none: it comes from the try-on image, which shows it
as checked. In a test its photo pushed the video model to redraw it (a
sleeveless coat over a blazer got sleeves, or opened, in 2 videos of 2;
without the coat's photo it stayed right in 2 of 2). Unclassified photos
(their categories unknown) all go. Each goes as a separate image: the video
model fits every reference in its frame, so a packed canvas would shrink
each one.
For the same reason each product photo is cropped to the product first
(``media.trim``; a no-op when the try-on already did): in a square packshot
the product filled under a fifth of the video frame, too small for its
details to survive a close-up. A worn photo's face (another person) has its
eyes, nose, mouth and chin blurred (``media.blur_face``): the video model's
input filter blocked a request over a model's face in a product photo.

Once the plan is known, each product it shows in a close-up gets up to two
zoomed details (a logo, a plaque, a padlock: ``details.find``) as extra
images (``add_details``). In a test the video model drew a two-tone logo
right in 5 close-ups of 6 with its zoom, against 2 of 3 from the product
photo alone; packed under the product photo in one image it did worse (0 of
3). Each search is told its product's description (``search_names``), so a
worn photo gives no other garment's detail. The search (``find_details``)
only crops, with no image-model call, so it can run during the image
try-on; only the details added are upscaled (``details.upscale``), after
the plan. Upscaling every product's details
during the try-on took the image model's quota from its drawings (429).
An image animated from its try-on's run record reuses the details that
try-on found in each chosen photo (``recorded_details``): no new search.
"""

import dataclasses
import logging
from collections.abc import Sequence

from genmedia4commerce.core import media
from genmedia4commerce.core.types import Category, Framing, Product
from genmedia4commerce.workflows import details
from genmedia4commerce.workflows.video_tryon.types import (
    Closeup,
    Plan,
    ReferenceSet,
    VideoTryOnRequest,
)

__all__ = [
    "MAX_REFERENCES",
    "OMNI_PHOTO_CATEGORIES",
    "add_details",
    "build_references",
    "classified",
    "find_details",
    "product_names",
    "product_photo",
    "product_photos",
    "recorded_details",
    "search_names",
]

logger = logging.getLogger(__name__)

MAX_REFERENCES = 10  # the video model's limit, main reference included
# Zoomed details per close-up product: legible lettering ranks first, so one
# alone can be a maker's stamp while a padlock goes wrong.
DETAILS = 2

# What the plan and the prompt call a product, and how the video shows it.
NAMES: dict[Category, tuple[str, str]] = {
    "top": ("top", "worn by the person"),
    "bottom": ("bottoms", "worn by the person"),
    "dress": ("dress", "worn by the person"),
    "full_body_outer": ("coat", "worn by the person"),
    "footwear": ("shoes", "on the person's feet"),
    "headwear": ("hat", "on the person's head"),
    "eyewear": ("glasses", "on the person's face"),
    "jewelry_head": ("earrings", "on the person's ears"),
    "jewelry_neck": ("necklace", "around the person's neck"),
    "jewelry_hand": ("bracelet", "on the person's wrist"),
    "bag": ("bag", "in the person's hand"),
}
_UNKNOWN = ("product", "worn by the person")
# The products whose photo the video model gets (see the module doc).
OMNI_PHOTO_CATEGORIES: frozenset[Category] = frozenset(
    {
        "footwear",
        "bag",
        "headwear",
        "eyewear",
        "jewelry_head",
        "jewelry_neck",
        "jewelry_hand",
    }
)


def classified(req: VideoTryOnRequest) -> list[Product]:
    """The product of each photo, in order; [] when the classification does
    not match the photos one to one (their categories are unknown)."""
    products = req.classification.products if req.classification else []
    return products if len(products) == len(req.product_images) else []


def product_names(req: VideoTryOnRequest) -> list[tuple[str, str]]:
    """(name, how the video shows it) of each product photo, after its
    product's category when ``classified``."""
    products = classified(req)
    names, used = [], set()
    for k, _ in enumerate(req.product_images):
        name, worn = NAMES[products[k].category] if products else _UNKNOWN
        base, n = name, 2
        while name in used:  # two tops: "top", "top 2"
            name, n = f"{base} {n}", n + 1
        used.add(name)
        names.append((name, worn))
    return names


def _sent(req: VideoTryOnRequest) -> list[int]:
    """The positions of the product photos the video model gets: those of
    ``OMNI_PHOTO_CATEGORIES``, or every one when not ``classified``."""
    products = classified(req)
    return [
        k
        for k, _ in enumerate(req.product_images)
        if not products or products[k].category in OMNI_PHOTO_CATEGORIES
    ]


def product_photo(req: VideoTryOnRequest, k: int) -> bytes:
    """Product photo ``k`` cropped to the product, with the face blurred
    when it is worn (or unclassified, so possibly worn)."""
    photo = req.product_images[k]
    products = classified(req)
    if not products:
        return media.blur_face(media.trim(photo))
    refs = [r for r in products[k].refs if r.image == photo]
    if not any(r.presentation == "worn" for r in refs):
        return media.trim(photo)
    eyewear = products[k].category == "eyewear"
    return media.blur_face(media.trim(photo), keep_eyes=eyewear)


def build_references(
    req: VideoTryOnRequest, *, face: bytes | None, framing: Framing | None
) -> ReferenceSet:
    """Return the ordered references for one run.

    ``req.tryon_image`` is the main reference; ``face`` its face crop (None
    when it has no face); ``framing`` its framing (None: full body). At most
    ``MAX_REFERENCES`` images: the last product photos are dropped first.
    Only the accessories and shoes have a photo (see the module doc).
    """
    images = [req.animated_image]
    back = face_i = None
    if req.back_image:
        images.append(req.back_image)
        back = len(images) - 1
    if face:
        images.append(face)
        face_i = len(images) - 1
    room, sent = MAX_REFERENCES - len(images), _sent(req)
    names, described = product_names(req), search_names(req)
    if len(sent) > room:
        logger.info(f"dropping {len(sent) - room} product photos")
    closeups = [
        Closeup(len(images) + j, *names[k], product=described[j])
        for j, k in enumerate(sent[:room])
    ]
    images += [product_photo(req, k) for k in sent[:room]]
    return ReferenceSet(images, framing or "full_body", back, face_i, closeups)


def product_photos(req: VideoTryOnRequest) -> list[bytes]:
    """Every product photo ``build_references`` sends (cropped, face
    blurred), in order: what the detail search looks at."""
    return [product_photo(req, k) for k in _sent(req)]


def search_names(req: VideoTryOnRequest) -> list[str]:
    """The product of every photo sent (``product_photos``' order) as the
    detail search is told it: its description; "" when not ``classified``
    (its category unknown, the search asks as for any product)."""
    products = classified(req)
    return [products[k].description if products else "" for k in _sent(req)]


def recorded_details(
    req: VideoTryOnRequest,
) -> list[list[details.Detail]] | None:
    """The details of every photo sent (``product_photos``' order) that the
    try-on found (``req.closeups``); None when it found none for them."""
    if req.closeups is None or len(req.closeups) != len(req.product_images):
        return None
    return [req.closeups[k][:DETAILS] for k in _sent(req)]


def find_details(
    photos: Sequence[bytes], products: Sequence[str], limit: int = DETAILS
) -> list[list[details.Detail]]:
    """The zoomed details of each photo (``details.find_each``), all at
    once, each search told its product (``products``, by position: only
    details on it), cropped but not upscaled: no image-model call, safe to
    run during the image stage."""
    return details.find_each(photos, limit=limit, products=products)


def add_details(
    refs: ReferenceSet,
    plan: Plan,
    found: Sequence[list[details.Detail]] | None = None,
) -> ReferenceSet:
    """``refs`` plus up to ``DETAILS`` zoomed details per product ``plan``
    shows in a close-up, found in its (cropped, face-blurred) photo and sent
    last, within ``MAX_REFERENCES``. Without one the product goes as before.

    ``found``: the details of every photo sent (``product_photos``),
    searched ahead (by position); without it the close-up products are
    searched now, at once, each told its product (``Closeup.product``).
    Only the details added are upscaled, all at once (a failed upscale
    keeps the crop).
    """
    shown = {s.region for s in plan.shots if s.move == "detail"}
    wanted = [k for k, c in enumerate(refs.closeups) if c.name in shown]
    left = MAX_REFERENCES - len(refs.images)
    if found is None:
        photos = [refs.images[refs.closeups[k].index] for k in wanted]
        named = [refs.closeups[k].product for k in wanted]
        room = min(DETAILS, left)
        hits = find_details(photos, named, room) if room > 0 else []
        by_product = dict(zip(wanted, hits, strict=False))
    else:
        by_product = {k: found[k] for k in wanted if k < len(found)}
    chosen: list[tuple[int, details.Detail]] = []
    for k in sorted(by_product):
        take = by_product[k][: max(min(DETAILS, left), 0)]
        chosen += [(k, d) for d in take]
        left -= len(take)
    upscaled = details.upscale([d for _, d in chosen]) if chosen else []
    images = list(refs.images)
    zooms: dict[int, list[tuple[int, str]]] = {}
    for (k, _), d in zip(chosen, upscaled, strict=True):
        images.append(d.image)
        zooms.setdefault(k, []).append((len(images) - 1, d.where))
    closeups = [
        dataclasses.replace(c, details=[*c.details, *zooms.get(k, [])])
        for k, c in enumerate(refs.closeups)
    ]
    return dataclasses.replace(refs, images=images, closeups=closeups)
