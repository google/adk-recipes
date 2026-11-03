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

"""The look photo and the crops of the products.

The look photo (``Classification.look_photo``: the reference model the
request names, else a look photo among the uploads) and every photo a
product's crop may be cut from (``hero.crop_sources``: for a product with
no whole worn upload) get one box call each (``cut.box_items``, told every
product that photo is a source for), in parallel, at most ``MAX_BOX_CALLS``
at a time. The look photo, whatever ``complete_look``, gives a top and a
bottom half (only when the hem on the legs reaches the shoes), which go to
the products of their categories as worn examples for the styling plan and
the drawing: they show how garments meet (tucked or not, a coat over
trousers, a hem over a shoe). Each photo gives the crops of the products
asked of it, and each product keeps its largest (pixel area; on a tie the
look photo's, then upload order). With ``complete_look``, the products the
look adds (``hero.added``) join, each with its crop as only image (added
shoes are upscaled only with ``upscale_crops``). A lone look photo (no
product photo besides) is the outfit to try on: every product it shows is
added, whatever ``complete_look``. Which item is which product comes from
the classifier's groups: no second classification, no name matching. Then
each product's hero (``hero.pick``) becomes its first image; the largest
upper-body hero crop of the look photo boxed like the top half gets the
half itself, and so does the largest bottom one boxed like the bottom
half; a half so taken is not sent again.
"""

import asyncio
import logging
import string
from collections.abc import Sequence

from genmedia4commerce.core import degraded, media, trace
from genmedia4commerce.core.errors import InputError
from genmedia4commerce.core.types import (
    Category,
    Classification,
    Presentation,
    Product,
    ProductRef,
)
from genmedia4commerce.workflows.tryon import hero
from genmedia4commerce.workflows.tryon.classify import (
    classify_products,
    routed,
)
from genmedia4commerce.workflows.tryon.cut import Boxed, box_items

__all__ = [
    "BAND_CATEGORIES",
    "attach_reference_bands",
    "classify",
    "complete",
    "upscale_shoes",
]

logger = logging.getLogger(__name__)

MAX_PRODUCTS = 8  # the uploaded products first; the look's fill up
MAX_BOX_CALLS = 4  # box calls at once: the judge model stalls with more

# Band -> the categories it shows. The reference model wears the uploaded
# look, so no model call matches them. A dress or long coat shows in both
# bands (its length and hem are in the bottom one).
BAND_CATEGORIES: dict[str, tuple[Category, ...]] = {
    "top": ("top", "full_body_outer", "dress", "jewelry_neck"),
    "bottom": ("bottom", "full_body_outer", "dress"),
}


def attach_reference_bands(
    classification: Classification, bands: dict[str, bytes]
) -> None:
    """Give each product the bands its category shows, as worn front
    references (``source="model_crop"``), marked when the look photo is on
    a mannequin (``worn``)."""
    look = classification.look_photo
    photos = classification.photos
    on = look is not None and look < len(photos) and photos[look].mannequin
    for product in classification.products:
        for band, categories in BAND_CATEGORIES.items():
            if band in bands and product.category in categories:
                product.refs.append(
                    ProductRef(
                        bands[band],
                        "front",
                        presentation="worn",
                        source="model_crop",
                        mannequin=on,
                    )
                )


def upscale_shoes(crop: bytes) -> bytes:
    """The shoe crop upscaled to 2K (a full-body photo shows them small);
    the crop itself when the upscale fails."""
    try:
        return media.upscale(crop, image_size="2K")
    except Exception as exc:
        logger.warning(f"shoe upscale failed, keeping the crop: {exc}")
        degraded.report("shoe upscale")
        return crop


async def _upscale_added_shoes(
    classification: Classification, crops: dict[str, bytes]
) -> None:
    """Shoes added from the look photo get their crop upscaled."""
    refs = [
        p.refs[0]
        for p in classification.products
        if p.category == "footwear"
        and all(r.source == "model_crop" for r in p.refs)
    ]
    sharp = await asyncio.gather(
        *(asyncio.to_thread(upscale_shoes, r.image) for r in refs)
    )
    for ref, image in zip(refs, sharp, strict=True):
        name = next(n for n, c in crops.items() if c is ref.image)
        crops[name] = ref.image = image


def _asked(
    c: Classification, products: Sequence[Product], images: Sequence[bytes]
) -> dict[int, list[Product]]:
    """Each photo to box, with the products asked of it: the look photo
    (its halves), and every crop source of each product with no whole worn
    upload."""
    asked: dict[int, list[Product]] = {}
    if c.look_photo is not None:
        asked[c.look_photo] = []
    for p in products:
        if hero.worn(p, c, images) is None:
            for source in hero.crop_sources(p, c):
                asked.setdefault(source, []).append(p)
    return asked


async def _box(
    images: Sequence[bytes], asked: dict[int, list[Product]], look: int | None
) -> dict[int, Boxed]:
    """One box call per photo of ``asked``, at most ``MAX_BOX_CALLS`` at
    once; the look photo's halves too."""
    gate = asyncio.Semaphore(MAX_BOX_CALLS)

    async def one(index: int, products: list[Product]) -> Boxed:
        items = [(p.category, p.description) for p in products]
        async with gate:
            return await asyncio.to_thread(
                box_items, images[index], items, bands=index == look
            )

    done = await asyncio.gather(*(one(i, ps) for i, ps in asked.items()))
    return dict(zip(asked, done, strict=True))


