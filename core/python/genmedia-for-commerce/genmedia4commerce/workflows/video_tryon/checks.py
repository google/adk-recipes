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

"""Product checks of a try-on video, each product on a few still frames.

Like the image try-on, the products are judged by the one-call outfit judge
(``eval.outfit``, video-frame mode), each against its own photos and named
by its category; the judge lists defects per product, so one product cannot
hide another (an older whole-outfit judge passed a bag with an invented
padlock next to a good coat). The frames are the stillest of each half of
the clip and the middle of each close shot (``pick_frames``), each with a
view (``label_views``). One lite call lists the products each frame shows
(``visible``); then one judge call per frame with every product it shows,
all in parallel, each given ``JUDGE_CAP_S`` (``start_judges``,
``collect``). A frame with no answer by then is "not checked", never a
rejection. A product whose only findings are minor (``outfit.MINOR``: a
fabric texture difference) passes its check and costs the score one
imperfection per minor finding of its frame with the most (not per frame).
``summarize`` turns the checks into the verdict's parts.
"""

import logging
import time
from collections.abc import Sequence
from concurrent.futures import Executor, Future
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field

from genmedia4commerce.core import gen, media, video
from genmedia4commerce.core.errors import EvalError, user_message
from genmedia4commerce.core.gen import Part, TextParams
from genmedia4commerce.core.models import get_models
from genmedia4commerce.core.types import Verdict
from genmedia4commerce.eval import orientation, outfit
from genmedia4commerce.workflows.tryon.evaluate import _eval_refs
from genmedia4commerce.workflows.video_tryon.refs import (
    classified,
    product_names,
    product_photo,
)
from genmedia4commerce.workflows.video_tryon.types import (
    Plan,
    ReferenceSet,
    VideoTryOnRequest,
)

__all__ = [
    "JUDGE_CAP_S",
    "Frame",
    "Item",
    "collect",
    "frame_details",
    "label_views",
    "pairs",
    "pick_frames",
    "products",
    "start_judges",
    "summarize",
    "visible",
]

logger = logging.getLogger(__name__)

JUDGE_CAP_S = 120  # seconds for each judge call, from when the judges start
SETTLE_S = 0.5  # a crossfade may blend a shot's first and last half second
CLOSEUP_GAP_S = 0.5  # a close-up frame nearer a still frame is a repeat
VISIBILITY_SIDE = 512
MAX_NAMED = 4  # rejected checks named in the description
OUTFIT = "outfit"
NOTHING = "no product photo to check"

VISIBILITY_SYSTEM = (
    "You see the products of an outfit, one per line as 'id: name', then "
    "{n} frames of a fashion video (FRAME_0 to FRAME_{last}). For each "
    "frame, list the ids of the products visible in it, even partly (a "
    "sleeve, a strap, the tip of a shoe). Return JSON only."
)


@dataclass
class Item:
    """One product to check, with its references per view."""

    id: str  # its close-up name ("bag", "top 2"), or "outfit"
    name: str  # what the visibility call calls it, e.g. "crossbody bag"
    product_filter: str | None  # its category; None: the whole outfit
    front: list[bytes]  # for front, side and close-up frames
    back: list[bytes]  # for back frames; empty: not judged there


@dataclass
class Frame:
    """One frame to judge."""

    index: int
    second: float
    image: bytes
    closeup: bool = False  # inside a detail shot
    view: str = "front"
    visible: list[str] = field(default_factory=list)  # ids of its items


def products(req: VideoTryOnRequest, refs: ReferenceSet) -> list[Item]:
    """One item per product, in order, photo sent to the video or not (a
    garment gets none: ``refs``).

    A classified product is judged against its best upload of the view
    (``tryon.evaluate._eval_refs``, no crops), on back frames only when it
    has a back upload. Without uploads (a direct animation through the API
    sends none), against its photo as the video gets it, on front frames.
    Unclassified, each close-up against the photo sent to the video, on
    front frames. With no product photo at all, the whole outfit is judged
    against the try-on images, unfiltered.
    """
    sent = {c.name: refs.images[c.index] for c in refs.closeups}
    items = [Item(n, n, n, [photo], []) for n, photo in sent.items()]
    if classified(req):
        names = [name for name, _ in product_names(req)]
        items = [_item(req, k, name, sent) for k, name in enumerate(names)]
    if items:
        return items
    back = [req.back_image] if req.back_image else []
    return [Item(OUTFIT, OUTFIT, None, [req.animated_image], back)]


def _item(req: VideoTryOnRequest, k: int, name: str, sent: dict) -> Item:
    """Classified product ``k``; ``sent``: the photos the video got."""
    p = classified(req)[k]
    back = any(r.view == "back" and r.source == "upload" for r in p.refs)
    return Item(
        name,
        p.description or name,
        p.category,
        _eval_refs(p, "front") or [sent.get(name) or product_photo(req, k)],
        _eval_refs(p, "back") if back else [],
    )


