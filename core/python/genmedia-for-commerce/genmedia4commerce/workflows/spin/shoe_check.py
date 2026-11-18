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

"""Checks of a shoe spin, read from the judge model's view of each frame.

Frames at 2 per second and the last one go to the judge model in one call
(``shoe_refs.label_images`` in eval mode: a standing view or
``not_standing``). A clockwise turn steps right -> front_right -> front ->
front_left -> left -> back_left -> back -> back_right -> right. An
anticlockwise spin is reversed. A spin is rejected when it turns back, jumps
two views or more between two frames half a second apart (a clean spin never
steps more than one), shows the shoe not standing on its sole, or does not
come back to its first view. It is cut on the frame most like its first,
around the first sample back at the start. Then each judged view goes to the
shoe judge on the frame labelled with it.

On 47 labelled bad videos and 28 good ones, 73 of 75 were read right; the two
misses end 10-20 degrees short of the start, between two views.
"""

import logging
from dataclasses import dataclass, field
from itertools import pairwise

from genmedia4commerce.core import media, trace
from genmedia4commerce.core import video as core_video
from genmedia4commerce.core.types import Verdict
from genmedia4commerce.eval import video_fidelity
from genmedia4commerce.workflows.spin import shoe_refs
from genmedia4commerce.workflows.spin.shoe_prompts import (
    NOT_STANDING,
    STANDING_VIEWS,
)

__all__ = ["JUDGED_VIEWS", "Reading", "Turn", "check_turn", "evaluate"]

logger = logging.getLogger(__name__)

# An eye-level spin never shows the top; the left views are not judged.
JUDGED_VIEWS = ("right", "front_right", "back_right", "front", "back")
SAMPLE_FPS = 2
JUMP = 2  # views between two samples: a jump
MAX_UNCLEAR = 0.25  # share of samples allowed without a label
FULL_TURN = len(STANDING_VIEWS)  # 45-degree steps in one turn
# A rejected spin scores like a 2-defect fidelity reject (both unusable).
REJECT_IMPERFECTIONS = 2


@dataclass(frozen=True)
class Reading:
    """What the labels say: direction, steps turned, problems.

    ``back_at``: the sample where a full turn is first completed.
    """

    clockwise: bool
    steps: int
    back_at: int | None
    problems: list[str]


@dataclass
class Turn:
    """The clip (reversed, cut) and its labelled samples ``(index, PNG)``."""

    clip: bytes
    frames: list[tuple[int, bytes]]
    labels: list[str]
    problems: list[str]
    details: dict = field(default_factory=dict)


def _step(a: int, b: int) -> int:
    """+1..+3 clockwise, -1..-3 the other way, 4 half a turn."""
    d = (b - a) % FULL_TURN
    return d if d <= FULL_TURN // 2 else d - FULL_TURN


def _steps(labels: list[str], times: list[float]) -> list[tuple]:
    """(sample, time, step) between consecutive samples with a view."""
    views = [
        (k, times[k], STANDING_VIEWS.index(label))
        for k, label in enumerate(labels)
        if label in STANDING_VIEWS
    ]
    return [(k, t, _step(a, b)) for (_, _, a), (k, t, b) in pairwise(views)]


def _label_problems(labels: list[str], times: list[float]) -> list[str]:
    fallen = [
        t for t, x in zip(times, labels, strict=True) if x == NOT_STANDING
    ]
    unclear = sum(x not in (*STANDING_VIEWS, NOT_STANDING) for x in labels)
    problems = []
    if fallen:
        at = ", ".join(f"{t:.1f} s" for t in fallen)
        problems.append(f"the shoe is not standing on its sole at {at}")
    if len(fallen) + unclear == len(labels):
        problems.append("no frame shows the shoe's view")
    elif unclear > MAX_UNCLEAR * len(labels):
        problems.append(f"{unclear} of {len(labels)} frames have no view")
    return problems


