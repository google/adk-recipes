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

"""The generation tool a Studio job runs (``studio.jobs``), and its
arguments, from the job's spec: the studio's refs resolved, its settings
with their defaults. ``for_studio`` turns a client's spec into the one to
run (a "Make video" from the studio's own image, or a video of one of its
Photo Try-On results), ``check_refs`` refuses a spec naming images its
studio may not use, ``refresh_cache`` says whether the job skips the
preprocessing cache's reads ("Try again")."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from genmedia4commerce.core import gcs, media
from genmedia4commerce.core.errors import InputError
from genmedia4commerce.core.settings import get_settings
from genmedia4commerce.studio import media as sm
from genmedia4commerce.studio import patches as rules
from genmedia4commerce.studio import store
from genmedia4commerce.workflows.tryon import framing

__all__ = [
    "ANIMATING",
    "NEW_IMAGE",
    "NOT_AN_IMAGE",
    "check_refs",
    "for_studio",
    "image_of",
    "photo_of",
    "refresh_cache",
    "tool_call",
]

_TOOLS = {"tryon": "try_on", "video_tryon": "video_try_on", "spin": "spin_360"}
#: The spec's image fields: one ref each, or a list of refs.
_ONE_REF = ("person", "reference_model", "tryon_image", "back_image")
_REF_LISTS = ("products", "spin")
#: Set by the server only, from the studio's image (``for_studio``).
_SERVER_KEYS = ("tryon_image", "back_image", "source_run", "image", "progress")
NOT_AN_IMAGE = "Choose one of this studio's try-on images."
#: The first progress line of a video of a Photo Try-On result.
ANIMATING = "Animating your photo"
NEW_IMAGE = "Making a 9:16 image first"

logger = logging.getLogger(__name__)


def image_of(st: dict[str, Any], uri: Any) -> dict[str, Any] | None:
    """The item of the studio's video try-on images (``images.video_tryon``
    of state ``st``) whose ``uri`` is ``uri``, or ``None``."""
    if not (isinstance(uri, str) and uri):
        return None
    images = (st.get("images") or {}).get("video_tryon") or []
    return next(
        (i for i in images if isinstance(i, dict) and i.get("uri") == uri),
        None,
    )


def photo_of(st: dict[str, Any], uri: Any) -> dict[str, Any] | None:
    """The front view of the studio's Photo Try-On results
    (``results.tryon`` of state ``st``) whose ``uri`` is ``uri``, or
    ``None``."""
    if not (isinstance(uri, str) and uri):
        return None
    results = (st.get("results") or {}).get("tryon") or []
    return next(
        (
            r
            for r in results
            if isinstance(r, dict)
            and r.get("uri") == uri
            and r.get("view") != "back"
        ),
        None,
    )


def for_studio(spec: dict[str, Any], st: dict[str, Any]) -> dict[str, Any]:
    """The spec to run for a client's ``spec`` in studio state ``st``.

    A Video Try-On spec with a ``tryon_image`` is a "Make video": that
    image must be one of the studio's video try-on images
    (``images.video_tryon``) or Photo Try-On front results
    (``_from_photo``), else ``InputError``, and its run record
    (``source_run``), its back view and ``GENMEDIA_VIDEOS_PER_IMAGE``
    videos come from the server, whatever the client sent. Any other spec
    is a full run: ``tryon_image``, ``back_image`` and ``source_run`` are
    dropped.
    """
    uri = spec.get("tryon_image")
    out = {k: v for k, v in spec.items() if k not in _SERVER_KEYS}
    if spec.get("capability") != "video_tryon" or not uri:
        return out
    image = image_of(st, uri)
    if image is None:
        return _from_photo(spec, st, out)
    return {**out, **_videos(spec, uri, image.get("back"), image.get("run"))}


def _videos(spec: dict[str, Any], uri: str, back: Any, run: Any) -> dict:
    """The server's part of a spec animating image ``uri``."""
    settings = spec.get("settings")
    settings = dict(settings) if isinstance(settings, dict) else {}
    settings["num_variations"] = get_settings().videos_per_image
    return {
        "tryon_image": uri,
        "back_image": str(back or ""),
        "source_run": str(run or ""),
        "settings": settings,
    }


def _from_photo(
    spec: dict[str, Any], st: dict[str, Any], out: dict[str, Any]
) -> dict[str, Any]:
    """The spec of a video of Photo Try-On front result ``tryon_image``.

    The photo is 3:4, the video 9:16. A full-body photo (the framing of its
    run record) whose single garment's span, if any, shows the head (e.g.
    head to knees) is cut to 9:16 (``_video_frame``) and that image animated,
    as the job's one image (``image``): like a full run, the job replaces
    Video Try-On's images and videos. Any other is a full run from the
    studio's inputs (``out``), which makes a 9:16 try-on image first.
    ``progress`` says which.
    """
    uri, studio_id = spec["tryon_image"], str(spec.get("studio_id") or "")
    photo = photo_of(st, uri)
    if photo is None or not sm.allowed_ref(uri, studio_id):
        raise InputError(NOT_AN_IMAGE)
    run = str(photo.get("run") or "")
    if not _animates(run, studio_id):
        return {**out, "progress": NEW_IMAGE}
    frame = _video_frame(uri)
    results = (st.get("results") or {}).get("tryon") or []
    back = next(
        (
            str(r.get("uri") or "")
            for r in results
            if isinstance(r, dict)
            and r.get("view") == "back"
            and r.get("run") == run
        ),
        "",
    )
    image = rules.clean(photo, rules.IMAGE_KEYS) | {
        "index": 0,
        "uri": frame,
        "url": sm.preview_url(frame),
        "passed": photo.get("status") == "ready",
        "back": back,
    }
    return {
        **out,
        **_videos(spec, frame, back, run),
        "image": image,
        "progress": ANIMATING,
    }


