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

"""Checks of a non-shoe spin, all started together once the clip is made.

Optical flow (``motion.check_rotation``: direction, turn-backs), the judge
model's frame labels (a frame every half second and the last one, each one
of the eight views around the product, ``broken`` or ``unclear``) and the
glitch call (``motion.detect_glitches``, on the clip as made) run at once. A
spin that turns back is rejected; an anticlockwise one is reversed (with its
labels). The labels then say whether it closes: the view opposite its first
one shows up, and a frame of its last third has its first view, where the
clip is cut on the frame most like the first (``shoe_check.loop_end``). A
spin that never comes back within one view (it stops a quarter turn short)
is rejected; one that ends one view short, or whose labels failed, is cut as
before (the frame of its last second most like the first). The judge gets
the labelled frames (no second labelling call) while the glitch call ends.

On 18 spins with references (15 made for this, 3 earlier): the one that
stopped a quarter short was the only one the labels rejected; optical flow
and the glitch call passed it. A spin is labelled in 9-12 s; the glitch call
takes 8-24 s.
"""

import logging
from concurrent.futures import Future

import numpy as np

from genmedia4commerce.core import gen, media, trace
from genmedia4commerce.core import video as core_video
from genmedia4commerce.core.models import get_models
from genmedia4commerce.core.retry import ContextPool
from genmedia4commerce.core.types import Verdict
from genmedia4commerce.eval import motion, video_fidelity
from genmedia4commerce.workflows.spin.product_refs import PRODUCT_VIEWS
from genmedia4commerce.workflows.spin.shoe_check import loop_end

__all__ = ["evaluate", "label_frames", "read_close"]

logger = logging.getLogger(__name__)

SAMPLE_FPS = 2
RETURN_SHARE = 1 / 3  # the first view is looked for in this end of the clip
LOOP_S = 1.0  # fallback cut: the last second's frame most like the first
MIN_SPIN_S = 4.0  # a shorter clip is never cut
# A glitch reject scores like a 2-defect fidelity reject (40): both clips are
# unusable, so a broken spin no longer outranks a near miss (it scored 70).
GLITCH_IMPERFECTIONS = 2
_LABEL_SIDE = 768
_HALF = len(PRODUCT_VIEWS) // 2
_ROTATION_FINDINGS = {
    "anticlockwise": "rotation check: anticlockwise, clip reversed",
    "unknown": "rotation check: too little motion to tell, clip kept as is",
    "invalid": "rotation check: the spin turns back",
}

_LABEL_SYSTEM = (
    "You are an expert in product photography. You receive reference photos "
    "of ONE product, each labelled with the view it shows, then frames "
    "sampled in order from a 360-degree spin video of that product, labelled "
    "FRAME_0, FRAME_1, ... Label EACH frame with the view of the product it "
    "shows, by where the product's FRONT points in the picture (the front "
    "as in the references):\n"
    "- front: the front faces the camera. back: the front faces away.\n"
    "- right / left: side view, the front points to the right / left edge "
    "of the picture.\n"
    "- front_right / front_left: three-quarter, the front faces the camera "
    "and points to the right / left.\n"
    "- back_right / back_left: three-quarter, the front faces away and "
    "points to the right / left.\n"
    "- broken: not a clean view of this product: two copies of it or a "
    "different product, the product cut by an edge of the picture, deformed "
    "or merging with something, or a cut to another shot.\n"
    "- unclear: the view cannot be told (for instance the product looks the "
    "same from several sides).\n"
    "Compare the frames with each other and with the references to tell "
    "close views apart. Return one entry per frame, in order."
)
_LABEL_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "labels": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "frame": {"type": "INTEGER"},
                    "label": {
                        "type": "STRING",
                        "enum": [*PRODUCT_VIEWS, "broken", "unclear"],
                    },
                },
                "required": ["frame", "label"],
            },
        }
    },
    "required": ["labels"],
}


