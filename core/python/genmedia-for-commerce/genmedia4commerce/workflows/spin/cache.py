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

"""The 360 spin's preprocessing cache (``core.cache``): what a run works
out from the product photos before it makes a video, kept for the next run
with the same photos.

One entry, ``spin_cache/<key>/prepared.json``. Key: the photos' digests in
their order, ``GENMEDIA_SPIN_CLOSEUPS``, the judge and image model ids and
the code fingerprint. Value (``Prepared``): the classification, the
reference views in generation order (shoes: labelled, cut apart, mirrored
by the image model; other products: cut out), the mirrored views and the
close-ups (found and upscaled). A reference that is a request photo as it
is is not stored but named by position. The entry is kept only when no
step took a fallback (``core.degraded``), in the background once the
references are built; nothing of the videos is kept. A refresh
(``core.cache.refreshing``, the Studio's "Try again") reads nothing and
replaces the entry. With ``GENMEDIA_USE_CACHE`` off nothing is read or
written.
"""

import asyncio
import logging
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass
from typing import TypeVar

from genmedia4commerce.core import cache, degraded
from genmedia4commerce.core.cache import SPIN_CACHE, Lookup
from genmedia4commerce.core.models import get_models
from genmedia4commerce.core.settings import get_settings
from genmedia4commerce.workflows.details import Detail
from genmedia4commerce.workflows.spin.types import SpinClassification

__all__ = ["Prepared", "SpinCache"]

logger = logging.getLogger(__name__)

PREPARED = "prepared"
T = TypeVar("T")


@dataclass
class Prepared:
    """What a run worked out from the product photos."""

    classification: SpinClassification
    refs: dict[str, bytes]  # view -> image, in generation order
    mirrored: list[str]  # the mirrored views (shoes)
    closeups: list[Detail]


_TYPES = (SpinClassification, Detail)


class SpinCache:
    """One run's use of the cache; with ``GENMEDIA_USE_CACHE`` off each
    step runs as it is and nothing is read or written."""

    def __init__(self, images: Sequence[bytes]):
        """For the request's product photos."""
        self.images = list(images)
        self.on = get_settings().use_cache
        self.refresh = cache.refresh_requested()
        self.marker = degraded.Marker()  # the fallbacks of ``tracked`` steps
        self.hit: Prepared | None = None
        self._key = ""
        self._replace = self.refresh

    def _lookup(self) -> Lookup:
        models = get_models()
        self._key = cache.key(
            kind="spin",
            photos=[cache.digest(p) for p in self.images],
            closeups=get_settings().spin_closeups,
            models=[models.judge, models.image],
        )
        if self.refresh:
            return Lookup()
        return cache.load(SPIN_CACHE, self._key, PREPARED)

    async def load(self) -> Prepared | None:
        """What an earlier run kept for these photos, rebuilt around this
        request's own photos; None on a miss."""
        if not self.on:
            return None
        lookup = await asyncio.to_thread(self._lookup)
        self.hit = lookup.decode(Prepared, self.images, _TYPES)
        self._replace = self.refresh or lookup.stale
        return self.hit

    async def tracked(self, step: Awaitable[T]) -> T:
        """``step`` awaited with its fallbacks recorded in ``marker``."""
        with degraded.tracking(self.marker):
            return await step

    def save(self, prepared: Prepared) -> None:
        """Start writing ``prepared`` when it was computed (not read) and
        no ``tracked`` step took a fallback. Never raises (logged)."""
        if not self.on or self.hit is not None:
            return
        if not self.marker.ok:
            logger.info(f"spin cache: not kept: {self.marker.reasons}")
            return
        try:
            draft = cache.draft(
                SPIN_CACHE,
                self._key,
                PREPARED,
                prepared,
                inputs=self.images,
                replace=self._replace,
            )
            cache.in_background(lambda: cache.save(draft))
        except Exception as exc:  # the cache never fails a run
            logger.warning(f"spin cache: not saved: {exc!r}")
