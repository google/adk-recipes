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

"""The studio's patch rules: how a patch or an app action changes a studio
state (a plain dict), with nothing stored. ``studio.state`` stores the
result.

Patches and actions are ``{"<op>": {...}}``, applied by ``apply_patch``:

- ``capability``: the page to show;
- ``settings``: settings to merge, only the known keys of each page
  (``SETTING_KEYS``) with valid values; an action names the ``page`` its
  keys are for. The try-on options (``TRYON_OPTIONS``) are booleans
  ``complete_look`` and ``upscale_crops``;
- ``set``: ``{field, item}`` for person or reference_model, or
  ``{field, items}`` to replace the product or spin list;
- ``add``: ``{field, item, replace_index}`` for product or spin;
- ``remove``: ``{field, index}``; ``clear``: ``{field}``;
- ``job_started``: ``{job_id, capability}``, a generation to follow (the
  results of its page, and Video Try-On's images, are cleared), or with
  ``tryon_image`` a "Make video" of one of Video Try-On's images (nothing
  cleared).

Items are ``{label, ref}``; the app finds or fetches thumbnails itself.
A label is one short line (``events.one_line``); an action's
``description`` is ignored (the server describes images itself).

The server alone writes the results of each page (``results``) and the
try-on images a video try-on animated (``images``, ``IMAGE_KEYS``), each
video naming its image (``source``); ``replace_videos`` puts the videos of
a "Make video" in place of that image's. ``upgrade`` maps studios saved
with the retired "Animate an image" start.
"""

from __future__ import annotations

import copy
from typing import Any

from genmedia4commerce.core.errors import InputError
from genmedia4commerce.studio.events import (
    CAPABILITY_NAMES,
    MAX_LABEL,
    one_line,
)

__all__ = [
    "CAPABILITIES",
    "IMAGE_KEYS",
    "LIST_FIELDS",
    "MAX_ITEMS",
    "RESULT_KEYS",
    "SETTING_KEYS",
    "SLOT_FIELDS",
    "TRYON_KEYS",
    "TRYON_OPTIONS",
    "VIDEO_KEYS",
    "apply_patch",
    "check_action",
    "clean",
    "index_of",
    "job_id_of",
    "per_page",
    "replace_videos",
    "settings_of",
    "state_key",
    "tryon_options",
    "upgrade",
]

CAPABILITIES = tuple(CAPABILITY_NAMES)
SLOT_FIELDS = ("person", "reference_model")
LIST_FIELDS = ("product", "spin")
_STATE_KEYS = {"product": "products", "spin": "spin"}
#: The try-on options of the reference model, with their defaults.
TRYON_OPTIONS = {"complete_look": True, "upscale_crops": False}
#: The values of the settings that take a fixed set of them.
_CHOICES: dict[str, tuple[Any, ...]] = {
    "complete_look": (True, False),
    "upscale_crops": (True, False),
}
#: Settings kept on one page whichever page sets them.
TRYON_KEYS = ("scenario", "style_guidelines", *TRYON_OPTIONS)
VIDEO_KEYS = ("motion", "prompt")
#: The settings each page keeps; nothing else is ever stored.
_VIDEO = ("aspect_ratio", "resolution", "num_variations")
SETTING_KEYS = {
    "tryon": (*TRYON_KEYS, "aspect_ratio", "image_size", "num_variations"),
    "video_tryon": (*VIDEO_KEYS, *_VIDEO),
    "spin": _VIDEO,
}
OPS: tuple[str, ...]
OPS = ("capability", "settings", "set", "add", "remove", "clear")
OPS += ("job_started",)
#: The ops whose value is one name, not an object.
_NAMED = {"capability": CAPABILITIES}
#: What an action may carry besides its one op.
_ACTION_KEYS = ("source", "description", "page")
_ITEM_KEYS = ("label", "ref")
#: A result; a video's ``source`` is the try-on image it was made from;
#: ``fixed``: corrected by the try-on's fix pass.
RESULT_KEYS: tuple[str, ...]
RESULT_KEYS = ("index", "status", "score", "uri", "view", "note", "run")
RESULT_KEYS += ("source", "fixed")
#: A try-on image a video try-on animated: ``passed`` its checks, and
#: ``back``, the back view its videos used (for the server's "Make video");
#: ``fixed`` as for a result.
IMAGE_KEYS: tuple[str, ...]
IMAGE_KEYS = ("index", "status", "score", "uri", "note", "run", "passed")
IMAGE_KEYS += ("back", "fixed")
_PLAIN = (str, int, float, bool)
#: The most items of each list: try-on products (the try-on's limit) and
#: spin photos (the spin's).
MAX_ITEMS = {"product": 20, "spin": 8}
_MAX_TEXT = 2000


