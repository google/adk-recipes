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

"""The parts of a Studio job document that ``studio.jobs`` folds its
workflow's events into: the results, their order, the try-on images of a
video try-on, and the counts of the front variations expected and
failed (``expected`` and ``failed``, ``{"image_results": n, "results":
n}``)."""

from __future__ import annotations

from typing import Any

from genmedia4commerce.studio import media as sm
from genmedia4commerce.studio import patches as rules

__all__ = [
    "add_image",
    "bump",
    "expected",
    "ranked",
    "record_animated",
    "result",
    "source",
]


def _variations(spec: dict[str, Any]) -> int:
    """The spec's ``settings.num_variations`` as an int, at least 1 (its
    page's default when unset: ``patches.VARIATIONS``)."""
    raw = (spec.get("settings") or {}).get("num_variations")
    raw = raw or rules.variations(spec.get("capability"))
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 1


def expected(spec: dict[str, Any]) -> dict[str, int]:
    """How many front variations a job makes in each list when it starts:
    a video try-on from photos makes its try-on images first, and its
    videos are added once it picks the image to animate (``bump``)."""
    n = _variations(spec)
    photos = spec.get("capability") == "video_tryon"
    photos = photos and not spec.get("tryon_image")
    return {"image_results": n if photos else 0, "results": 0 if photos else n}


def bump(job: dict[str, Any], name: str, key: str, n: int = 1) -> None:
    """Add ``n`` to count ``key`` of ``job[name]`` (``expected`` or
    ``failed``; a document saved before these counts has none)."""
    counts = job.setdefault(name, {"image_results": 0, "results": 0})
    counts[key] = counts.get(key, 0) + n


def result(v: dict[str, Any], video: bool) -> dict[str, Any] | None:
    """One variation for the app and the history; ``None`` without media.
    One made with its checks off has no score (``types.unchecked``); a
    spin's MP4 (``video_uri``) gets its own ``video_url``."""
    uri = v.get("media_uri") or ""
    if not uri:
        return None
    verdict = v.get("verdict") or {}
    checked = (verdict.get("details") or {}).get("checked") is not False
    mp4 = v.get("video_uri") or ""
    return {
        "index": v.get("index"),
        "status": v.get("status"),
        "score": v.get("score") if checked else None,
        "uri": uri,
        "url": sm.preview_url(uri, video=video),
        "note": verdict.get("description", ""),
        "view": (v.get("extra") or {}).get("view") or "front",
        "fixed": bool(v.get("fixed")),
        **(
            {"video_uri": mp4, "video_url": sm.preview_url(mp4, inline=False)}
            if mp4
            else {}
        ),
    }


def ranked(results: list[dict], order: list[int], best: Any) -> list[dict]:
    """``results`` in the judge's order (unranked ones keep theirs, last),
    the back views, drawn from the best front, right after it: the judge
    ranks the fronts only, and fronts and back views are numbered apart."""
    rank = {i: n for n, i in enumerate(order)}
    backs = [r for r in results if r.get("view") == "back"]
    fronts = sorted(
        (r for r in results if r.get("view") != "back"),
        key=lambda r: rank.get(r["index"], len(rank)),
    )
    at = next(
        (n + 1 for n, r in enumerate(fronts) if r["index"] == best),
        len(fronts),
    )
    return [*fronts[:at], *backs, *fronts[at:]]


def _views(job: dict[str, Any], view: str) -> dict[Any, dict[str, Any]]:
    """The try-on images of one view by index (fronts and back views are
    numbered apart)."""
    return {
        r["index"]: r
        for r in job["image_results"]
        if (r.get("view") == "back") == (view == "back")
    }


def add_image(job: dict[str, Any], front: dict[str, Any]) -> None:
    """Show a video try-on's try-on image ``front`` in ``images``
    (``passed``: it passed its checks; ``back``: set once animated)."""
    image = {**front, "passed": front.get("status") == "ready", "back": ""}
    image.pop("view", None)
    job.setdefault("images", []).append(image)


def record_animated(job: dict[str, Any], data: dict[str, Any]) -> bool:
    """Record the back view of the try-on image a video try-on animates
    (``image_selected``) on that image; whether it is one of ``images``."""
    index = data.get("front_index")
    images = job.get("images") or []
    image = next((i for i in images if i.get("index") == index), None)
    if image is None:
        return False
    back = _views(job, "back").get(data.get("back_index")) or {}
    image["back"] = back.get("uri") or ""
    return True


def source(job: dict[str, Any], index: Any) -> str:
    """The try-on image a video was made from: a "Make video"'s image, the
    one image of a photo's video (no ``extra.source_index``), or the front
    of ``extra.source_index``."""
    if job.get("tryon_image"):
        return str(job["tryon_image"])
    images = job.get("images") or []
    if index is None and len(images) == 1:
        return str(images[0].get("uri") or "")
    return (_views(job, "front").get(index) or {}).get("uri", "")
