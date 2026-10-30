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

"""One animated try-on image: its back view, plan and videos.

``video_stage`` animates one image: its references, the plan, the zoomed
details of the plan's close-ups, then ``Animation.videos`` videos, each
checked by ``evaluate.evaluate_clip``. ``animate`` is one branch of an
end-to-end run: the back view drawn from its front first (a single product
with a back upload), then ``progress("image_selected")`` and the video
stage. ``Streams`` runs the image stage and the branches at once and merges
their events; ``finish`` ranks every video of the run.
"""

import asyncio
import contextlib
import dataclasses
import itertools
import logging
from collections.abc import AsyncGenerator, Iterator
from dataclasses import dataclass, field
from typing import Any

from genmedia4commerce.core import gen, media, trace
from genmedia4commerce.core.errors import user_message
from genmedia4commerce.core.events import (
    CompleteEvent,
    ErrorEvent,
    Event,
    ProgressEvent,
    VariationEvent,
)
from genmedia4commerce.core.gen import VideoParams
from genmedia4commerce.core.runner import (
    RunPolicy,
    default_rank_key,
    run_variations,
)
from genmedia4commerce.core.settings import get_settings
from genmedia4commerce.core.types import Framing, Verdict
from genmedia4commerce.workflows.tryon import pipeline as tryon_pipeline
from genmedia4commerce.workflows.tryon.types import Prepared
from genmedia4commerce.workflows.video_tryon.evaluate import evaluate_clip
from genmedia4commerce.workflows.video_tryon.plan import plan_animation
from genmedia4commerce.workflows.video_tryon.prompt import render
from genmedia4commerce.workflows.video_tryon.refs import (
    add_details,
    build_references,
    recorded_details,
)
from genmedia4commerce.workflows.video_tryon.types import (
    Plan,
    VideoTryOnRequest,
)

__all__ = [
    "NO_VIDEO",
    "Animation",
    "Streams",
    "animate",
    "animate_only",
    "finish",
    "video_stage",
]

logger = logging.getLogger(__name__)

# Back images per animated image (the photo try-on draws num_variations):
# up to 3 branches draw their back views at once.
BACK_VIEWS = 1

NO_VIDEO = "No video was produced"


@dataclass
class Animation:
    """What every animated image of a run shares.

    ``req``: the request, with the product photos the try-on used;
    ``framing``: of the try-on images; ``ahead``: the detail search started
    during the image stage (crops, not upscaled); ``backs``: what draws the
    back views (None, or no back upload: none). The counters number the
    back views and the videos across the run.
    """

    req: VideoTryOnRequest
    params: VideoParams
    policy: RunPolicy
    framing: Framing | None = None
    ahead: asyncio.Task | None = None
    backs: Prepared | None = None
    back_ids: Iterator[int] = field(default_factory=itertools.count)
    video_ids: Iterator[int] = field(default_factory=itertools.count)

    @property
    def videos(self) -> int:
        """The videos of each animated image: the request's
        ``videos_per_image``, else ``settings.videos_per_image``."""
        return self.req.videos_per_image or get_settings().videos_per_image


class Streams:
    """Event streams run at once, their events merged as they come.

    Each stream is drained by its own task; ``add`` also works while the
    events are read. ``running`` counts the streams not yet ended.
    """

    def __init__(self) -> None:
        """Start with no stream."""
        self._queue: asyncio.Queue = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        self.running = 0

    def add(self, stream: AsyncGenerator[Event, None], key: Any = None) -> None:
        """Start draining ``stream``; its events come with ``key``."""
        self.running += 1
        self._tasks.append(asyncio.create_task(self._drain(stream, key)))

    async def _drain(
        self, stream: AsyncGenerator[Event, None], key: Any
    ) -> None:
        try:
            async with contextlib.aclosing(stream):
                async for event in stream:
                    self._queue.put_nowait((key, event))
        except Exception:
            logger.exception(f"video try-on: stream {key} failed")
        finally:
            self._queue.put_nowait((key, None))

    async def get(self) -> tuple[Any, Event | None]:
        """The next event and its stream's key; (key, None) once that
        stream has ended."""
        key, event = await self._queue.get()
        if event is None:
            self.running -= 1
        return key, event

    async def close(self) -> None:
        """Cancel the streams still running and wait until they stop."""
        pending = [task for task in self._tasks if not task.done()]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