def _valid(key: str, v: Any) -> bool:
    """Whether ``v`` is a value the setting takes (any plain value for the
    settings without a fixed set; a bool is never taken for a number)."""
    if not isinstance(v, _PLAIN):
        return False
    choices = _CHOICES.get(key)
    return choices is None or any(
        type(v) is type(c) and v == c for c in choices
    )


def tryon_options(settings: Any) -> dict[str, Any]:
    """The try-on options of the Photo Try-On settings, with the defaults
    for the unset or invalid ones."""
    s = settings if isinstance(settings, dict) else {}
    return {
        k: s[k] if _valid(k, s.get(k)) else default
        for k, default in TRYON_OPTIONS.items()
    }


def settings_of(st: dict[str, Any], cap: str) -> dict[str, Any]:
    """A page's settings in a studio state (created when missing)."""
    return st.setdefault("settings", {}).setdefault(cap, {})


def state_key(field_name: str) -> str:
    """The state key of a field (``product`` is kept as ``products``)."""
    return _STATE_KEYS.get(field_name, field_name)


def clean(obj: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    """The plain values (strings, numbers, booleans) of ``obj`` under the
    given keys; nothing else of ``obj`` is ever stored."""
    out: dict[str, Any] = {}
    for k in keys:
        v = obj.get(k) if isinstance(obj, dict) else None
        if isinstance(v, _PLAIN):
            out[k] = v[:_MAX_TEXT] if isinstance(v, str) else v
    return out


def item_of(obj: Any) -> dict[str, Any] | None:
    item = clean(obj, _ITEM_KEYS)
    if "label" in item:
        item["label"] = one_line(item["label"], MAX_LABEL)
    return item if isinstance(item.get("ref"), str) and item["ref"] else None


def index_of(v: Any) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) else -1


def per_page(st: dict[str, Any], key: str) -> dict[str, Any]:
    if not isinstance(st.get(key), dict):
        st[key] = {}
    return st[key]


def _list(st: dict[str, Any], field_name: str) -> list[Any]:
    key = state_key(field_name)
    if not isinstance(st.get(key), list):
        st[key] = []
    return st[key]


def job_id_of(st: dict[str, Any]) -> Any:
    job = st.get("job")
    return job.get("job_id") if isinstance(job, dict) else None


def _page_of(key: str, page: Any) -> Any:
    """The page a setting is kept on (``page``: the action's, or the
    studio's page for a patch)."""
    if key in TRYON_KEYS:
        return "tryon"
    return "video_tryon" if key in VIDEO_KEYS else page


def _known(values: Any, page: Any) -> dict[str, tuple[str, Any]]:
    """``{key: (page, value)}`` of the known settings with plain values."""
    out = {}
    for k, v in (values if isinstance(values, dict) else {}).items():
        cap = _page_of(k, page)
        if cap in CAPABILITIES and k in SETTING_KEYS[cap] and _valid(k, v):
            out[k] = (cap, v[:_MAX_TEXT] if isinstance(v, str) else v)
    return out


def _items(st: dict[str, Any], p: dict[str, Any]) -> None:
    """The ``set``, ``add``, ``remove`` and ``clear`` ops of a patch."""
    if isinstance(put := p.get("set"), dict):
        name = put.get("field")
        if name in LIST_FIELDS and isinstance(put.get("items"), list):
            items = [i for i in map(item_of, put["items"]) if i]
            st[state_key(name)] = items[: MAX_ITEMS[name]]
        elif name in SLOT_FIELDS and (item := item_of(put.get("item"))):
            st[name] = item
    add = p.get("add")
    add = add if isinstance(add, dict) else {}
    if add.get("field") in LIST_FIELDS and (item := item_of(add.get("item"))):
        items, i = _list(st, add["field"]), index_of(add.get("replace_index"))
        if 0 <= i < len(items):
            items[i] = item
        elif len(items) < MAX_ITEMS[add["field"]]:
            items.append(item)
    rm = p.get("remove")
    if isinstance(rm, dict) and rm.get("field") in LIST_FIELDS:
        items, i = _list(st, rm["field"]), index_of(rm.get("index"))
        if 0 <= i < len(items):
            items.pop(i)
    if isinstance(clear := p.get("clear"), dict):
        name = clear.get("field")
        if name in LIST_FIELDS:
            st[state_key(name)] = []
        elif name in SLOT_FIELDS:
            st[name] = None


def _start_job(st: dict[str, Any], job: Any) -> None:
    """Follow a new generation; one that already ended is ignored. A full
    run clears its page's results (and Video Try-On's images); a "Make
    video" (``tryon_image``) clears nothing."""
    jid = job.get("job_id") if isinstance(job, dict) else None
    cap = job.get("capability") if isinstance(job, dict) else None
    if not (isinstance(jid, str) and jid and cap in CAPABILITIES):
        return
    if jid in (st.get("finished_jobs") or []) or job_id_of(st) == jid:
        return
    followed = {"job_id": jid[:64], "capability": cap}
    image = job.get("tryon_image")
    if cap == "video_tryon" and isinstance(image, str) and image:
        followed["tryon_image"] = image[:_MAX_TEXT]
    else:
        per_page(st, "results")[cap] = []
        if cap == "video_tryon":
            per_page(st, "images")[cap] = []
    st["job"] = followed


