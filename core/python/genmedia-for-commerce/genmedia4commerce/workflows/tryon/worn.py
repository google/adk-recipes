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

"""The worn product photos on a mannequin, before anything else uses them.

A worn product photo shows a model, whose hair, face or skin the drawing
sometimes copied. Right after the classifier, every photo it saw worn
(``Photo.worn``: product photos, a look photo, the reference model) gets
one image-model edit that turns the person into a grey mannequin, pasted
back onto the photo (``mannequin``: every product pixel stays the photo's
own), all at once. ``normalise`` swaps them into the request and the
classification by identity (what matches images with ``is`` still does),
so the box calls, crops, close-ups, heroes, cards, the plan, the prompt and
the judge all get them, and marks each (``Photo.mannequin``,
``ProductRef.mannequin``: the prompt says "on a mannequin"). The person
photo is never touched. A photo whose edit fails twice stays as it is, a
fallback (``degraded.report``). Each converted photo is kept in the
preprocessing cache (``product_cache/<key>/mannequin.json``; key: the
photo's digest, ``mannequin.VERSION`` and the image model) when
``GENMEDIA_USE_CACHE`` is on; a refresh (``core.cache.refreshing``)
remakes it.
"""

import asyncio
import dataclasses
import logging

from genmedia4commerce.core import cache, degraded, gen, trace
from genmedia4commerce.core.cache import PRODUCT_CACHE, Lookup
from genmedia4commerce.core.gen import ImageParams
from genmedia4commerce.core.models import get_models
from genmedia4commerce.core.settings import get_settings
from genmedia4commerce.core.types import Classification
from genmedia4commerce.workflows.tryon import mannequin
from genmedia4commerce.workflows.tryon.types import TryOnRequest

__all__ = ["normalise"]

logger = logging.getLogger(__name__)

TRIES = 2
_NAME = "mannequin"


def _edit(photo: bytes) -> bytes:
    """One mannequin edit of ``photo`` (its aspect ratio kept), pasted back
    onto it."""
    edit = gen.generate_image(
        [mannequin.PROMPT, photo], params=ImageParams(aspect_ratio="auto")
    )
    return mannequin.paste_back(photo, edit)


async def _made(photo: bytes) -> bytes | None:
    """``photo`` on a mannequin, ``TRIES`` tries; None when every one
    fails (reported)."""
    for attempt in range(1, TRIES + 1):
        try:
            return await asyncio.to_thread(_edit, photo)
        except Exception as exc:
            logger.warning(f"mannequin edit {attempt}/{TRIES} failed: {exc}")
    degraded.report("mannequin photo")
    return None


def _key(photo: bytes) -> str:
    """The cache key of ``photo``'s conversion (hashes it: in a thread)."""
    return cache.key(
        kind=_NAME,
        photo=cache.digest(photo),
        version=mannequin.VERSION,
        model=get_models().image,
    )


def _keep(key: str, made: bytes, replace: bool) -> None:
    """Write the conversion in the background; never raises (logged)."""
    try:
        draft = cache.draft(PRODUCT_CACHE, key, _NAME, made, replace=replace)
        cache.in_background(lambda: cache.save(draft))
    except Exception as exc:  # the cache never fails a run
        logger.warning(f"mannequin cache: not saved: {exc!r}")


async def _converted(photo: bytes) -> bytes | None:
    """``photo`` on a mannequin: kept in the cache, else made and kept;
    None when it could not be made."""
    if not get_settings().use_cache:
        return await _made(photo)
    refresh = cache.refresh_requested()
    key = await asyncio.to_thread(_key, photo)
    lookup = Lookup()
    if not refresh:
        lookup = await asyncio.to_thread(cache.load, PRODUCT_CACHE, key, _NAME)
    kept = lookup.decode(bytes)
    if kept is not None:
        return kept
    made = await _made(photo)
    if made is not None:
        _keep(key, made, refresh or lookup.stale)
    return made


def _swap(c: Classification, new: dict[int, bytes | None]) -> None:
    """Each upload of ``c`` whose image is a key of ``new`` (by ``id``)
    gets its conversion and is marked; one not converted (None) stays."""
    for product in c.products:
        for ref in [*product.refs, *product.details]:
            if ref.source == "upload" and id(ref.image) in new:
                converted = new[id(ref.image)]
                ref.mannequin = converted is not None
                ref.image = converted or ref.image


async def normalise(req: TryOnRequest, c: Classification) -> TryOnRequest:
    """``req`` with every photo ``c`` saw worn on a mannequin, in parallel;
    ``c`` gets them too, marked (its photos and uploads). A photo that could
    not be converted stays, unmarked."""
    photos = [*req.product_images, req.reference_model_image]
    worn = {
        i: photo
        for i, p in enumerate(c.photos)
        if p.worn and (photo := photos[i])
    }
    if not worn:
        return req
    done = await asyncio.gather(*(_converted(p) for p in worn.values()))
    new: dict[int, bytes | None] = {}
    for (i, photo), converted in zip(worn.items(), done, strict=True):
        c.photos[i].mannequin = converted is not None
        new[id(photo)] = converted
        if converted is not None:
            trace.save(f"mannequin-{i}", converted)
    _swap(c, new)
    model = req.reference_model_image
    return dataclasses.replace(
        req,
        product_images=[new.get(id(p)) or p for p in req.product_images],
        reference_model_image=new.get(id(model)) or model,
    )
