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

"""App events and generations of the Studio, as one sentence each for the
agent's history.

Every sentence carries the ``gs://`` (or ``catalog:``) reference of the
images involved, so the agent can reuse them later. Text that comes from a
client (labels, search queries) goes in as one short line (``one_line``),
and image descriptions only as the server wrote them.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = [
    "CAPABILITY_NAMES",
    "FIELD_NAMES",
    "ended_note",
    "finished_note",
    "narrate",
    "narrate_action",
    "one_line",
    "started_note",
]

FIELD_NAMES = {
    "person": "Person",
    "reference_model": "Reference model",
    "product": "Products",
    "spin": "Spin photos",
}
CAPABILITY_NAMES = {
    "tryon": "Photo Try-On",
    "video_tryon": "Video Try-On",
    "spin": "360° Spin",
}
_SOURCES = {"gallery": "from the gallery", "catalog": "from the catalogue"}
MAX_LABEL = 80
MAX_QUERY = 200
MAX_DESCRIPTION = 300
#: Control characters (line breaks included) and runs of spaces.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]+")
_SPACES = re.compile(r"\s{2,}")


def one_line(text: Any, limit: int) -> str:
    """``text`` as one line of at most ``limit`` characters: control
    characters and line breaks become spaces."""
    flat = _CONTROL.sub(" ", str(text or ""))
    return _SPACES.sub(" ", flat).strip()[:limit]


def _item(ev: dict[str, Any]) -> tuple[str, str]:
    item = ev.get("item") or {}
    label = one_line(item.get("label"), MAX_LABEL) or "an image"
    return label, one_line(item.get("ref"), 500)


def _where(ev: dict[str, Any]) -> str:
    field = str(ev.get("field") or "")
    return FIELD_NAMES.get(field, field or "the studio")


def _search(ev: dict[str, Any]) -> str:
    top = [
        f"{i + 1}) {one_line(it.get('label', '?'), MAX_LABEL)}"
        f" ({it.get('ref', '')})"
        for i, it in enumerate((ev.get("items") or [])[:3])
    ]
    found = "; ".join(top) or "no results"
    query = one_line(ev.get("query"), MAX_QUERY)
    return f"User searched the catalogue for {query!r}: {found}."


def narrate(ev: dict[str, Any]) -> str | None:
    """One sentence for an app event, or ``None`` for unknown events."""
    kind = ev.get("type")
    label, ref = _item(ev)
    where = _where(ev)
    desc = one_line(ev.get("description"), MAX_DESCRIPTION)
    if kind == "upload":
        about = f": {desc}" if desc else ""
        return f"User uploaded {label} in {where}{about} ref: {ref}"
    if kind == "select":
        src = _SOURCES.get(str(ev.get("source")), "")
        about = f" ({desc})" if desc else ""
        return (
            f"User picked {label}{about} {src} as {where}. ref: {ref}".replace(
                "  ", " "
            )
        )
    if kind == "remove":
        return f"User removed {label} from {where}. ref: {ref}"
    if kind == "clear":
        return f"User cleared {where}."
    if kind == "page":
        cap = ev.get("capability", "")
        return f"User switched to the {CAPABILITY_NAMES.get(cap, cap)} page."
    if kind == "settings":
        return f"User set {ev.get('key')} to {ev.get('value')!r}."
    if kind == "search":
        return _search(ev)
    return None


def _action_events(
    action: dict[str, Any], removed: Any, descriptions: dict[str, str]
) -> list[dict[str, Any]]:
    """The app events (as ``narrate`` takes them) of one checked action;
    image descriptions only from ``descriptions`` (written by the server,
    never the client's)."""
    source = action.get("source")

    def picked(name: Any, item: Any) -> dict[str, Any]:
        item = item if isinstance(item, dict) else {}
        ref = str(item.get("ref") or "")
        return {
            "type": "upload" if source == "upload" else "select",
            "source": source,
            "field": name,
            "item": item,
            "description": descriptions.get(ref, ""),
        }

    put, add = action.get("set") or {}, action.get("add") or {}
    taken = action.get("remove") or action.get("clear") or {}
    if cap := action.get("capability"):
        return [{"type": "page", "capability": cap}]
    if settings := action.get("settings"):
        return [
            {"type": "settings", "key": k, "value": v}
            for k, v in settings.items()
        ]
    if item := put.get("item") or add.get("item"):
        return [picked(put.get("field") or add.get("field"), item)]
    if isinstance(put.get("items"), list):
        return [picked(put.get("field"), i) for i in put["items"]]
    if removed:
        return [
            {"type": "remove", "field": taken.get("field"), "item": removed}
        ]
    if "clear" in action:
        return [{"type": "clear", "field": taken.get("field")}]
    return []


def narrate_action(
    action: dict[str, Any], removed: Any, descriptions: dict[str, str]
) -> str:
    """The sentences for a user action of the app (``studio.state``):
    ``removed`` is the item a remove or clear took away, ``descriptions``
    the server's descriptions of catalogue copies and uploads by ``gs://``
    URI."""
    events = _action_events(action, removed, descriptions)
    return " ".join(t for t in map(narrate, events) if t)


def started_note(spec: dict[str, Any]) -> str:
    """One sentence with a generation's inputs and their refs."""
    cap = str(spec.get("capability"))
    if spec.get("tryon_image"):  # a "Make video" of one try-on image
        return f"Started the videos of try-on image {spec['tryon_image']}."
    parts = [f"Started {CAPABILITY_NAMES.get(cap, cap)}"]
    for key in ("person", "reference_model"):
        if spec.get(key):
            parts.append(f"{key}: {spec[key]}")
    for key in ("products", "spin"):
        if spec.get(key):
            parts.append(f"{key}: [{', '.join(spec[key])}]")
    settings = {k: v for k, v in (spec.get("settings") or {}).items() if v}
    if settings:
        parts.append(f"settings: {settings}")
    return "; ".join(parts) + "."


def finished_note(cap: str, results: list[dict[str, Any]]) -> str:
    """One sentence with a generation's outputs, best first."""

    def one(r: dict[str, Any]) -> str:
        score = (
            f"score {round(r['score'])}" if r.get("score") is not None else ""
        )
        note = (r.get("note") or "")[:120]
        details = ", ".join(x for x in (r.get("view", ""), score, note) if x)
        return f"{r['uri']} ({details})"

    best, *others = results
    text = f"{CAPABILITY_NAMES.get(cap, cap)} finished. Best: {one(best)}"
    if others:
        text += "; others: " + "; ".join(one(r) for r in others)
    return text + "."


def ended_note(cap: str, job: dict[str, Any]) -> str:
    """The note of a generation that ended: its outputs, or its error."""
    if job.get("status") == "done" and job.get("results"):
        return finished_note(cap, job["results"])
    return f"{CAPABILITY_NAMES.get(cap, cap)} failed: {job.get('error')}"