def _crops(
    c: Classification,
    asked: dict[int, list[Product]],
    boxed: dict[int, Boxed],
) -> dict[str, ProductRef]:
    """Each product's crop, by id: its largest (pixel area), on a tie the
    look photo's, then the first in upload order; marked when its photo is
    on a mannequin (``worn``)."""
    crops: dict[str, ProductRef] = {}
    areas: dict[str, int] = {}
    for index in sorted(asked, key=lambda i: (i != c.look_photo, i)):
        photo = c.photos[index]
        how: Presentation = "worn" if photo.worn else "flat"
        for p, crop in zip(asked[index], boxed[index].crops, strict=True):
            if crop is None:
                continue
            width, height = media.image_size(crop)
            if width * height > areas.get(p.id, 0):
                areas[p.id] = width * height
                crops[p.id] = ProductRef(
                    crop,
                    "front",
                    presentation=how,
                    source="model_crop",
                    mannequin=photo.mannequin,
                )
    return crops


def _halves(asked: list[Product], look: Boxed | None) -> dict[str, bytes]:
    """The look photo's halves to give the products of their categories
    (``asked``: the products asked of it): for each half, when hero crops
    of products are boxed like it (``Boxed.top_half``, upper-body ones;
    ``Boxed.bottom_half``, bottoms), the largest takes the half itself as
    its image, and the half is not sent again."""
    if look is None:
        return {}
    bands = dict(look.bands)
    for band, like in (("top", look.top_half), ("bottom", look.bottom_half)):
        for k in like if band in bands else []:
            hero = asked[k].refs[0] if asked[k].refs else None
            if hero is not None and hero.image is look.crops[k]:
                hero.image = bands.pop(band)
                break
    return bands


def _heroes(
    c: Classification,
    products: Sequence[Product],
    crops: dict[str, ProductRef],
    images: Sequence[bytes],
) -> list[Product]:
    """The products with their hero first (``hero.pick``), the added ones
    lettered after the uploaded ones, up to ``MAX_PRODUCTS``; an added
    product with no crop is dropped."""
    kept: list[Product] = []
    tiers: dict[str, str] = {}
    for n, p in enumerate(products):
        is_added = n >= len(c.products)
        if is_added and len(kept) >= MAX_PRODUCTS:
            break
        picked = hero.pick(p, crops.get(p.id), c, images)
        if picked is None:
            logger.info(f"look: {p.category} {p.description!r} not cropped")
            continue
        tier, ref = picked
        if is_added:
            p.id = string.ascii_uppercase[len(kept)]
        p.refs = [ref, *(r for r in p.refs if r is not ref)]
        tiers[p.id] = tier
        logger.info(f"hero of {p.id} ({p.description!r}): {tier}")
        kept.append(p)
    trace.save("heroes", tiers)
    return kept


def _named(bands: dict[str, bytes], products: Sequence[Product]):
    """Every crop by name: the halves, then each crop hero (``item-<k>``)
    that is no half."""
    named = dict(bands)
    crops = [
        p.refs[0].image
        for p in products
        if p.refs[0].source == "model_crop"
        and not any(p.refs[0].image is b for b in bands.values())
    ]
    named |= {f"item-{k}": crop for k, crop in enumerate(crops)}
    return named


def _checked(c: Classification) -> Classification:
    """``c``; ``InputError`` when it has no product (a lone look photo
    where none was found)."""
    if not c.products:
        raise InputError("no product to try on was found in the photos")
    return c


async def complete(
    classification: Classification,
    product_images: Sequence[bytes],
    reference_model_image: bytes | None,
    complete_look: bool,
    *,
    upscale_crops: bool = False,
) -> tuple[Classification, dict[str, bytes]]:
    """Box the look photo and the crop sources (``_asked``, ``_box``): the
    halves go to the products of their categories, each crop to its
    product, and, with ``complete_look`` or a lone look photo, the products
    the look adds join (added shoes upscaled with ``upscale_crops``); each
    product's hero is its first image. No product at all is an
    ``InputError``. Also returns every crop by name (halves, ``item-<k>``).
    """
    c = classification
    model = [] if reference_model_image is None else [reference_model_image]
    images = [*product_images, *model][: len(c.photos)]
    lone = not c.products  # only look photos: try on their outfit
    added = hero.added(c) if complete_look or lone else []
    products = [*c.products, *added]
    asked = _asked(c, products, images)
    boxed = await _box(images, asked, c.look_photo)
    kept = _heroes(c, products, _crops(c, asked, boxed), images)
    done = _checked(routed(kept, c.look_photo, c.photos))
    k = c.look_photo
    look = None if k is None else boxed.get(k)
    bands = _halves([] if k is None else asked.get(k, []), look)
    named = _named(look.bands if look else {}, kept)
    if upscale_crops:
        await _upscale_added_shoes(done, named)
    attach_reference_bands(done, bands)
    return done, named


async def classify(
    product_images: Sequence[bytes],
    reference_model_image: bytes | None,
    complete_look: bool,
    *,
    upscale_crops: bool = False,
) -> tuple[Classification, dict[str, bytes]]:
    """Classify the uploads and the reference model
    (``classify_products``), then box them (``complete``)."""
    found = await asyncio.to_thread(
        classify_products, product_images, reference_model_image
    )
    return await complete(
        found,
        product_images,
        reference_model_image,
        complete_look,
        upscale_crops=upscale_crops,
    )
