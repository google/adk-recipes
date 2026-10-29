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

"""Reference set of the shoe spin, built with the judge and image models.

1. One call labels every photo: a view, ``invalid``, or several shoes that
   touch (``multiple_touching``) or stand apart (``multiple_separate``).
2. A single-shoe photo is trimmed to its content.
3. Shoes apart: one mask call, which also gives each shoe's view (asked
   once more when a mask fails its fit or coverage check), then every shoe
   is cut with its own shadow and the real background, the other shoes
   repainted (``shoe_split``).
4. Shoes touching: the image model erases the back shoe; the cut of the
   largest mask (its view from the same mask call) is the fallback when its
   output is refused or unusable.
5. One call labels the image model's outputs; only pieces with a view are
   kept.
6. Per candidate, the foot the shoe is made for; per pair pointing the same
   way, whether they show the same side of the shoe.
7. Where two show opposite sides, the left-foot shoes of that direction are
   mirrored (their view flipped) by the image model (``shoe_twin``: the
   same shoe for the other foot, its lettering reading correctly, not
   backwards); everything else is used as is.
8. One reference per view in ``ORDER``: as is before mirrored (a mirrored
   shoe only for a view no real one shows; dropped when its twin fails),
   then a photo before a mask cut before an image-model output.

No AI upscale: the video model gets each reference scaled to its 720p frame
anyway, and an image model asked to upscale a shoe cut from a pair photo
redrew the missing shoe in 6 of 12 tries.
"""

import itertools
import logging
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from genmedia4commerce.core import gen, media, trace
from genmedia4commerce.core.models import get_models
from genmedia4commerce.core.retry import ContextPool
from genmedia4commerce.workflows.spin import shoe_split as split
from genmedia4commerce.workflows.spin import shoe_twin
from genmedia4commerce.workflows.spin.shoe_prompts import (
    FOOT_SCHEMA,
    FOOT_SYSTEM,
    NANO_ERASE_PROMPT,
    PAIRWISE_SCHEMA,
    PAIRWISE_SYSTEM,
    classify_prompt,
)

__all__ = [
    "MAX_REFERENCES",
    "NANO_ERASE_PROMPT",
    "ORDER",
    "SIDES",
    "ShoeRefs",
    "build",
    "label_images",
]

logger = logging.getLogger(__name__)

_NANO_PARAMS = gen.ImageParams(aspect_ratio="auto", image_size="2K")
# Side views, by direction family: the toe toward the right or the left.
_R = ("right", "front_right", "back_right")
_L = ("left", "front_left", "back_left")
SIDES = _R + _L
ORDER = (*SIDES, "front", "back", "top_front")  # generation order
MAX_REFERENCES = 10  # images the video model takes in one request
_VIEWS = frozenset(ORDER)
_FAMILY = {**dict.fromkeys(_R, "R"), **dict.fromkeys(_L, "L")}
_FLIP = dict(zip(SIDES, _L + _R, strict=True))  # a mirrored view
_RANK = {"photo": 0, "mask": 1, "nano": 2}
_PAIRED = 5  # candidates of a family compared pairwise
_LABEL_SIDE = 768  # photos are labelled at most this size
_JUDGE_SIDE = 1024  # and judged (foot, pairwise) at most this size


@dataclass(frozen=True)
class ShoeRefs:
    """The references (view -> PNG, generation order) and the mirrored views."""

    images: dict[str, bytes]
    mirrored: frozenset[str] = frozenset()


@dataclass(eq=False)  # compared by identity
class _Piece:
    """A shoe cut from a photo of several (``role``: cut, nano, fallback)."""

    photo: int
    role: str
    image: bytes
    name: str
    view: str = "missing"


@dataclass(frozen=True)
class _Candidate:
    image: bytes  # PNG
    view: str
    source: str  # photo, mask or nano
    name: str  # in the trace


def _params() -> gen.TextParams:
    return gen.TextParams(model=get_models().judge, thinking="MEDIUM")


def label_images(images: Sequence[bytes], eval_mode: bool = False) -> list[str]:
    """One call: a label per image, ``missing`` when the answer skips one.

    ``eval_mode`` (frames of a spin video): the eight standing views or
    ``not_standing``.
    """
    parts: list = []
    for i, image in enumerate(images):
        parts += [f"IMAGE_{i}:", media.resize_max(image, _LABEL_SIDE)]
    system, schema = classify_prompt(eval_mode)
    raw = gen.generate_json(
        parts, system=system, schema=schema, params=_params()
    )
    entries = raw.get("labels") if isinstance(raw, dict) else None
    found = {
        e.get("image"): e.get("label")
        for e in entries or []
        if isinstance(e, dict)
    }
    return [found.get(i) or "missing" for i in range(len(images))]


