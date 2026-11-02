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

"""``run_spin``: classify -> references -> video variations -> validate.

Shoes: shoe reference set (``shoe_refs``: views labelled by the judge model,
pairs cut into single shoes, a shoe mirrored only when two photos show
opposite sides), one request with the tuned turntable prompt, then
``shoe_check``: the judge model labels the frames (direction, turn-backs,
jumps, full turn, loop cut) and the shoe judge compares the labelled frames
with the references (mirrored references never judged).
Any other product: cut-out references, two half turns of one stored
interaction (the first half is made again if it turns back; a rejected spin
is continued again from the same first half), then ``product_check``:
rotation check, frame labels and glitch call at once (a spin that turns
back or never comes back to its first view is rejected; an anticlockwise
one is reversed; the clip is cut where it closes), product judge.
"""

import asyncio
import contextlib
import dataclasses
import logging
from collections.abc import AsyncGenerator, Sequence

from genmedia4commerce.core import gen, media, trace
from genmedia4commerce.core import video as core_video
from genmedia4commerce.core.errors import InputError, user_message
from genmedia4commerce.core.events import (
    ClassifiedEvent,
    ErrorEvent,
    Event,
    ProgressEvent,
)
from genmedia4commerce.core.runner import RunPolicy, run_variations
from genmedia4commerce.core.settings import get_settings
from genmedia4commerce.core.types import Verdict, quality_score
from genmedia4commerce.eval import motion
from genmedia4commerce.workflows import details
from genmedia4commerce.workflows.spin import (
    classify,
    product_check,
    product_refs,
    shoe_check,
    shoe_refs,
)
from genmedia4commerce.workflows.spin.prompt import (
    TURN_DURATION,
    orbit_turns,
    shoe_prompt,
)
from genmedia4commerce.workflows.spin.types import (
    SpinClassification,
    SpinRequest,
)

__all__ = ["run_spin"]

logger = logging.getLogger(__name__)

MAX_IMAGES = 8


MAX_REFERENCES = 10  # images the video model takes in one request
MAX_CLOSEUPS = 2  # detail close-ups added after the views
AUTO = "auto"  # aspect ratio picked from the product's shape (_auto_aspect)
# Narrower than this times its height at its widest -> 9:16. A near-square
# bag (0.83) gained only 15-20% in 9:16 and published 0 of 6 vs 3 of 4.
TALL = 0.8
FIRST_HALF_TRIES = 2  # a first half that turns back is made again
# A rejected spin is continued again from its first half; with 2, that one
# spare continuation is started at once, while the spin is being checked.
CONTINUATIONS = 2


def _validate(images: Sequence[bytes]) -> None:
    if not 1 <= len(images) <= MAX_IMAGES:
        raise InputError(f"spin needs 1 to {MAX_IMAGES} product images")
    if not all(images):
        raise InputError("empty product image")


def _evaluate(
    clip: bytes, refs: dict[str, bytes], route: str
) -> tuple[Verdict, bytes]:
    """The route's checks: ``shoe_check`` (the judge model's view of each
    frame) or ``product_check`` (flow, frame labels and glitch call at once,
    then the product judge)."""
    if route == "shoes":
        return shoe_check.evaluate(clip, refs)
    return product_check.evaluate(clip, refs)


async def _references(
    req: SpinRequest, classification: SpinClassification
) -> tuple[dict[str, bytes], frozenset[str]]:
    """The reference views, and which of them are mirrored (shoes only)."""
    mirrored: frozenset[str] = frozenset()
    if classification.product_type == "shoes":
        built = await asyncio.to_thread(shoe_refs.build, req.product_images)
        refs, mirrored = built.images, built.mirrored
        if not refs:
            raise InputError("no usable shoe reference in the photos")
    else:
        refs = await asyncio.to_thread(
            product_refs.build,
            req.product_images,
            classification.views,
            classification.description,
        )
    for view, ref in refs.items():
        trace.save(f"ref-{view}", ref)
    return refs, mirrored


def _auto_aspect(refs: dict[str, bytes], *, shoes: bool) -> str:
    """9:16 for a product taller than wide at its widest, else 16:9.

    Omni works on a 1280x720 (or 720x1280) frame: the product's long side goes
    along the 1280 px side. The references are cut-outs at camera height;
    a shoe is measured on its right side view, else on its first side view
    (three-quarter or left; a front or top view says nothing of its length).
    """
    images = list(refs.values())
    if shoes:
        sides = [v for v in refs if v in shoe_refs.SIDES]
        side = "right" if "right" in refs else next(iter(sides), None)
        images = [refs[side]] if side else []
    if not images:
        return "16:9"
    sizes = [media.image_size(image) for image in images]
    widest = max(width / height for width, height in sizes)
    return "9:16" if widest < TALL else "16:9"


def _score(verdict: Verdict) -> float:
    return quality_score(verdict.publish, verdict.imperfection_count)


def _half_problem(half: bytes) -> str | None:
    """Why a first half turn is made again: it turns back.

    Only the local rotation check (about 3 s): a Gemini glitch check here cost
    20-40 s on every spin and failed 1 good-looking half in 16; the whole
    spin's checks catch glitches.
    """
    return "turns back" if motion.check_rotation(half) == "invalid" else None


def _first_half(images: list[bytes], prompt: str, params) -> bytes:
    """The first half turn (stored), made again while it fails a cheap check."""
    for attempt in range(1, FIRST_HALF_TRIES + 1):
        half = gen.generate_video(images, prompt, params=params, store=True)
        problem = _half_problem(half)
        if problem is None:
            break
        trace.save(
            "first-half-rejected", {"attempt": attempt, "reason": problem}
        )
        logger.info(f"first half turn rejected ({problem}), try {attempt}")
    return half  # after the last try the full checks decide