def _detail_windows(plan: Plan, clip_s: float) -> list[tuple[float, float]]:
    """(start, end) second of each detail shot past its crossfades, the
    plan timing scaled to the clip length."""
    scale = clip_s / (sum(s.seconds for s in plan.shots) or 1.0)
    windows, start = [], 0.0
    for shot in plan.shots:
        end = start + shot.seconds
        if shot.move == "detail":
            windows.append((start * scale + SETTLE_S, end * scale - SETTLE_S))
        start = end
    return windows


def pick_frames(
    clip: bytes, stable: list[tuple[int, bytes]], plan: Plan
) -> list[Frame]:
    """The frames to judge, by index: ``video.stillest_frames``, plus for
    each detail shot the stable frame (``stable``, the clip's
    ``video.pick_stable_frames``) nearest its middle, unless a frame is
    already within ``CLOSEUP_GAP_S``. Frames inside a detail shot are
    close-ups."""
    n_frames, fps, _ = video.probe(clip)
    windows = _detail_windows(plan, n_frames / fps)
    frames = [Frame(i, i / fps, png) for i, png in video.stillest_frames(clip)]
    for lo, hi in windows:
        middle = (lo + hi) / 2
        inside = [
            (abs(i / fps - middle), i, png)
            for i, png in stable
            if lo <= i / fps <= hi
        ]
        if not inside:
            continue
        _, i, png = min(inside)
        if all(abs(i / fps - f.second) >= CLOSEUP_GAP_S for f in frames):
            frames.append(Frame(i, i / fps, png))
    for f in frames:
        f.closeup = any(lo <= f.second <= hi for lo, hi in windows)
    return sorted(frames, key=lambda f: f.index)


def label_views(frames: list[Frame], references: dict[str, bytes]) -> None:
    """Set the view of each frame (one Gemini call, ``references`` guiding
    it). A close-up is no back view: always "front". If labelling fails,
    every frame is front."""
    try:
        labels = orientation.label_frames(
            [f.image for f in frames], strategy="gemini", references=references
        )
    except Exception as exc:  # a lost view must not lose the video
        logger.warning(f"frame views failed, all judged as front: {exc}")
        labels = ["front"] * len(frames)
    for f, label in zip(frames, labels, strict=True):
        f.view = "front" if f.closeup else label


def _visibility_schema(ids: list[str]) -> dict:
    shown = {"type": "ARRAY", "items": {"type": "STRING", "enum": ids}}
    row = {
        "type": "OBJECT",
        "properties": {"frame": {"type": "INTEGER"}, "visible": shown},
        "required": ["frame", "visible"],
    }
    return {
        "type": "OBJECT",
        "properties": {"frames": {"type": "ARRAY", "items": row}},
        "required": ["frames"],
    }


def visible(frames: Sequence[Frame], items: Sequence[Item]) -> list[list[str]]:
    """The ids of the items each frame shows (one lite-model call).

    The whole outfit needs no call: it is always shown. On a failure, or for
    a frame the answer leaves out, every item counts: a check too many costs
    a call, one too few lets a defect through.
    """
    ids = [item.id for item in items]
    if not frames or all(item.product_filter is None for item in items):
        return [ids for _ in frames]
    lines = "\n".join(f"{item.id}: {item.name}" for item in items)
    parts: list[Part] = [f"PRODUCTS:\n{lines}"]
    for k, f in enumerate(frames):
        parts += [f"FRAME_{k}:", media.resize_max(f.image, VISIBILITY_SIDE)]
    system = VISIBILITY_SYSTEM.format(n=len(frames), last=len(frames) - 1)
    try:
        raw = gen.generate_json(
            parts,
            system=system,
            schema=_visibility_schema(ids),
            params=TextParams(model=get_models().text_lite, thinking="LOW"),
        )
        if not isinstance(raw, dict):
            raise EvalError(f"not a JSON object: {raw!r:.200}")
        answer = {
            row["frame"]: [i for i in ids if i in row["visible"]]
            for row in raw["frames"]
        }
    except Exception as exc:
        logger.warning(f"visibility check failed, every product counts: {exc}")
        return [ids for _ in frames]
    return [answer.get(k, ids) for k in range(len(frames))]


def pairs(
    frames: Sequence[Frame], items: Sequence[Item]
) -> list[tuple[Frame, Item, list[bytes]]]:
    """(frame, item, references) to judge: each item on the frames showing
    it, against its references for the frame's view (none: skipped). An item
    no frame shows is judged on the first front frame, where every product
    should show, so a vanished product cannot pass unchecked."""
    seen = {i for f in frames for i in f.visible}
    first = next((f for f in frames if f.view != "back"), None)
    found = []
    for f in frames:
        for item in items:
            refs = item.back if f.view == "back" else item.front
            shown = item.id in f.visible or (item.id not in seen and f is first)
            if shown and refs:
                found.append((f, item, refs))
    return found