def _masks(image: bytes, size: tuple[int, int]) -> list[dict]:
    """``split.ask_masks``; a failed call gives no masks."""
    try:
        return split.ask_masks(image, size)
    except Exception as exc:
        logger.warning(f"shoe mask call failed: {exc}")
        return []


def _filled(arr: np.ndarray, items: list[dict]):
    """The items' views and masks, empty masks dropped."""
    height, width = arr.shape[:2]
    pairs = zip(items, split.to_masks(items, width, height), strict=True)
    kept = [(item["view"], m) for item, m in pairs if m.any()]
    return [v for v, _ in kept], [m for _, m in kept]


def _score(attempt: tuple[list, list]) -> float:
    """Fewer failing masks, then more coverage; no masks scores -1."""
    items, scores = attempt
    if not items:
        return -1.0
    return -len(split.bad(scores)) + 0.01 * sum(c for _, c in scores)


def _separate(image: bytes, note: dict) -> list[tuple[bytes, str]]:
    """Every shoe of a photo whose shoes stand apart: (cut PNG, view).

    The masks are asked once more when one fails its checks (or none came
    back); the better attempt is kept.
    """
    img = split.open_rgb(image)
    arr = np.array(img)
    height, width = arr.shape[:2]
    attempts: list[tuple[list, list]] = []
    for _ in range(2):
        items = _masks(image, img.size)
        scores = split.quality(arr, items, split.to_masks(items, width, height))
        attempts.append((items, scores))
        if items and not split.bad(scores):
            break
    items, scores = max(attempts, key=_score)
    note["mask_calls"] = len(attempts)
    note["masks"] = [[round(f, 2), round(c, 2)] for f, c in scores]
    views, masks = _filled(arr, items)
    if not masks:
        return []
    grown = split.grow(masks)
    cuts = [split.cut_keep(arr, masks, grown, k) for k in range(len(masks))]
    return [(split.png(cut), v) for cut, v in zip(cuts, views, strict=True)]


def _fallback(image: bytes) -> list[tuple[bytes, str]]:
    """The cut of the largest mask of a photo whose shoes touch, with its
    view (0 or 1)."""
    img = split.open_rgb(image)
    arr = np.array(img)
    views, masks = _filled(arr, _masks(image, img.size))
    if not masks:
        return []
    k = int(np.argmax([m.sum() for m in masks]))
    cut = split.cut_keep(arr, masks, split.grow(masks), k)
    return [(split.png(cut), views[k])]


def _nano(image: bytes) -> list[tuple[bytes, str]]:
    """The image model's erase of the back shoe, trimmed, its view still
    ``missing`` (0 or 1)."""
    try:
        erased = gen.generate_image(
            [NANO_ERASE_PROMPT, image], params=_NANO_PARAMS
        )
        trimmed = media.crop_to_content(split.open_rgb(erased))
        return [(split.png(trimmed), "missing")]
    except Exception as exc:
        logger.warning(f"nano clean-up failed: {exc}")
        return []


def _split(images, labels, notes: dict) -> list[_Piece]:
    """The pieces of every photo of several shoes (one pool, all at once)."""
    jobs = []
    with ContextPool(max_workers=max(1, 2 * len(images))) as pool:
        for i, (image, label) in enumerate(zip(images, labels, strict=True)):
            if label == "multiple_separate":
                note: dict = {}
                notes[i] = note
                jobs.append((i, "cut", pool.submit(_separate, image, note)))
            elif label == "multiple_touching":
                jobs.append((i, "nano", pool.submit(_nano, image)))
                jobs.append((i, "fallback", pool.submit(_fallback, image)))
        done = [(i, role, job.result()) for i, role, job in jobs]
    pieces = []
    for i, role, outputs in done:
        for k, (out, view) in enumerate(outputs):
            name = f"photo {i} {role}" + (f" {k}" if role == "cut" else "")
            pieces.append(_Piece(i, role, out, name, view))
    return pieces


def _label_pieces(pieces: list[_Piece]) -> None:
    """One call labels the image model's outputs (the cuts have their view
    from the mask call); a failure leaves them ``missing``."""
    nano = [p for p in pieces if p.role == "nano"]
    if not nano:
        return
    try:
        views = label_images([p.image for p in nano])
    except Exception as exc:
        logger.warning(f"piece labelling failed: {exc}")
        return
    for piece, view in zip(nano, views, strict=True):
        piece.view = view


def _usable(pieces: list[_Piece]) -> list[_Piece]:
    """Pieces with a view; for a touching photo the image model's output,
    else its fallback cut."""
    nano = {p.photo for p in pieces if p.role == "nano" and p.view in _VIEWS}
    return [
        p
        for p in pieces
        if p.view in _VIEWS and not (p.role == "fallback" and p.photo in nano)
    ]