def _tag(source: int | None) -> dict:
    """A video's ``extra``: its stage and, end to end, its try-on image."""
    return {"stage": "video"} | (
        {} if source is None else {"source_index": source}
    )


def _plan_event(
    plan: Plan, references: int, source: int | None, videos: int
) -> ProgressEvent:
    of = "" if source is None else f" of try-on variation {source + 1}"
    message = f"Animation{of}: {plan.summary()}"
    if plan.note:
        message += f". {plan.note}"
    shots = [dataclasses.asdict(shot) for shot in plan.shots]
    data = {
        "shots": shots,
        "note": plan.note,
        "references": references,
        "videos": videos,
    }
    if source is not None:
        data["source_index"] = source
    return ProgressEvent(stage="plan", message=message, data=data)


async def video_stage(
    req: VideoTryOnRequest, anim: Animation, source: int | None = None
) -> AsyncGenerator[Event, None]:
    """References from ``req.tryon_image``, the plan, then the videos,
    numbered across the run (``source``: the try-on image's index, end to
    end); one error event when preparing fails. No complete event.

    The details of the plan's close-ups are upscaled once the plan is known
    (``add_details``), from ``anim.ahead`` when the search ran ahead, else
    from the try-on's run record (``recorded_details``).
    """
    try:
        reference_face = await asyncio.to_thread(
            media.crop_face, req.animated_image
        )
        refs = await asyncio.to_thread(
            build_references, req, face=reference_face, framing=anim.framing
        )
        request = req.prompt if req.motion == "custom" else None
        plan = await asyncio.to_thread(plan_animation, refs, request)
        ahead = anim.ahead  # shared: one branch stopping must not cancel it
        if ahead is not None:
            found = await asyncio.shield(ahead)
        else:
            found = recorded_details(req)
        refs = await asyncio.to_thread(add_details, refs, plan, found)
    except Exception as exc:
        logger.exception("video try-on: preparing the animation failed")
        yield ErrorEvent(
            message=user_message(exc),
            retryable=getattr(exc, "retryable", False),
        )
        return
    prompt = render(plan, refs)
    count = anim.videos
    planned = _plan_event(plan, len(refs.images), source, count)
    trace.save(
        "plan", {**planned.data, "prompt": prompt, "changes": plan.changes}
    )
    yield planned
    params = dataclasses.replace(anim.params, duration=f"{plan.duration}s")

    async def make(_index: int) -> bytes:
        return await asyncio.to_thread(
            gen.generate_video, refs.images, prompt, params=params
        )

    async def evaluate(clip: bytes) -> tuple[Verdict, bytes]:
        verdict = await asyncio.to_thread(
            evaluate_clip, clip, req, reference_face, plan, refs
        )
        return verdict, clip

    policy = anim.policy.replace(num_variations=count)
    variations = trace.scoped(
        run_variations(policy, make, evaluate), stage="video"
    )
    async with contextlib.aclosing(variations):
        async for event in variations:
            if isinstance(event, VariationEvent):  # ``finish`` ranks them
                event.index = next(anim.video_ids)
                event.extra = {**event.extra, **_tag(source)}
                yield event


