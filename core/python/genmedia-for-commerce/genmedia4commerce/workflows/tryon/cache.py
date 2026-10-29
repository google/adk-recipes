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

"""The photo try-on's preprocessing cache (``core.cache``): what a run
works out from the product photos before it draws, kept for the next run
with the same photos. Nothing from the person photo is kept: its
preparation (``inputs.wearer``) runs on every run.

- The products, ``product_cache/<key>/analysis.json``. Key: the product
  photos' digests in their order, the reference model's (or "none"),
  ``complete_look``, ``upscale_crops`` and ``GENMEDIA_TRYON_CLOSEUPS``.
  Value (``Analysis``): the classification (each product with its photos,
  its hero first, and detail photos, the look photo, and each photo as the
  classifier saw it, with the products it shows whole), the crops by name
  and the close-up search's result (each product searched with its
  close-ups; None without a search). The request's photos are not stored:
  they are named by position and a hit is rebuilt around this request's
  own (trimmed) photos, as ``record.payload`` matches images by identity.
  Trimming is not cached (it is quick and the same every time).
- The outfit's styling plan, ``product_cache/<key>/plan-<digest of the
  styling rules used>.json``, valid only for the analysis entry it names:
  new rules recompute the plan alone.

Every key also holds the judge and image model ids and the code
fingerprint. A result is kept only when it took no fallback
(``core.degraded``: a failed close-up search or shoe upscale), and
only once the whole preprocessing is over, in the background (``save``): a
failed or cancelled run keeps nothing. A refresh
(``core.cache.refreshing``, the Studio's "Try again") reads nothing and
replaces each entry. ``classify`` is ``reference.classify`` through the
same cache, for a video try-on that classifies its photos itself.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from genmedia4commerce.core import cache, degraded
from genmedia4commerce.core.cache import PRODUCT_CACHE, Lookup
from genmedia4commerce.core.models import get_models
from genmedia4commerce.core.settings import get_settings
from genmedia4commerce.core.types import (
    Classification,
    Photo,
    PhotoItem,
    Product,
    ProductRef,
)
from genmedia4commerce.workflows.details import Detail
from genmedia4commerce.workflows.tryon import outfit, reference
from genmedia4commerce.workflows.tryon.closeups import Found
from genmedia4commerce.workflows.tryon.types import (
    CompositionPlan,
    TryOnRequest,
)

__all__ = ["Analysis", "TryOnCache", "classify"]

logger = logging.getLogger(__name__)

ANALYSIS = "analysis"
_ANALYSIS_TYPES = (
    Classification,
    Product,
    ProductRef,
    Photo,
    PhotoItem,
    Detail,
)


@dataclass
class Analysis:
    """What a run worked out from the product photos."""

    classification: Classification
    crops: dict[str, bytes]  # the halves and product crops, by name
    found: Found | None  # the close-up search's result; None: no search

    def parts(self) -> tuple:
        """What ``pipeline._classify`` returns: the classification, each
        product's hero, the crops and the search, here already over."""
        search = None
        if self.found is not None:
            search = asyncio.get_running_loop().create_future()
            search.set_result(self.found)
        chosen = outfit.choose_garment_images(self.classification)
        return self.classification, chosen, self.crops, search


def _product_parts(
    product_images: Sequence[bytes],
    look: bytes | None,
    complete_look: bool,
    upscale_crops: bool,
) -> dict:
    """The key parts of an analysis (hashes every photo: call in a
    thread)."""
    models = get_models()
    return {
        "products": [cache.digest(p) for p in product_images],
        "look": cache.digest(look) if look else "none",
        "complete_look": complete_look,
        "upscale_crops": upscale_crops,
        "models": [models.judge, models.image],
    }


def _inputs(req: TryOnRequest) -> list[bytes | None]:
    """The request's photos an entry names by position."""
    return [*req.product_images, req.reference_model_image]


def _start_saving(
    product: cache.Draft | None,
    plan: cache.Draft | None = None,
    entry: str | None = None,
) -> None:
    """Write the drafts in the background, the plan last: it names the
    analysis entry (``entry``, the one read, else ``product`` once
    stored)."""
    if not any((product, plan)):
        return

    def write() -> None:
        stored = entry
        if product is not None:
            stored = cache.save(product)
        if plan is not None and stored:
            cache.save(plan, analysis=stored)

    cache.in_background(write)


