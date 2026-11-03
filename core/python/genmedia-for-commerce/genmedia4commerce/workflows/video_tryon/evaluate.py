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

"""The checks of one try-on video, all started at once:

1. Products (``checks``): one judge call per still frame and close-up,
   each with every product of the look, each within ``checks.JUDGE_CAP_S``.
2. The whole clip (``eval.clip_check``): one call over about one frame per
   second, the try-on images the video model was given and the face crop:
   a person who is not the face crop's rejects ("face: <reason>"; no face
   crop, no identity question), and so does a frame showing what no
   reference shows.

A rejection by 2 returns at once, without waiting for the judges. A failed
or late whole-clip check (``clip_check.DEADLINE_S``) is skipped (a flaky
call must not reject a good video) and the verdict says so.
"""

import logging
import time
from concurrent.futures import Future, wait

from genmedia4commerce.core import video
from genmedia4commerce.core.errors import GenMediaError, user_message
from genmedia4commerce.core.retry import ContextPool
from genmedia4commerce.core.types import Verdict, quality_score
from genmedia4commerce.eval import clip_check
from genmedia4commerce.workflows.video_tryon import checks
from genmedia4commerce.workflows.video_tryon.types import (
    Plan,
    ReferenceSet,
    VideoTryOnRequest,
)

__all__ = ["evaluate_clip"]

logger = logging.getLogger(__name__)

SKIPPED = "; whole-clip check skipped (failed)"


def _verdict(
    publish: bool,
    imperfections: int,
    description: str,
    details: dict,
    *,
    error: bool = False,
) -> Verdict:
    """A verdict with its score (``details["score"]``)."""
    score = quality_score(publish, imperfections)
    return Verdict(
        publish, imperfections, description, error, {**details, "score": score}
    )


def _whole_clip(
    frames: list[tuple[float, bytes]],
    req: VideoTryOnRequest,
    face: bytes | None,
) -> dict:
    """``details["clip_check"]``: the frames' seconds and the answer of
    ``clip_check.check_clip``, or ``skipped`` with why when it failed."""
    found: dict = {"seconds": [round(s, 1) for s, _ in frames]}
    try:
        answer = clip_check.check_clip(
            frames, req.animated_image, req.back_image, face
        )
    except GenMediaError as exc:
        logger.warning(f"whole-clip check failed, skipped: {exc}")
        return {**found, "skipped": user_message(exc)}
    return {**found, **answer}


def _clip_result(whole: Future, started: float) -> dict:
    """The whole-clip check's details, waited for until
    ``clip_check.DEADLINE_S`` after ``started``; late, it is skipped."""
    left = started + clip_check.DEADLINE_S - time.monotonic()
    if wait([whole], timeout=max(0.0, left)).done:
        return whole.result()
    logger.warning("whole-clip check late, skipped")
    return {"skipped": "no answer in time"}


def _rejected(found: dict) -> Verdict | None:
    """The rejection of the whole-clip check, if any."""
    if found.get("same_person") is False:
        text = f"face: {found['face_reason']}"
    elif found.get("invented_frames"):
        text = f"shows what no reference shows: {found['invented_what']}"
    else:
        return None
    return _verdict(False, 0, text, {"clip_check": found})


def evaluate_clip(
    clip: bytes,
    req: VideoTryOnRequest,
    reference_face: bytes | None,
    plan: Plan,
    refs: ReferenceSet,
) -> Verdict:
    """Run the checks of the module doc on one video."""
    stable = video.pick_stable_frames(clip)
    frames = checks.pick_frames(clip, stable, plan)
    if not frames:  # nothing decoded: never a pass
        text = "eval incomplete: no frames decoded from the video"
        return _verdict(False, 0, text, {}, error=True)
    items = checks.products(req, refs)
    sampled = clip_check.pick_frames(clip)
    pool = ContextPool(max_workers=1 + len(frames))
    try:
        started = time.monotonic()
        whole = pool.submit(_whole_clip, sampled, req, reference_face)
        pending = checks.start_judges(pool, frames, items)
        found = _clip_result(whole, started)
        if rejected := _rejected(found):
            return rejected
        results = checks.collect(pending, started)
    finally:
        # Late calls are dropped; the runner's cancel stops their retries.
        pool.shutdown(wait=False, cancel_futures=True)
    publish, imperfections, text, error = checks.summarize(results)
    text += SKIPPED if "skipped" in found else ""
    details = {
        "clip_check": found,
        "frames": checks.frame_details(frames),
        "checks": results,
    }
    return _verdict(publish, imperfections, text, details, error=error)