async def _back_view(
    anim: Animation, front: VariationEvent
) -> AsyncGenerator[VariationEvent, None]:
    """The back variations drawn from ``front``, numbered across the run,
    with its index as ``source_index``; drawing stops at the first error."""
    ids: dict[int, int] = {}
    backs, image = anim.backs, front.media
    if backs is None or backs.policy is None or image is None:
        logger.warning("video try-on: no back view to draw, front only")
        return
    one = dataclasses.replace(  # several branches draw at once: 1 back each
        backs, policy=backs.policy.replace(num_variations=BACK_VIEWS)
    )
    drawn = trace.scoped(tryon_pipeline.back_view(one, image), stage="image")
    try:
        async with contextlib.aclosing(drawn):
            async for event in drawn:
                local = event.index
                event.index = ids[local] = next(anim.back_ids)
                extra = {**event.extra, "stage": "image"}
                extra["source_index"] = front.index
                if "ranked" in extra:  # the back ranking, on the last one
                    extra["ranked"] = [ids[i] for i in extra["ranked"]]
                    extra["best_index"] = ids.get(extra["best_index"])
                event.extra = extra
                yield event
    except Exception as exc:
        logger.warning(f"video try-on: back view failed, front only: {exc}")


def _error_videos(
    anim: Animation, source: int, reason: str
) -> list[VariationEvent]:
    """The videos of an image whose animation could not be prepared."""
    return [
        VariationEvent(
            next(anim.video_ids),
            "error",
            None,
            None,
            0.0,
            error=f"Preparing the animation failed: {reason}",
            extra=_tag(source),
        )
        for _ in range(anim.videos)
    ]


async def animate(
    anim: Animation, front: VariationEvent, passed: bool
) -> AsyncGenerator[Event, None]:
    """One branch: the back view of ``front`` when one can be drawn, then
    ``progress("image_selected")`` and its videos. ``passed``: ``front``
    passed its checks. A failed preparation reports its videos as errors.
    """
    source, backs = front.index, []
    if anim.backs is not None and anim.backs.has_back:
        yield ProgressEvent(
            "back",
            f"Generating the back view of try-on variation {source + 1}",
            data={"source_index": source},
        )
        async for drawn in _back_view(anim, front):
            backs.append(drawn)
            yield drawn
    best = backs[-1].extra.get("best_index") if backs else None
    back = next((v for v in backs if v.index == best), None)
    trace.save("selected-front", front.media)
    trace.save("selected-back", back.media if back else None)
    yield ProgressEvent(
        stage="image_selected",
        message=f"Animating try-on variation {source + 1}"
        + ("" if passed else " (none passed every check)"),
        data={
            "front_index": source,
            "back_index": back.index if back else None,
        },
    )
    req = dataclasses.replace(
        anim.req,
        tryon_image=front.media,
        back_image=back.media if back else None,
    )
    stage = video_stage(req, anim, source)
    async with contextlib.aclosing(stage):
        async for event in stage:
            if isinstance(event, ErrorEvent):
                for video in _error_videos(anim, source, event.message):
                    yield video
                return
            yield event


def finish(videos: list[VariationEvent]) -> Event:
    """The run's last event: every video ranked (``runner``'s order), or an
    error when no video was made at all."""
    if not any(v.media for v in videos):
        reason = next((v.error for v in videos if v.error), None)
        message = f"{NO_VIDEO}: {reason}" if reason else NO_VIDEO
        return ErrorEvent(message=message, retryable=True)
    ranked = sorted(videos, key=default_rank_key)
    return CompleteEvent(
        ranked_indices=[v.index for v in ranked],
        best_index=next((v.index for v in ranked if v.status == "ready"), None),
    )


async def animate_only(anim: Animation) -> AsyncGenerator[Event, None]:
    """Animate ``anim.req.tryon_image`` (no image stage): the video stage,
    then ``finish``; a failed preparation is the run's error."""
    videos: list[VariationEvent] = []
    stage = video_stage(anim.req, anim)
    async with contextlib.aclosing(stage):
        async for event in stage:
            if isinstance(event, VariationEvent):
                videos.append(event)
            yield event
            if isinstance(event, ErrorEvent):
                return
    yield finish(videos)