def _shoe_steps(
    refs: dict[str, bytes], params: gen.VideoParams, mirrored: frozenset[str]
):
    """One video from the tuned shoe prompt (naming the mirrored references),
    then the shoe route's checks (never judging a mirrored one)."""
    images = list(refs.values())
    flipped = [k for k, view in enumerate(refs, start=1) if view in mirrored]
    prompt = shoe_prompt(params.aspect_ratio, flipped)
    judged = {v: ref for v, ref in refs.items() if v not in mirrored}

    async def make(_: int) -> bytes:
        return await asyncio.to_thread(
            gen.generate_video, images, prompt, params=params
        )

    async def check(clip: bytes) -> tuple[Verdict, bytes]:
        return await asyncio.to_thread(_evaluate, clip, judged, "shoes")

    return make, check


async def _closeups(req: SpinRequest, shoes: bool):
    """Close-up references of the product's small details (setting
    ``GENMEDIA_SPIN_CLOSEUPS``); not for shoes yet. Looked for in the photos
    while the references are built (the caller keeps them within the video
    model's ``MAX_REFERENCES``)."""
    if shoes or not get_settings().spin_closeups:
        return []
    found = await asyncio.to_thread(
        details.find, req.product_images, limit=MAX_CLOSEUPS
    )
    for k, closeup in enumerate(found):
        trace.save(
            f"ref-closeup-{k}", closeup.image, meta={"where": closeup.where}
        )
    return found


def _product_steps(
    refs: dict[str, bytes], params, description: str, closeups: Sequence = ()
):
    """Two half turns of one interaction (prompt.orbit_turns).

    The first half's rotation is checked before it is continued. While the
    spin is checked, a spare continuation of the same first half (its first
    4 s stay as they are) is already being made; a rejected spin is judged
    again on it and the better one is kept (``CONTINUATIONS`` 1: no spare).
    """
    images = list(refs.values()) + [c.image for c in closeups]
    first, second = orbit_turns(description, list(refs), closeups)
    params = dataclasses.replace(params, duration=TURN_DURATION)

    def extend(half: bytes) -> bytes:
        start = getattr(half, "extends", None) or getattr(
            half, "interaction_id", None
        )
        return gen.generate_video(images, second, params=params, extends=start)

    async def make(_: int) -> bytes:
        half = await asyncio.to_thread(_first_half, images, first, params)
        return await asyncio.to_thread(extend, half)

    async def check(clip: bytes) -> tuple[Verdict, bytes]:
        spare = None
        if CONTINUATIONS > 1 and getattr(clip, "extends", None):
            spare = asyncio.create_task(asyncio.to_thread(extend, clip))
        try:
            verdict, out = await asyncio.to_thread(
                _evaluate, clip, refs, "other"
            )
            if spare is None or verdict.publish or verdict.error:
                return verdict, out
            again = await asyncio.to_thread(
                _evaluate, await spare, refs, "other"
            )
            return (
                again if _score(again[0]) > _score(verdict) else (verdict, out)
            )
        finally:
            if spare is not None and not spare.done():
                spare.cancel()  # its result is not needed

    return make, check


async def run_spin(req: SpinRequest) -> AsyncGenerator[Event, None]:
    """Generate 360 spin variations of a product; stream the events:
    ``classified`` {product_type, views, description}, ``progress``,
    ``variation`` (media = animated WebP) per finished variation, then
    ``complete``; or one ``error`` if the run cannot start."""
    try:
        _validate(req.product_images)
        classification = await asyncio.to_thread(
            classify.classify, req.product_images
        )
        yield ClassifiedEvent(payload=dataclasses.asdict(classification))
        shoes = classification.product_type == "shoes"
        finding = asyncio.create_task(_closeups(req, shoes))  # meanwhile
        try:
            refs, mirrored = await _references(req, classification)
        except BaseException:
            finding.cancel()
            raise
        params = req.video or gen.VideoParams(aspect_ratio=AUTO)
        picked = params.aspect_ratio == AUTO
        if picked:
            ratio = _auto_aspect(refs, shoes=shoes)
            params = dataclasses.replace(params, aspect_ratio=ratio)
        ratio = params.aspect_ratio + (" (auto)" if picked else "")
        closeups = (await finding)[: MAX_REFERENCES - len(refs)]
        extra = f" + {len(closeups)} close-ups" if closeups else ""
        yield ProgressEvent(
            "references",
            f"Using {len(refs)} reference views{extra} · {ratio}",
            data={
                "views": list(refs),
                "aspect_ratio": params.aspect_ratio,
                "closeups": [c.where for c in closeups],
            },
        )
        policy = req.policy or RunPolicy.from_settings()
        if shoes:
            make, check = _shoe_steps(refs, params, mirrored)
        else:
            make, check = _product_steps(
                refs, params, classification.description, closeups
            )

        async def evaluate(clip: bytes) -> tuple[Verdict, bytes]:
            verdict, clip = await check(clip)
            verdict.details["score"] = _score(verdict)
            # Every spin is an animated WebP, also one the checks kept as
            # the video model's MP4.
            return verdict, await asyncio.to_thread(core_video.to_webp, clip)

        yield ProgressEvent("generating", "Generating spin videos")
        variations = trace.scoped(
            run_variations(policy, make, evaluate), stage="video"
        )
        async with contextlib.aclosing(variations):
            async for event in variations:
                yield event
    except Exception as exc:
        logger.warning(f"spin failed: {exc}", exc_info=True)
        yield ErrorEvent(
            message=user_message(exc),
            retryable=getattr(exc, "retryable", False),
        )