def label_frames(frames: list[bytes], refs: dict[str, bytes]) -> list[str]:
    """One judge-model call: a label per frame (``missing`` if skipped)."""
    parts: list = []
    for view, ref in refs.items():
        parts += [f"REFERENCE ({view}):", media.resize_max(ref, _LABEL_SIDE)]
    for i, frame in enumerate(frames):
        parts += [f"FRAME_{i}:", media.resize_max(frame, _LABEL_SIDE)]
    raw = gen.generate_json(
        parts,
        system=_LABEL_SYSTEM,
        schema=_LABEL_SCHEMA,
        params=gen.TextParams(model=get_models().judge, thinking="MEDIUM"),
    )
    entries = raw.get("labels") if isinstance(raw, dict) else None
    found = {
        e.get("frame"): e.get("label")
        for e in entries or []
        if isinstance(e, dict)
    }
    return [found.get(i) or "missing" for i in range(len(frames))]


def _gap(a: str, b: str) -> int:
    """Views between ``a`` and ``b`` around the product (0 to 4)."""
    d = abs(PRODUCT_VIEWS.index(a) - PRODUCT_VIEWS.index(b))
    return min(d, len(PRODUCT_VIEWS) - d)


def read_close(labels: list[str], times: list[float]) -> tuple[str, int | None]:
    """Whether the spin closes: ``closed`` (with the sample back at its first
    view), ``short`` (one view short), ``open`` (never back within one view,
    or never on the far side) or ``unknown`` (no first view to go by)."""
    start = labels[0] if labels else None
    if start not in PRODUCT_VIEWS:
        return "unknown", None
    far = PRODUCT_VIEWS[
        (PRODUCT_VIEWS.index(start) + _HALF) % len(PRODUCT_VIEWS)
    ]
    end_from = times[-1] * (1 - RETURN_SHARE)
    views = [(k, x) for k, x in enumerate(labels) if x in PRODUCT_VIEWS]
    back_at = next(
        (k for k, x in views if times[k] >= end_from and x == start), None
    )
    upto = back_at if back_at is not None else len(labels)
    if not any(_gap(x, far) <= 1 for k, x in views if k < upto):
        return "open", None
    if back_at is not None:
        return "closed", back_at
    near = any(times[k] >= end_from and _gap(x, start) == 1 for k, x in views)
    return ("short", None) if near else ("open", None)


def _close_loop(
    clip: bytes, window_s: float = LOOP_S
) -> tuple[bytes, int | None]:
    """End the spin on the frame of its last ``window_s`` most like its
    first (a spin past 360 degrees is cut back to where it started)."""
    frames = core_video.extract_frames(clip)
    _, fps, _ = core_video.probe(clip)
    window = max(1, round(window_s * fps))
    # Clear of the half turn, where the far side can look like the start: no
    # cut unless the clip is 3 windows long.
    if len(frames) < max(round(MIN_SPIN_S * fps), 3 * window):
        return clip, None
    start = len(frames) - window
    scores = [media.ssim(frame, frames[0]) for frame in frames[start:]]
    cut = start + int(np.argmax(scores)) + 1
    if len(frames) - cut < 2:  # it already ends on (or next to) its best match
        return clip, None
    return core_video.trim(clip, start_frame=0, end_frame=cut), cut


def _sampled(clip: bytes, refs: dict[str, bytes]):
    """The clip's frames, fps, sampled indices and their labels."""
    frames = core_video.extract_frames(clip)
    _, fps, _ = core_video.probe(clip)
    indices = list(range(0, len(frames), max(1, round(fps / SAMPLE_FPS))))
    if indices[-1] != len(frames) - 1:
        indices.append(len(frames) - 1)
    return (
        frames,
        fps,
        indices,
        label_frames([frames[i] for i in indices], refs),
    )


def _labelled(labelling: Future, flipped: bool):
    """The labelling's result, reversed with the clip; None when it failed."""
    try:
        frames, fps, indices, labels = labelling.result()
    except Exception as exc:
        logger.warning(f"product spin: frame labelling failed: {exc}")
        return None
    if flipped:
        frames, labels = frames[::-1], labels[::-1]
        indices = [len(frames) - 1 - i for i in reversed(indices)]
    return frames, fps, indices, labels