class TryOnCache:
    """One run's use of the cache; with ``GENMEDIA_USE_CACHE`` off each
    step runs as it is and nothing is read or written."""

    def __init__(self, req: TryOnRequest):
        """For ``req`` as received (its photos not trimmed yet)."""
        self.req = req
        self.on = get_settings().use_cache
        self.refresh = cache.refresh_requested()
        self.hit: Analysis | None = None
        self._entry: str | None = None  # the hit's entry id
        self._key = ""
        self._replace = self.refresh
        self._plan: cache.Draft | None = None

    def _lookup(self) -> Lookup:
        r = self.req
        parts = _product_parts(
            r.product_images,
            r.reference_model_image,
            r.complete_look,
            r.upscale_crops,
        )
        closeups = get_settings().tryon_closeups
        self._key = cache.key(kind="tryon", closeups=closeups, **parts)
        if self.refresh:
            return Lookup()
        return cache.load(PRODUCT_CACHE, self._key, ANALYSIS)

    async def analysis(
        self, trimming: Awaitable[TryOnRequest]
    ) -> tuple[TryOnRequest, Analysis | None]:
        """Read the analysis while ``trimming`` makes the trimmed request;
        that request and the analysis rebuilt around its photos (None on a
        miss)."""
        if not self.on:
            return await trimming, None
        lookup, req = await asyncio.gather(
            asyncio.to_thread(self._lookup), trimming
        )
        self.hit = lookup.decode(Analysis, _inputs(req), _ANALYSIS_TYPES)
        self._replace = self.refresh or lookup.stale
        if self.hit is not None and lookup.index is not None:
            self._entry = lookup.index["entry"]
        return req, self.hit

    async def plan(
        self,
        guidelines: str,
        compute: Callable[[], Awaitable[CompositionPlan]],
    ) -> CompositionPlan:
        """The styling plan for ``guidelines``: kept with the analysis
        this run read, or ``compute``d and kept for later when it took no
        fallback."""
        name = f"plan-{cache.digest(guidelines.encode())}"
        replace = self._replace
        if self.hit is not None and not self.refresh:
            lookup = await asyncio.to_thread(
                cache.load, PRODUCT_CACHE, self._key, name
            )
            if lookup.index and lookup.index.get("analysis") != self._entry:
                lookup = Lookup(stale=True)  # made for an older analysis
            kept = lookup.decode(CompositionPlan)
            if kept is not None:
                return kept
            replace = lookup.stale
        with degraded.tracking() as marker:
            made = await compute()
        if self.on and marker.ok:
            self._plan = cache.draft(
                PRODUCT_CACHE, self._key, name, made, replace=replace
            )
        return made

    def save(
        self,
        req: TryOnRequest,
        classification: Classification,
        crops: dict[str, bytes],
        search: asyncio.Future | None,
        marker: degraded.Marker,
    ) -> None:
        """Start writing what this run computed with no fallback, once its
        preprocessing is over: ``req`` trimmed, the analysis (``search``
        over) and its fallbacks (``marker``). Never raises (logged)."""
        if not self.on:
            return
        try:
            product = None
            if self.hit is None and marker.ok:
                found = None if search is None else search.result()
                product = cache.draft(
                    PRODUCT_CACHE,
                    self._key,
                    ANALYSIS,
                    Analysis(classification, crops, found),
                    inputs=_inputs(req),
                    replace=self._replace,
                )
            elif not marker.ok:
                logger.info(f"try-on cache: not kept: {marker.reasons}")
            _start_saving(product, self._plan, self._entry)
        except Exception as exc:  # the cache never fails a run
            logger.warning(f"try-on cache: not saved: {exc!r}")


def _classify_key(*args) -> str:
    return cache.key(kind="classify", **_product_parts(*args))


async def classify(
    product_images: Sequence[bytes],
    reference_model_image: bytes | None,
    complete_look: bool,
    *,
    upscale_crops: bool = False,
) -> tuple[Classification, dict[str, bytes]]:
    """``reference.classify`` through the cache (entries of their own:
    these photos are not trimmed and get no close-up search)."""
    args = (product_images, reference_model_image, complete_look)
    if not get_settings().use_cache:
        return await reference.classify(*args, upscale_crops=upscale_crops)
    refresh = cache.refresh_requested()
    key = await asyncio.to_thread(_classify_key, *args, upscale_crops)
    lookup = Lookup()
    if not refresh:
        lookup = await asyncio.to_thread(
            cache.load, PRODUCT_CACHE, key, ANALYSIS
        )
    inputs = [*product_images, reference_model_image]
    kept = lookup.decode(Analysis, inputs, _ANALYSIS_TYPES)
    if kept is not None:
        return kept.classification, kept.crops
    with degraded.tracking() as marker:
        made = await reference.classify(*args, upscale_crops=upscale_crops)
    if not marker.ok:
        logger.info(f"try-on cache: not kept: {marker.reasons}")
        return made
    try:
        value, replace = Analysis(*made, found=None), refresh or lookup.stale
        draft = cache.draft(
            PRODUCT_CACHE, key, ANALYSIS, value, inputs=inputs, replace=replace
        )
        _start_saving(draft)
    except Exception as exc:  # the cache never fails a run
        logger.warning(f"try-on cache: not saved: {exc!r}")
    return made
