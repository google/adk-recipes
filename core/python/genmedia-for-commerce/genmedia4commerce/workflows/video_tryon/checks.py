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
the clip and the middle of each close shot (``pick_frames``). One judge
call per frame gets every product, with its back photo when it has one;
the judge says which side the frame shows and which products it shows
(a product it does not show is no defect there). All run in parallel,
each given ``JUDGE_CAP_S`` (``start_judges``, ``collect``). A frame with
no answer by then is "not checked", never a rejection. A product shown on
no answered frame but the close-ups is missing: one defect, ``MISSING``
(``_mark_missing``). A product whose only findings are minor (``outfit.MINOR``: a
fabric texture difference) passes its check and costs the score one
imperfection per minor finding of its frame with the most (not per frame).
``summarize`` turns the checks into the verdict's parts.
"""

import logging
import time
from collections.abc import Sequence
from concurrent.futures import Executor, Future
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass

from genmedia4commerce.core import video
from genmedia4commerce.core.errors import user_message
from genmedia4commerce.core.types import Verdict
from genmedia4commerce.eval import outfit
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
    "pick_frames",
    "products",
    "start_judges",
    "summarize",
]

logger = logging.getLogger(__name__)

JUDGE_CAP_S = 45  # seconds for each judge call, from when the judges start
SETTLE_S = 0.5  # a crossfade may blend a shot's first and last half second
CLOSEUP_GAP_S = 0.5  # a close-up frame nearer a still frame is a repeat
MAX_NAMED = 4  # rejected checks named in the description
OUTFIT = "outfit"
MISSING = "not visible in the video"


@dataclass
class Item:
    """One product to check, with its references per view."""

    id: str  # its close-up name ("bag", "top 2"), or "outfit"
    product_filter: str | None  # its category; None: the whole outfit
    front: list[bytes]  # its front photo
    back: list[bytes]  # its back photo; empty: none


@dataclass
class Frame:
    """One frame to judge."""

    index: int
    second: float
    image: bytes
    closeup: bool = False  # inside a detail shot
    view: str | None = None  # the side the judge saw; None: no answer


def products(req: VideoTryOnRequest, refs: ReferenceSet) -> list[Item]:
    """One item per product, in order, photo sent to the video or not (a
    garment gets none: ``refs``).

    A classified product is judged against its best front upload
    (``tryon.evaluate._eval_refs``, no crops) and its best back upload
    when it has one. Without uploads (a direct animation through the API
    sends none), against its photo as the video gets it. Unclassified,
    each close-up against the photo sent to the video. With no product
    photo at all, the whole outfit is judged against the try-on images
    (front and back), unfiltered.
    """
    sent = {c.name: refs.images[c.index] for c in refs.closeups}
    items = [Item(n, n, [photo], []) for n, photo in sent.items()]
    if classified(req):
        names = [name for name, _ in product_names(req)]
        items = [_item(req, k, name, sent) for k, name in enumerate(names)]
    if items:
        return items
    back = [req.back_image] if req.back_image else []
    return [Item(OUTFIT, None, [req.animated_image], back)]


def _item(req: VideoTryOnRequest, k: int, name: str, sent: dict) -> Item:
    """Classified product ``k``; ``sent``: the photos the video got."""
    p = classified(req)[k]
    back = any(r.view == "back" and r.source == "upload" for r in p.refs)
    return Item(
        name,
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


def start_judges(
    pool: Executor, frames: Sequence[Frame], items: Sequence[Item]
) -> list[tuple[Frame, list[Item], Future]]:
    """Submit one outfit judge per frame, each with every product: its
    front photo, and its back photo when it has one."""
    products = [
        outfit.OutfitProduct(
            item.front[0], item.product_filter, back=next(iter(item.back), None)
        )
        for item in items
    ]
    return [
        (
            f,
            list(items),
            pool.submit(
                outfit.evaluate_outfit, products, f.image, video_frame=True
            ),
        )
        for f in frames
    ]


def _frame_checks(
    f: Frame, items: list[Item], verdict: Verdict | None, error: str | None
) -> list[dict]:
    """The ``details["checks"]`` entries of one frame, one per product; no
    verdict and no error: the judge did not finish in time. A rejected
    frame rejects the products with defects other than minor ones (all
    those it shows when it names none); ``minor``: the findings that only
    lower the score (``outfit.MINOR``); ``visible``: whether the judge saw
    the product in the frame."""
    checked = verdict is not None and not verdict.error
    if verdict is not None and verdict.error:
        error = verdict.description or "judge error"
    found, minor, shown = {}, {}, {}
    if checked and verdict is not None:
        judged = verdict.details.get("products") or []
        found = {p.get("index"): p.get("findings") or [] for p in judged}
        minor = {p.get("index"): p.get("minor") or [] for p in judged}
        shown = {p.get("index"): p.get("visible", True) for p in judged}
        if not verdict.publish and not any(found.values()):
            found = {
                k: [verdict.description or "rejected"]
                for k in range(1, len(items) + 1)
                if shown.get(k, True)
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
                "closeup": f.closeup,
                "visible": shown.get(k, True) if checked else None,
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
    after ``started`` (``time.monotonic``). Each answered frame gets the
    view the judge saw."""
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
        if verdict is not None and not verdict.error:
            f.view = verdict.details.get("view")
        found += _frame_checks(f, items, verdict, error)
    _mark_missing(found)
    return found


def _mark_missing(found: list[dict]) -> None:
    """A product the judge saw on no answered frame but the close-ups is
    missing: its first such check gets one defect, ``MISSING``. A product
    not seen in a close-up is fine."""
    for product in dict.fromkeys(c["product"] for c in found):
        wide = [
            c
            for c in found
            if c["product"] == product and c["checked"] and not c["closeup"]
        ]
        if wide and not any(c["visible"] for c in wide):
            wide[0].update(
                publish=False, imperfections=1, findings=[MISSING], minor=[]
            )


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
    late or failing) the verdict is an error: retryable. Minor findings of
    passed checks only add imperfections (``_minor``).
    """
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
    """``details["frames"]``: the judged frames without their images, each
    with the view the judge saw (None: no answer)."""
    return [
        {
            "index": f.index,
            "second": round(f.second, 1),
            "view": f.view,
            "closeup": f.closeup,
        }
        for f in frames
    ]
