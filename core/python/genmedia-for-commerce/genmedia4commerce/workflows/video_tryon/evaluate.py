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

"""The checks of one try-on video:

1. Identity: one judge call over a few stable frames spread over the clip
   (``IDENTITY_FRAMES``) and the try-on image's face crop
   (``eval.identity``); a person who is not the face crop's rejects
   ("face: <reason>"). No face crop: no identity check.
2. Products (``checks``): on the stillest frames and the close-ups, the
   products a frame shows are judged against their own photos in one call
   per frame (the image try-on's judge), each within ``checks.JUDGE_CAP_S``.
3. Blind spots: no frame may show what neither try-on image shows.

Identity and blind spots run alongside 2 and reject at once, without
waiting for the judges. A failed identity or blind-spot check is skipped (a
flaky call must not reject a good video) and the verdict says so.

The stable frames of the identity check also give the close-ups;
``video.stillest_frames`` decodes the clip again for the still frames.
"""

import logging
import time
from concurrent.futures import FIRST_COMPLETED, Future, wait

from genmedia4commerce.core import video
from genmedia4commerce.core.errors import GenMediaError, user_message
from genmedia4commerce.core.retry import ContextPool
from genmedia4commerce.core.types import Verdict, quality_score
from genmedia4commerce.eval import identity, unseen
from genmedia4commerce.workflows.video_tryon import checks
from genmedia4commerce.workflows.video_tryon.types import (
    Plan,
    ReferenceSet,
    VideoTryOnRequest,
)

__all__ = ["IDENTITY_FRAMES", "evaluate_clip"]

logger = logging.getLogger(__name__)

IDENTITY_FRAMES = 6  # stable frames the identity check sees, at most


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


def _spread(frames: list[tuple[int, bytes]]) -> list[tuple[int, bytes]]:
    """At most ``IDENTITY_FRAMES`` of ``frames``, evenly spaced from the
    first to the last."""
    n = IDENTITY_FRAMES
    if len(frames) <= n:
        return frames
    return [frames[round(k * (len(frames) - 1) / (n - 1))] for k in range(n)]


def _identity(
    stable: list[tuple[int, bytes]], reference_face: bytes | None
) -> dict | None:
    """``details["identity"]``: the frames the check saw, ``same_person``
    (None: the call failed, skipped) and the reason; None without a face
    crop or a frame."""
    if reference_face is None or not stable:
        return None
    picked = _spread(stable)
    found: dict = {"frames": [i for i, _ in picked]}
    try:
        same, reason = identity.same_person(
            [png for _, png in picked], reference_face
        )
    except GenMediaError as exc:
        logger.warning(f"identity check failed, skipped: {exc}")
        return {**found, "same_person": None, "reason": user_message(exc)}
    return {**found, "same_person": same, "reason": reason}


def _blind_spots(
    clip: bytes, req: VideoTryOnRequest
) -> tuple[list[int], str, bool]:
    """(frames showing what neither try-on image shows, what, skipped)."""
    seen = [req.animated_image, *([req.back_image] if req.back_image else [])]
    try:
        return (*unseen.unseen_frames(clip, seen), False)
    except GenMediaError as exc:
        logger.warning(f"blind-spot check failed, skipped: {exc}")
        return [], "", True


def _rejected(face: Future, blind: Future) -> Verdict | None:
    """The rejection of a finished identity or blind-spot check, if any."""
    found = face.result() if face.done() else None
    if found and found["same_person"] is False:
        text = f"face: {found['reason']}"
        return _verdict(False, 0, text, {"identity": found})
    if blind.done() and blind.result()[0]:
        frames, what, _ = blind.result()
        text = f"shows what no reference shows: {what}"
        details = {"identity": found, "unseen_frames": frames}
        return _verdict(False, 0, text, details)
    return None


def _skipped(found: dict | None, blind_skipped: bool) -> str:
    """The note of the checks that failed and were skipped."""
    text = "; blind-spot check skipped (failed)" if blind_skipped else ""
    if found and found["same_person"] is None:
        text += "; face check skipped (failed)"
    return text


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
    seen = {"front": req.animated_image}
    if req.back_image:
        seen["back"] = req.back_image
    checks.label_views(frames, seen)
    pool = ContextPool(max_workers=3 + len(frames) * len(items))
    try:
        face = pool.submit(_identity, stable, reference_face)
        blind = pool.submit(_blind_spots, clip, req)
        shown = pool.submit(checks.visible, frames, items)
        wait([face, blind, shown], return_when=FIRST_COMPLETED)
        if rejected := _rejected(face, blind):
            return rejected
        for f, ids in zip(frames, shown.result(), strict=True):
            f.visible = ids
        started = time.monotonic()
        pending = checks.start_judges(pool, checks.pairs(frames, items))
        wait([face, blind])
        if rejected := _rejected(face, blind):
            return rejected
        results = checks.collect(pending, started)
    finally:
        # Late judges are dropped; the runner's cancel stops their retries.
        pool.shutdown(wait=False, cancel_futures=True)
    publish, imperfections, text, error = checks.summarize(results)
    text += _skipped(face.result(), blind.result()[2])
    details = {
        "identity": face.result(),
        "unseen_frames": [],
        "frames": checks.frame_details(frames),
        "checks": results,
    }
    return _verdict(publish, imperfections, text, details, error=error)
