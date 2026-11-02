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

"""Video Try-On: dress a person (image try-on), then animate the result.

End to end (no ``tryon_image``): every ``run_tryon`` event except its
``complete`` (variations tagged ``extra.stage="image"``). Each front that
passes its checks is animated at once, in its own branch (``branch.animate``)
while the other images are still being made: for a single product with a
back upload its own back view first (``progress("back")``, then variations
``extra.view="back"`` with ``extra.source_index``), then
``progress("image_selected")`` with ``data={"front_index", "back_index"}``.
With no front passing, the best-scoring one is animated
(" (none passed every check)"). The try-on image follows the video's frame:
9:16 for a 9:16 video (unless ``req.image`` sets the image params), else
3:4. Each animated image then streams ``progress("plan")`` (the animation:
``plan.plan_animation``, 1 to 3 shots, 8 to 10 seconds, in ``data={"shots",
"note", "references", "videos", "source_index"}``) and
``settings.videos_per_image`` video variations (mp4, ``extra.stage="video"``
and ``extra.source_index``), numbered across the run and checked by
``evaluate.evaluate_clip``; an image whose animation cannot be prepared
reports its videos as errors. One ``complete`` ranks every video; with no
video made at all, or no image, the run ends with one ``error`` instead.
Each product a plan shows in a close-up gets a zoomed detail
(``refs.add_details``: searched during the image stage, upscaled after the
plan). ``num_variations`` (the run policy) is the number of try-on images
here. With a ``tryon_image`` only that image is animated, into
``req.videos_per_image`` videos (the request's ``num_variations``; else
``settings.videos_per_image``), with no ``source_index``; a failed
preparation is the run's error. Its products come from the try-on's run
record (``workflows.tryon.record``: the chosen photos, the classification
and the close-ups found, no new search) or, as photos, are classified
first like the try-on's (a ``classified`` event) and their details
searched while the animation is prepared. With no product photo the video
gets no product reference and the outfit is judged against the image.
"""

import asyncio
import contextlib
import dataclasses
import logging
from collections.abc import AsyncGenerator, Sequence
from typing import Any, TypeGuard, get_args

from genmedia4commerce.core import gen, trace
from genmedia4commerce.core.errors import InputError
from genmedia4commerce.core.events import (
    ClassifiedEvent,
    CompleteEvent,
    ErrorEvent,
    Event,
    VariationEvent,
)
from genmedia4commerce.core.gen import ImageParams, VideoParams
from genmedia4commerce.core.runner import RunPolicy
from genmedia4commerce.core.types import Classification
from genmedia4commerce.workflows.tryon import cache, outfit
from genmedia4commerce.workflows.tryon import pipeline as tryon_pipeline
from genmedia4commerce.workflows.tryon.types import Prepared, TryOnRequest
from genmedia4commerce.workflows.video_tryon.branch import (
    Animation,
    Streams,
    animate,
    animate_only,
    finish,
)
from genmedia4commerce.workflows.video_tryon.refs import (
    find_details,
    product_photos,
    search_names,
)
from genmedia4commerce.workflows.video_tryon.types import (
    Motion,
    VideoTryOnRequest,
)

__all__ = ["run_video_tryon", "stage_one_image"]

logger = logging.getLogger(__name__)

NO_IMAGE = "Image try-on produced no image"
IMAGE = "image"  # the image stage's key among the merged streams


def _validate(req: VideoTryOnRequest, params: VideoParams) -> None:
    if req.tryon_image is None:
        n, most = len(req.product_images), tryon_pipeline.MAX_PRODUCT_IMAGES
        if not req.person_image:
            raise InputError("a tryon_image or a person_image is required")
        if not 1 <= n <= most or not all(req.product_images):
            raise InputError(f"1 to {most} product images required, got {n}")
    elif not req.tryon_image:
        raise InputError("tryon_image must not be empty")
    if req.motion not in get_args(Motion):
        raise InputError(f"motion must be one of {get_args(Motion)}")
    if req.motion == "custom" and not (req.prompt or "").strip():
        raise InputError("motion 'custom' needs a prompt")
    if params.aspect_ratio not in gen.VIDEO_ASPECT_RATIOS:
        raise InputError(
            f"aspect_ratio must be one of {gen.VIDEO_ASPECT_RATIOS}"
        )