def _animates(run: str, studio_id: str) -> bool:
    """Try-on run record ``run`` of studio ``studio_id`` is framed full body
    (``Classification.framing``) and its single product's span, if any,
    shows the head (``tryon.framing``); False when unknown."""
    if not (run and sm.allowed_record(run, studio_id)):
        return False
    try:
        c = json.loads(gcs.download(run))["classification"]
        single = c.get("mode") == "single"
        products = (c.get("products") or []) if single else []
        span = products[0].get("span") if len(products) == 1 else None
        return c["framing"] == "full_body" and (
            not span or framing.in_frame(span, "head")
        )
    except Exception as exc:  # no record, or not one: a full run then
        logger.warning("No framing in %s: %s", run, exc)
        return False


def _video_frame(uri: str) -> str:
    """The centre of image ``uri`` cut to 9:16 at its full height, saved
    next to it (``-9x16.png``); ``uri`` itself when it is no wider."""
    data = gcs.download(uri)
    width, height = media.image_size(data)
    new = round(height * 9 / 16)
    if new >= width:
        return uri
    left = (width - new) // 2
    cut = media.crop_box(data, (left, 0, left + new, height))
    bucket = get_settings().media_bucket
    path = re.sub(r"\.\w+$", "-9x16.png", uri.removeprefix(f"gs://{bucket}/"))
    return gcs.upload(cut, bucket=bucket, path=path, content_type="image/png")


def check_refs(spec: dict[str, Any], studio_id: str) -> None:
    """``InputError`` unless every image of ``spec`` is one studio
    ``studio_id`` may use (``media.allowed_ref``: a catalogue image, a
    catalogue copy or a file of its own runs; refs of older studios mapped
    first, as ``tool_call`` does) and its ``source_run`` is one of its own
    try-on records."""
    refs = [spec.get(key) or "" for key in _ONE_REF]
    for key in _REF_LISTS:
        values = spec.get(key) or []
        if not isinstance(values, list):
            raise InputError(f"{key} must be a list of image refs")
        refs += values
    for ref in refs:
        if ref == "":
            continue
        mapped = sm.resolve(ref) if isinstance(ref, str) else ref
        if not (isinstance(mapped, str) and sm.allowed_ref(mapped, studio_id)):
            raise InputError(f"Not an allowed image: {str(ref)[:200]}")
    run = spec.get("source_run") or ""
    if run and not sm.allowed_record(run, studio_id):
        raise InputError("source_run must be a try-on record of this studio")


def refresh_cache(spec: dict[str, Any]) -> bool:
    """Whether the job recomputes and replaces what the preprocessing
    cache keeps (``core.cache.refreshing``) instead of reading it: the
    spec's ``refresh_cache`` is ``true`` (the "Try again" of a failed
    generation)."""
    return spec.get("refresh_cache") is True


def _str(spec: dict[str, Any], key: str, default: str = "") -> str:
    return str((spec.get("settings") or {}).get(key) or default)


def _motion(spec: dict[str, Any]) -> str:
    """The video motion: custom when it has a prompt, else auto (also for
    studios saved with the old catwalk and turnaround motions)."""
    custom = _str(spec, "motion") == "custom" and _str(spec, "prompt").strip()
    return "custom" if custom else "auto"


def tool_call(spec: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """The generation tool's name and its arguments for one Studio spec;
    its checks off unless the spec's settings turn them on."""
    cap = spec.get("capability")
    checks = rules.tryon_options(spec.get("settings"), rules.CHECKS)
    count = rules.variations(cap)
    common: dict[str, Any] = {
        "num_variations": int(_str(spec, "num_variations", count)),
        "aspect_ratio": _str(spec, "aspect_ratio"),
        "output": "gcs",
        "session_id": store.session_id(str(spec.get("studio_id") or "")),
    }
    if cap == "spin":
        common["aspect_ratio"] = common["aspect_ratio"] or "auto"
        refs = [sm.resolve(r) for r in spec.get("spin") or []]
        return _TOOLS["spin"], {
            **common,
            "product_images_base64": refs,
            "resolution": _str(spec, "resolution", "720p"),
            "check_videos": checks["check_videos"],
        }
    people = {
        "product_images_base64": [
            sm.resolve(r) for r in spec.get("products") or []
        ],
        "person_image_base64": sm.resolve(spec.get("person") or ""),
        "reference_model_image_base64": sm.resolve(
            spec.get("reference_model") or ""
        ),
        "style_guidelines": _str(spec, "style_guidelines"),
        "scenario": _str(spec, "scenario"),
        "image_size": _str(spec, "image_size", "2K"),
        **rules.tryon_options(spec.get("settings")),
    }
    if cap == "video_tryon":
        common["aspect_ratio"] = common["aspect_ratio"] or "9:16"
        tryon_image = str(spec.get("tryon_image") or "")
        return _TOOLS["video_tryon"], {
            **common,
            **people,
            "tryon_image_base64": sm.resolve(tryon_image),
            "back_image_base64": sm.resolve(spec.get("back_image") or ""),
            # The try-on run that made the image: its products, not the
            # studio's (``tools.video_try_on``).
            "source_run": str(spec.get("source_run") or "")
            if tryon_image
            else "",
            "motion": _motion(spec),
            "prompt": _str(spec, "prompt"),
            "resolution": _str(spec, "resolution", "720p"),
            **checks,
        }
    common["aspect_ratio"] = common["aspect_ratio"] or "3:4"
    tryon = {**common, **people, "check_images": checks["check_images"]}
    return _TOOLS["tryon"], tryon