def _foot(image: bytes) -> str | None:
    """The foot the shoe is made for; None when the call fails."""
    try:
        raw = gen.generate_json(
            [media.resize_max(image, _JUDGE_SIDE)],
            system=FOOT_SYSTEM,
            schema=FOOT_SCHEMA,
            params=_params(),
        )
    except Exception as exc:
        logger.warning(f"foot call failed: {exc}")
        return None
    return raw.get("foot") if isinstance(raw, dict) else None


def _pairwise(a: bytes, b: bytes) -> str:
    """same_side, opposite_sides or unsure (also when the call fails)."""
    parts: list[gen.Part] = ["IMAGE_A:", media.resize_max(a, _JUDGE_SIDE)]
    parts += ["IMAGE_B:", media.resize_max(b, _JUDGE_SIDE)]
    try:
        raw = gen.generate_json(
            parts,
            system=PAIRWISE_SYSTEM,
            schema=PAIRWISE_SCHEMA,
            params=_params(),
        )
    except Exception as exc:
        logger.warning(f"pairwise call failed: {exc}")
        return "unsure"
    return (raw.get("answer") if isinstance(raw, dict) else None) or "unsure"


def _judge(cands: list[_Candidate]):
    """Every candidate's foot and the pairwise answers, all in parallel.

    Pairs: the first ``_PAIRED`` candidates of each direction family.
    """
    pairs: list[tuple[int, int]] = []
    for family in ("R", "L"):
        members = [
            k for k, c in enumerate(cands) if _FAMILY.get(c.view) == family
        ]
        pairs += itertools.combinations(members[:_PAIRED], 2)
    with ContextPool(max_workers=max(1, len(cands) + len(pairs))) as pool:
        feet = [pool.submit(_foot, c.image) for c in cands]
        answers = [
            pool.submit(_pairwise, cands[a].image, cands[b].image)
            for a, b in pairs
        ]
        return [f.result() for f in feet], {
            pair: f.result() for pair, f in zip(pairs, answers, strict=True)
        }


def _select(cands: list[_Candidate], feet: list, answers: dict) -> list:
    """(view, mirrored, candidate) for each view in ``ORDER`` that has one.

    A family with a pair on opposite sides has a conflict: its left-foot
    candidates are mirrored and their view flipped. A mirrored candidate is
    only a last resort, for a view no real (as-is) candidate shows.
    """
    conflict = {
        _FAMILY[cands[a].view]
        for (a, _), answer in answers.items()
        if answer == "opposite_sides"
    }
    options = []
    for cand, foot in zip(cands, feet, strict=True):
        mirror = _FAMILY.get(cand.view) in conflict and foot == "left"
        options.append(
            (_FLIP[cand.view] if mirror else cand.view, mirror, cand)
        )
    chosen: list = []
    for view in ORDER:
        if view == "top_front" and any(v == "front" for v, _, _ in chosen):
            continue
        found = [o for o in options if o[0] == view]
        if found:
            chosen.append(min(found, key=lambda o: (o[1], _RANK[o[2].source])))
    return chosen[:MAX_REFERENCES]


def build(images: Sequence[bytes]) -> ShoeRefs:
    """Build the shoe reference set (generation order; missing views absent).

    The labelling call's failure propagates: no reference can be built.
    """
    labels = label_images(images)
    notes: dict = {"labels": labels, "separate": {}}
    cands = [
        _Candidate(
            split.png(media.crop_to_content(split.open_rgb(image))),
            label,
            "photo",
            f"photo {i}",
        )
        for i, (image, label) in enumerate(zip(images, labels, strict=True))
        if label in _VIEWS
    ]
    pieces = _split(images, labels, notes["separate"])
    _label_pieces(pieces)
    kept = _usable(pieces)
    notes["pieces"] = {p.name: [p.view, p in kept] for p in pieces}
    cands += [
        _Candidate(
            p.image, p.view, "nano" if p.role == "nano" else "mask", p.name
        )
        for p in kept
    ]
    feet, answers = _judge(cands)
    chosen = _select(cands, feet, answers)
    jobs = [(c.image, c.view, view) for view, m, c in chosen if m]
    made = iter(shoe_twin.twins(jobs) if jobs else [])
    drawn = {view: next(made) if m else c.image for view, m, c in chosen}
    refs = {view: image for view, image in drawn.items() if image}
    mirrored = frozenset(view for view, m, _ in chosen if m and view in refs)
    notes["feet"] = {c.name: foot for c, foot in zip(cands, feet, strict=True)}
    notes["pairs"] = {
        f"{cands[a].name} | {cands[b].name}": answer
        for (a, b), answer in answers.items()
    }
    notes["selected"] = {view: c.name for view, _, c in chosen}
    notes["mirrored"] = sorted(mirrored)
    notes["twin_failed"] = sorted(drawn.keys() - refs.keys())
    trace.save("shoe-refs", notes)
    logger.info(f"shoe references: {list(refs)}, mirrored: {sorted(mirrored)}")
    return ShoeRefs(refs, mirrored)