def stage_one_image(video: VideoParams, **params: Any) -> ImageParams:
    """The try-on image params for ``video``; ``params`` set the others.

    A 9:16 video gets a 9:16 try-on image, which fills its frame. A 16:9
    video keeps the 3:4 default: a 16:9 person image is not a try-on.
    """
    if video.aspect_ratio == "9:16":
        params.setdefault("aspect_ratio", "9:16")
    return ImageParams(**params)


def _tryon_request(
    req: VideoTryOnRequest, policy: RunPolicy, video: VideoParams
) -> TryOnRequest:
    """Stage 1, fronts only: each animated front gets its own back view
    (``branch.animate``), so the animation can turn to show it."""
    if req.person_image is None:  # ``_validate`` requires it
        raise InputError("a tryon_image or a person_image is required")
    return TryOnRequest(
        product_images=req.product_images,
        person_image=req.person_image,
        reference_model_image=req.reference_model_image,
        complete_look=req.complete_look,
        upscale_crops=req.upscale_crops,
        style_guidelines=req.style_guidelines,
        scenario=req.scenario,
        generate_back=False,
        policy=policy,
        image=req.image or stage_one_image(video),
    )


def _used_products(
    found: Classification | None, req: VideoTryOnRequest
) -> tuple[list[bytes], Classification | None]:
    """The best upload of each product of ``found`` (``outfit.best_upload``:
    sharp, never a look-photo crop, which only a product the look adds has)
    and the classification naming them; the request's without one."""
    photos = (
        [outfit.best_upload(p).image for p in found.products] if found else []
    )
    if not photos:
        return req.product_images, req.classification
    return photos, found


def _search_ahead(used: VideoTryOnRequest) -> asyncio.Task:
    """Start the detail search of every product photo the video gets (the
    accessories and shoes the try-on uses: ``refs.product_photos``), each
    told its product (``refs.search_names``), in the background while the
    image stage goes on; every branch shares it.
    It crops but does not upscale (image-model calls here took the try-on's
    quota: 429); each plan later picks its close-ups, only their details are
    upscaled (``add_details``) and the rest are dropped."""

    def search():
        try:
            return find_details(product_photos(used), search_names(used))
        except Exception as exc:  # the plan's close-ups are searched then
            logger.warning(f"video try-on: detail search ahead failed: {exc}")
            return None

    return asyncio.create_task(asyncio.to_thread(search))


def _classified(anim: Animation, event: ClassifiedEvent) -> None:
    """Take the framing and the products the try-on used from stage 1."""
    photos, classification = _used_products(event.classification, anim.req)
    anim.req = dataclasses.replace(
        anim.req, product_images=photos, classification=classification
    )
    anim.framing = event.payload.get("framing")
    if anim.ahead is None:
        anim.ahead = _search_ahead(anim.req)


async def _classify(req: VideoTryOnRequest) -> VideoTryOnRequest | None:
    """``req`` with its product photos classified as the image try-on does
    (``reference.classify``, through the preprocessing cache:
    ``tryon.cache.classify``) and replaced by the photo chosen for each
    product; None when they are classified already, there are none, or the
    classifier failed (every photo then goes, unclassified)."""
    if not req.product_images or req.classification is not None:
        return None
    try:
        found, _ = await cache.classify(
            req.product_images,
            req.reference_model_image,
            req.complete_look,
            upscale_crops=req.upscale_crops,
        )
    except Exception as exc:
        logger.warning(f"video try-on: classifying the products failed: {exc}")
        return None
    photos, classification = _used_products(found, req)
    if classification is None:
        return None
    return dataclasses.replace(
        req, product_images=photos, classification=classification
    )


