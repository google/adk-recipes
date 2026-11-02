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
run (a "Make video" from the studio's own image), ``check_refs`` refuses a
spec naming images its studio may not use, ``refresh_cache`` says whether
the job skips the preprocessing cache's reads ("Try again")."""

from __future__ import annotations

from typing import Any

from genmedia4commerce.core.errors import InputError
from genmedia4commerce.core.settings import get_settings
from genmedia4commerce.studio import media as sm
from genmedia4commerce.studio import patches as rules
from genmedia4commerce.studio import store

__all__ = [
    "NOT_AN_IMAGE",
    "check_refs",
    "for_studio",
    "image_of",
    "refresh_cache",
    "tool_call",
]

_TOOLS = {"tryon": "try_on", "video_tryon": "video_try_on", "spin": "spin_360"}
#: The spec's image fields: one ref each, or a list of refs.
_ONE_REF = ("person", "reference_model", "tryon_image", "back_image")
_REF_LISTS = ("products", "spin")
#: Set by the server only, from the studio's image (``for_studio``).
_SERVER_KEYS = ("tryon_image", "back_image", "source_run")
NOT_AN_IMAGE = "Choose one of this studio's video try-on images."


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


def for_studio(spec: dict[str, Any], st: dict[str, Any]) -> dict[str, Any]:
    """The spec to run for a client's ``spec`` in studio state ``st``.

    A Video Try-On spec with a ``tryon_image`` is a "Make video": that
    image must be one of the studio's video try-on images
    (``images.video_tryon``, else ``InputError``), and its run record
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
        raise InputError(NOT_AN_IMAGE)
    settings = spec.get("settings")
    settings = dict(settings) if isinstance(settings, dict) else {}
    settings["num_variations"] = get_settings().videos_per_image
    return {
        **out,
        "tryon_image": uri,
        "back_image": str(image.get("back") or ""),
        "source_run": str(image.get("run") or ""),
        "settings": settings,
    }


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
    """The generation tool's name and its arguments for one Studio spec."""
    cap = spec.get("capability")
    common: dict[str, Any] = {
        "num_variations": int(_str(spec, "num_variations", "1")),
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
        }
    common["aspect_ratio"] = common["aspect_ratio"] or "3:4"
    return _TOOLS["tryon"], {**common, **people}
