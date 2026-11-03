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

"""Per-user A2UI context and answer cards."""

from __future__ import annotations

import time
from typing import Any

from app.auth.identity import acting_user_id

A2UI_VERSION = "v0.9.1"
A2UI_CATALOG_ID = "https://a2ui.org/specification/v0_9_1/catalogs/adk-voice-agent/catalog.json"

STAGE_TTL_S = 90.0

MAX_CARD_ITEMS = 20
MAX_CARD_TEXT = 300

_ATTENDEE = "attendee"
# No message bodies or file contents: a thread's cards are stored with the thread.
CARD_KINDS: dict[str, dict[str, type | str]] = {
    "events": {
        "id": str,
        "summary": str,
        "start": str,
        "end": str,
        "all_day": bool,
        "location": str,
        "attendees": _ATTENDEE,
        "html_link": str,
    },
    "threads": {
        "id": str,
        "subject": str,
        "from": str,
        "date": str,
        "snippet": str,
        "unread": bool,
        "message_count": int,
    },
    "files": {
        "id": str,
        "name": str,
        "mime_type": str,
        "modified_time": str,
        "web_view_link": str,
    },
}
_ATTENDEE_FIELDS = ("email", "response")


def _card_value(kind: type | str, value: Any, *, name: str = "") -> Any:
    if kind == _ATTENDEE:
        if not isinstance(value, list):
            return None
        people = [
            {
                k: p[k][:MAX_CARD_TEXT]
                for k in _ATTENDEE_FIELDS
                if isinstance(p.get(k), str)
            }
            for p in value[:MAX_CARD_ITEMS]
            if isinstance(p, dict)
        ]
        return [p for p in people if p.get("email")]
    if kind is str:
        if not isinstance(value, str):
            return None
        if name.endswith("_link") and not value.lower().startswith("https://"):
            return None
        return value[:MAX_CARD_TEXT]
    if kind is int:
        is_int = isinstance(value, int) and not isinstance(value, bool)
        return value if is_int else None
    return value if isinstance(value, bool) else None


def card_items(kind: str, raw_items: Any) -> list[dict[str, Any]]:
    """Trim `raw_items` to `kind`'s allowlisted fields, dropping items without `id`."""
    fields = CARD_KINDS.get(kind)
    if fields is None:
        raise ValueError(
            f"unknown card kind {kind!r}: use one of {', '.join(CARD_KINDS)}"
        )
    if not isinstance(raw_items, list):
        raise ValueError("items should be a list")
    items: list[dict[str, Any]] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        item = {
            name: value
            for name, field_kind in fields.items()
            if (value := _card_value(field_kind, raw.get(name), name=name))
            not in (None, [])
        }
        if item.get("id"):
            items.append(item)
        if len(items) == MAX_CARD_ITEMS:
            break
    return items


_dismissed: dict[str, set[str]] = {}
_cards: dict[str, tuple[dict[str, Any], float]] = {}


def record_context_card(
    *,
    kind: str,
    title: str,
    subtitle: str,
    brand_icon: str,
    bullets: list[str],
    surface_id: str,
    items: list[dict[str, Any]] | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    user_id = acting_user_id()
    _dismissed.setdefault(user_id, set()).discard(surface_id)
    surface = {
        "version": A2UI_VERSION,
        "surfaceId": surface_id,
        "catalogId": A2UI_CATALOG_ID,
        "kind": "context_card",
        "subkind": kind,
        "slot": "stage",
        "harness": brand_icon,
        "title": title,
        "subtitle": subtitle,
        "dataModel": {
            "context": {
                "brandIcon": brand_icon,
                "bullets": bullets,
                "items": items or [],
                "meta": meta or {},
            }
        },
    }
    _cards[user_id] = (surface, time.time() + STAGE_TTL_S)


def active_context_cards() -> list[dict[str, Any]]:
    user_id = acting_user_id()
    entry = _cards.get(user_id)
    if entry is None:
        return []
    card, expires_at = entry
    if (
        card["surfaceId"] in _dismissed.get(user_id, set())
        or time.time() >= expires_at
    ):
        return []
    return [card]


def dismiss_context_card(surface_id: str) -> None:
    _dismissed.setdefault(acting_user_id(), set()).add(surface_id)