async def _animate_image(
    req: VideoTryOnRequest, params: VideoParams, policy: RunPolicy
) -> AsyncGenerator[Event, None]:
    """Animate ``req.tryon_image`` (``branch.animate_only``): unclassified
    product photos are classified first (a ``classified`` event) and,
    unless the try-on's run record brought them, their details are searched
    while the animation is prepared."""
    classified = await _classify(req)
    if classified is not None and classified.classification is not None:
        req, c = classified, classified.classification
        yield ClassifiedEvent(payload=c.to_dict(), classification=c)
    framing = req.classification.framing if req.classification else None
    anim = Animation(req, params, policy, framing)
    if req.closeups is None and req.product_images:
        anim.ahead = _search_ahead(req)
    stage = animate_only(anim)
    try:
        async with contextlib.aclosing(stage):
            async for event in stage:
                yield event
    finally:
        if anim.ahead is not None:
            anim.ahead.cancel()  # no-op once used


def _best_scoring(fronts: Sequence[VariationEvent]) -> VariationEvent | None:
    """With no front passing, the best-scoring one with an image is
    animated anyway; None when no image was made at all."""
    made = [v for v in fronts if v.media is not None]
    return max(made, key=lambda v: v.score, default=None)


def _start(
    streams: Streams, anim: Animation, front: VariationEvent, passed: bool
) -> None:
    """Animate ``front`` in its own branch, traced with its index."""
    branch = trace.scoped(animate(anim, front, passed), source=front.index)
    streams.add(branch, front.index)


def _image_event(
    event: Event, anim: Animation, fronts: list[VariationEvent]
) -> TypeGuard[VariationEvent]:
    """Handle one stage-1 event to forward; True when it is a front that
    passed (to animate)."""
    if isinstance(event, ClassifiedEvent):
        _classified(anim, event)
    if not isinstance(event, VariationEvent):
        return False
    event.extra = {**event.extra, "stage": "image"}
    fronts.append(event)
    return event.status == "ready" and event.media is not None


async def _end_to_end(
    req: VideoTryOnRequest,
    params: VideoParams,
    policy: RunPolicy,
) -> AsyncGenerator[Event, None]:
    """Image try-on; each front that passes is animated at once (the best
    one when none does); then every video ranked."""
    prepared = Prepared()
    anim = Animation(req, params, policy, backs=prepared)
    stage = tryon_pipeline.run_tryon(
        _tryon_request(req, policy, params), prepared
    )
    fronts: list[VariationEvent] = []
    videos: list[VariationEvent] = []
    streams, started = Streams(), False
    streams.add(stage, IMAGE)
    try:
        while streams.running:
            key, event = await streams.get()
            if key != IMAGE:  # a branch
                if event is not None:
                    if _is_video(event):
                        videos.append(event)
                    yield event
                continue
            if event is None:  # stage 1 ended
                if not started:
                    front = _best_scoring(fronts)
                    if front is None:
                        yield ErrorEvent(message=NO_IMAGE, retryable=True)
                        return
                    _start(streams, anim, front, passed=False)
                continue
            if isinstance(event, CompleteEvent):
                continue
            if _image_event(event, anim, fronts):
                _start(streams, anim, event, passed=True)
                started = True
            yield event
            if isinstance(event, ErrorEvent):
                return
        yield finish(videos)
    finally:
        await streams.close()
        if anim.ahead is not None:
            anim.ahead.cancel()  # no-op once used; else not needed


def _is_video(event: Event) -> TypeGuard[VariationEvent]:
    return (
        isinstance(event, VariationEvent)
        and event.extra.get("stage") == "video"
    )


async def run_video_tryon(
    req: VideoTryOnRequest,
) -> AsyncGenerator[Event, None]:
    """Run one Video Try-On and stream its events (see the module doc).

    Raises ``InputError`` on invalid input before the first event.
    """
    params = req.video or VideoParams(aspect_ratio="9:16")
    _validate(req, params)
    policy = req.policy or RunPolicy.from_settings()
    if req.tryon_image is None:
        stage = _end_to_end(req, params, policy)
    else:
        stage = _animate_image(req, params, policy)
    async with contextlib.aclosing(stage):
        async for event in stage:
            yield event