def replace_videos(
    st: dict[str, Any], image: str, videos: list[dict[str, Any]]
) -> None:
    """Put ``videos`` (a "Make video" of try-on image ``image``) in place of
    that image's Video Try-On videos, numbered above every index there so
    indices stay unique; the other videos are kept."""
    page = per_page(st, "results")
    kept = [r for r in page.get("video_tryon") or [] if isinstance(r, dict)]
    top = max((index_of(r.get("index")) for r in kept), default=-1)
    new = [
        {**clean(v, RESULT_KEYS), "index": top + 1 + n, "source": image}
        for n, v in enumerate(videos)
    ]
    page["video_tryon"] = [r for r in kept if r.get("source") != image] + new


def upgrade(st: dict[str, Any]) -> None:
    """Map a studio saved with the retired "Animate an image" start, in
    place: its ``video_start`` and ``tryon_image`` dropped, and its
    ``source`` (the image its videos were made from) as Video Try-On's one
    image when it has no ``images``, named as the ``source`` of its videos
    that name none (so a "Make video" of it replaces them)."""
    st.pop("video_start", None)
    st.pop("tryon_image", None)
    source = st.pop("source", None)
    old = source.get("video_tryon") if isinstance(source, dict) else None
    uri = old.get("uri") if isinstance(old, dict) else None
    if not (isinstance(uri, str) and uri):
        return
    images = per_page(st, "images")
    if "video_tryon" not in images:
        image = clean(old, IMAGE_KEYS)
        passed = image.get("status") == "ready"
        images["video_tryon"] = [{**image, "passed": passed}]
        for video in per_page(st, "results").get("video_tryon") or []:
            if isinstance(video, dict) and not video.get("source"):
                video["source"] = image["uri"]


def apply_patch(st: dict[str, Any], p: Any) -> None:
    """Apply one patch or action to a studio state; unknown fields, settings
    and values are ignored."""
    if not isinstance(p, dict):
        return
    if p.get("capability") in CAPABILITIES:
        st["capability"] = p["capability"]
    page = p.get("page") or st.get("capability")
    for k, (cap, v) in _known(p.get("settings"), page).items():
        settings_of(st, cap)[k] = v
    _items(st, p)
    _start_job(st, p.get("job_started"))


REFERENCE_MODEL_HINT = (
    "The reference model is a photo the user uploads of a model already "
    "wearing the look; gallery models and catalogue photos cannot be used."
)


#: Where the Studio copies catalogue images in the media bucket
#: (``studio.media.COPIES_PREFIX``, repeated: this module imports nothing).
_CATALOGUE_COPIES = "/runs/studio_catalog/"


def is_catalogue_image(ref: str) -> bool:
    """A catalogue ref or the Studio's bucket copy of one."""
    return ref.startswith("catalog:") or _CATALOGUE_COPIES in ref


def _check_reference_model(op: str, value: Any) -> None:
    """A reference model is the user's own look photo: never a catalogue
    image (a gallery model or a catalogue product)."""
    item = (
        value.get("item") if op == "set" and isinstance(value, dict) else None
    )
    if (
        isinstance(item, dict)
        and value.get("field") == "reference_model"
        and is_catalogue_image(str(item.get("ref", "")))
    ):
        raise InputError(REFERENCE_MODEL_HINT)


def check_action(action: Any) -> dict[str, Any]:
    """A copy of an app action: one known op with a valid value, and only
    the known extra keys and settings."""
    keys = set(action) if isinstance(action, dict) else set()
    ops = [k for k in OPS if k in keys]
    if len(ops) != 1 or keys - set(OPS) - set(_ACTION_KEYS):
        raise InputError(f"An action is one of {', '.join(OPS)}.")
    op, value = ops[0], action[ops[0]]
    if op in _NAMED and value not in _NAMED[op]:
        raise InputError(f"Not a valid {op} action.")
    if op not in _NAMED and not (isinstance(value, dict) and value):
        raise InputError(f"Not a valid {op} action.")
    action = copy.deepcopy(action)
    # Never the client's words about an image: the server's own
    # descriptions are used (``studio.state``).
    action.pop("description", None)
    action["source"] = one_line(action.get("source"), 40)
    _check_reference_model(op, value)
    if op == "settings":
        known = _known(value, action.get("page"))
        if not known:
            raise InputError("No known setting in this action.")
        action["settings"] = {k: v for k, (_, v) in known.items()}
    return action