def _closed(clip: bytes, labelled, details: dict):
    """The clip cut where it closes, its labelled samples kept (None without
    labels), the cut frame, and why it is rejected (None when it is not)."""
    if labelled is None:
        clip, cut = _close_loop(clip)
        return clip, None, cut, None
    frames, fps, indices, labels = labelled
    state, back_at = read_close(labels, [i / fps for i in indices])
    details["turn"] = {"labels": labels, "closes": state}
    if state == "open":
        reason = (
            "the spin does not come back to its first view (started at "
            f"{labels[0]}, ended at {labels[-1]})"
        )
        return clip, None, None, reason
    if state == "closed":
        cut = loop_end(frames, indices, back_at)
        if cut is not None:
            clip = core_video.encode_frames(frames[:cut], fps=fps)
    else:  # one view short or no first view: cut as before
        clip, cut = _close_loop(clip)
    if state == "unknown":  # the judge labels the frames itself
        return clip, None, cut, None
    kept = [k for k, i in enumerate(indices) if cut is None or i < cut]
    samples = ([indices[k] for k in kept], [labels[k] for k in kept], frames)
    return clip, samples, cut, None


def _rotation_details(rotation: str, flipped: bool) -> dict:
    checked: dict = {"verdict": rotation, "reversed": flipped}
    if rotation in _ROTATION_FINDINGS:  # a findings line shows in the UI
        checked["findings"] = [_ROTATION_FINDINGS[rotation]]
    return checked


def _judge(clip, refs, samples) -> Verdict:
    references = {v: refs[v] for v in PRODUCT_VIEWS if v in refs}
    if not references:
        description = "spin valid; no reference view to judge fidelity"
        return Verdict(True, description=description)
    labelled = {}
    if samples is not None:
        indices, labels, frames = samples
        labelled = {
            "frames": [(i, frames[i]) for i in indices],
            "labels": labels,
        }
    return video_fidelity.evaluate_video_fidelity(
        clip, references, orientation="gemini", judge="product", **labelled
    )


def evaluate(clip: bytes, refs: dict[str, bytes]) -> tuple[Verdict, bytes]:
    """Flow, labels and glitch call at once; turn-backs and open spins are
    rejected, the clip reversed and cut, then judge and glitch verdict."""
    pool = ContextPool(max_workers=4)
    try:
        labelling = pool.submit(_sampled, clip, refs)
        glitching = pool.submit(motion.detect_glitches, clip)
        rotation = motion.check_rotation(clip)
        flipped = rotation == "anticlockwise"
        details: dict = {"rotation": _rotation_details(rotation, flipped)}
        if rotation == "invalid":
            description = "the spin turns back instead of one full turn"
            verdict = Verdict(False, GLITCH_IMPERFECTIONS, description)
            verdict.details.update(details)
            return verdict, clip
        if flipped:
            clip = core_video.reverse(clip)
            trace.save("clip-reversed", clip)
        clip, samples, cut, problem = _closed(
            clip, _labelled(labelling, flipped), details
        )
        if cut is not None:
            trace.save("clip-closed", clip, meta={"cut_frame": cut})
            details["loop"] = {"cut_frame": cut}
        if problem is not None:
            verdict = Verdict(False, GLITCH_IMPERFECTIONS, problem)
            verdict.details.update(details)
            return verdict, clip
        judging = pool.submit(_judge, clip, refs, samples)
        glitch = glitching.result()
        if not glitch.publish:
            details["glitch"] = glitch.description
            imperfections = 0 if glitch.error else GLITCH_IMPERFECTIONS
            verdict = Verdict(
                False, imperfections, glitch.description, error=glitch.error
            )
        else:
            verdict = judging.result()
        verdict.details.update(details)
        return verdict, clip
    finally:
        pool.shutdown(wait=False)  # a rejection does not wait for the rest