def read_turn(labels: list[str], times: list[float]) -> Reading:
    """Direction, turn-backs, jumps, full turn from the sample labels."""
    steps = _steps(labels, times)
    forward = sum(s for *_, s in steps if 0 < s < FULL_TURN // 2)
    clockwise = forward >= -sum(s for *_, s in steps if s < 0)
    problems, turned, back_at = [], 0, None
    for k, t, raw in steps:
        s = raw if abs(raw) == FULL_TURN // 2 or clockwise else -raw
        if s >= JUMP:
            problems.append(f"jumps {s * 45} degrees at {t:.1f} s")
        elif s < 0:
            problems.append(f"turns back at {t:.1f} s")
        turned += max(s, 0)
        if back_at is None and turned >= FULL_TURN:
            back_at = k
    problems += _label_problems(labels, times)
    if turned < FULL_TURN:
        problems.append(f"turns {turned * 45} degrees, not a full turn")
    return Reading(clockwise, turned, back_at, problems)


def _samples(n_frames: int, fps: float) -> list[int]:
    indices = list(range(0, n_frames, max(1, round(fps / SAMPLE_FPS))))
    return indices if indices[-1] == n_frames - 1 else [*indices, n_frames - 1]


def loop_end(frames: list[bytes], indices: list[int], back_at: int | None):
    """The frame after the one most like the first, from the sample before
    the return to the sample after it; None when the clip ends there."""
    if back_at is None:
        return None
    lo = indices[back_at - 1] + 1
    hi = indices[min(back_at + 1, len(indices) - 1)]
    best = max(
        range(lo, hi + 1), key=lambda i: media.ssim(frames[i], frames[0])
    )
    return best + 1 if len(frames) - (best + 1) >= 2 else None


def check_turn(clip: bytes) -> Turn:
    """Label the samples (one judge call), reverse, read, cut."""
    frames = core_video.extract_frames(clip)
    if len(frames) < 2:
        return Turn(clip, [], [], ["the video could not be read"])
    _, fps, _ = core_video.probe(clip)
    indices = _samples(len(frames), fps)
    labels = shoe_refs.label_images(
        [frames[i] for i in indices], eval_mode=True
    )
    reading = read_turn(labels, [i / fps for i in indices])
    reversed_ = not reading.clockwise
    if reversed_:
        frames, labels = frames[::-1], labels[::-1]
        indices = [len(frames) - 1 - i for i in reversed(indices)]
        clip = core_video.encode_frames(frames, fps=fps)
        trace.save("clip-reversed", clip)
        reading = read_turn(labels, [i / fps for i in indices])
    details = {"labels": labels, "reversed": reversed_}
    details["degrees"], details["findings"] = (
        reading.steps * 45,
        reading.problems,
    )
    end = (
        None if reading.problems else loop_end(frames, indices, reading.back_at)
    )
    if end is not None:
        clip = core_video.encode_frames(frames[:end], fps=fps)
        trace.save("clip-closed", clip, meta={"cut_frame": end})
        details["cut_frame"] = end
        kept = [k for k, i in enumerate(indices) if i < end]
        indices, labels = [indices[k] for k in kept], [labels[k] for k in kept]
    picks = [(i, frames[i]) for i in indices]
    return Turn(clip, picks, labels, reading.problems, details)


def evaluate(clip: bytes, refs: dict[str, bytes]) -> tuple[Verdict, bytes]:
    """The turn checks, then the shoe judge on the labelled frames."""
    try:
        turn = check_turn(clip)
    except Exception as exc:
        logger.warning(f"shoe spin: frame labelling failed: {exc}")
        description = f"eval incomplete: frame labelling failed — {exc}"
        return Verdict(False, error=True, description=description), clip
    details = {"turn": turn.details}
    if turn.problems:
        description = "; ".join(turn.problems)
        verdict = Verdict(
            False, REJECT_IMPERFECTIONS, description, details=details
        )
        return verdict, turn.clip
    references = {v: refs[v] for v in JUDGED_VIEWS if v in refs}
    if not references:
        description = "spin valid; no reference view to judge fidelity"
        return Verdict(
            True, description=description, details=details
        ), turn.clip
    verdict = video_fidelity.evaluate_video_fidelity(
        turn.clip,
        references,
        orientation="shoe",
        judge="shoe",
        frames=turn.frames,
        labels=turn.labels,
    )
    verdict.details.update(details)
    return verdict, turn.clip