def start_judges(
    pool: Executor, judged: list[tuple[Frame, Item, list[bytes]]]
) -> list[tuple[Frame, list[Item], Future]]:
    """Submit one outfit judge per frame, with every product it shows."""
    by_frame: dict[int, tuple[Frame, list[tuple[Item, list[bytes]]]]] = {}
    for f, item, refs in judged:
        by_frame.setdefault(f.index, (f, []))[1].append((item, refs))
    return [
        (
            f,
            [item for item, _ in shown],
            pool.submit(
                outfit.evaluate_outfit,
                [
                    outfit.OutfitProduct(refs[0], item.product_filter)
                    for item, refs in shown
                ],
                f.image,
                video_frame=True,
            ),
        )
        for f, shown in by_frame.values()
    ]


def _frame_checks(
    f: Frame, items: list[Item], verdict: Verdict | None, error: str | None
) -> list[dict]:
    """The ``details["checks"]`` entries of one frame, one per product; no
    verdict and no error: the judge did not finish in time. A rejected
    frame rejects the products with defects other than minor ones (all of
    them when it names none); ``minor``: the findings that only lower the
    score (``outfit.MINOR``)."""
    checked = verdict is not None and not verdict.error
    if verdict is not None and verdict.error:
        error = verdict.description or "judge error"
    found, minor = {}, {}
    if checked and verdict is not None:
        judged = verdict.details.get("products") or []
        found = {p.get("index"): p.get("findings") or [] for p in judged}
        minor = {p.get("index"): p.get("minor") or [] for p in judged}
        if not verdict.publish and not any(found.values()):
            found = {
                k: [verdict.description or "rejected"]
                for k in range(1, len(items) + 1)
            }
    entries = []
    for k, item in enumerate(items, start=1):
        findings = [str(x) for x in found.get(k, [])]
        small = [str(x) for x in minor.get(k, [])]
        blocking = len(findings) > len(small)
        entries.append(
            {
                "product": item.id,
                "frame_index": f.index,
                "second": round(f.second, 1),
                "view": f.view,
                "publish": (verdict.publish or not blocking)
                if checked and verdict is not None
                else None,
                "checked": checked,
                "imperfections": len(findings),
                "findings": findings,
                "minor": small,
                "error": error,
            }
        )
    return entries


def collect(
    pending: list[tuple[Frame, list[Item], Future]], started: float
) -> list[dict]:
    """The checks; each frame's judge is waited for until ``JUDGE_CAP_S``
    after ``started`` (``time.monotonic``)."""
    found = []
    for f, items, future in pending:
        timeout = max(0.0, started + JUDGE_CAP_S - time.monotonic())
        try:
            verdict, error = future.result(timeout=timeout), None
        except FutureTimeout:
            verdict, error = None, None
        except Exception as exc:
            logger.warning(f"judge of frame {f.index}: {exc}")
            verdict, error = None, user_message(exc)
        found += _frame_checks(f, items, verdict, error)
    return found


def _minor(passed: list[dict]) -> tuple[int, str]:
    """(imperfections, description) of the minor findings of passed
    checks: each product counts its most on one frame (a texture seen on
    three frames is one difference, not three)."""
    most: dict[str, int] = {}
    for c in passed:
        most[c["product"]] = max(most.get(c["product"], 0), len(c["minor"]))
    named = [c for c in passed if c["minor"]][:MAX_NAMED]
    if not named:
        return 0, ""
    return sum(most.values()), "; minor differences (score only): " + "; ".join(
        f"{c['product']} at {c['second']:g}s: {c['minor'][0]}" for c in named
    )


def summarize(found: list[dict]) -> tuple[bool, int, str, bool]:
    """(publish, imperfections, description, error) of the checks.

    Publish when no checked pair rejects. When no pair was checked (judges
    late or failing) the verdict is an error: retryable. No pair at all:
    nothing to judge, publish. Minor findings of passed checks only add
    imperfections (``_minor``).
    """
    if not found:
        return True, 0, NOTHING, False
    checked = [c for c in found if c["checked"]]
    failed = [c for c in checked if not c["publish"]]
    late = sum(not c["checked"] and not c["error"] for c in found)
    errored = sum(bool(c["error"]) for c in found)
    if failed:
        text = "rejected: " + "; ".join(
            f"{c['product']} at {c['second']:g}s: "
            + (c["findings"] or ["rejected"])[0]
            for c in failed[:MAX_NAMED]
        )
    elif checked:
        n_products = len({c["product"] for c in checked})
        n_frames = len({c["frame_index"] for c in checked})
        text = (
            f"all {len(checked)} product checks passed "
            f"({n_products} products x {n_frames} frames)"
        )
    else:
        text = "no product check finished"
    n_minor, minor_text = _minor([c for c in checked if c["publish"]])
    text += minor_text
    if late:
        text += f"; {late} checks not finished in time"
    if errored:
        text += f"; {errored} checks errored"
    imperfections = sum(c["imperfections"] for c in failed) + n_minor
    return bool(checked) and not failed, imperfections, text, not checked


def frame_details(frames: Sequence[Frame]) -> list[dict]:
    """``details["frames"]``: the judged frames without their images."""
    return [
        {
            "index": f.index,
            "second": round(f.second, 1),
            "view": f.view,
            "visible": f.visible,
        }
        for f in frames
    ]
