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

"""USER profile backed by Memory Bank native Structured Profiles.

The dream-review pass generates a structured profile server-side (see
``app/memory/dream_review.py``); here we read it back via
``retrieve_profiles`` and render it for the system prompt.
``on_session_start_callback`` loads it once per session into
``state['user_profile']``, which ``render_user_profile`` formats into a
``## User Profile`` block in the volatile tier.

That block is re-sent every turn outside the cached prefix, so it carries the
narrative fields only — ``PreloadMemoryTool`` already retrieves the content the
lists duplicate. ``include_lists=True`` renders the whole profile for
``/lha/memories``. See ``docs/memory.md``.
"""

from __future__ import annotations

import logging
import re
from typing import Any

# engine_resource_name re-exported for callers/tests that referenced it here
# before the adapter boundary was introduced.
from app.memory.adapter import engine_resource_name, memory_adapter

logger = logging.getLogger(__name__)


def _clean_list(value: Any) -> list[str]:
    """Normalize a profile list field: scalars, bullets, blanks, dupes."""
    if isinstance(value, str):
        value = [value]
    elif not isinstance(value, list):
        return []
    seen: set[str] = set()
    cleaned: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        # Memory Bank sometimes emits facts pre-formatted as markdown bullets.
        text = re.sub(r"^(?:[-*+]\s+)+", "", item.strip()).strip()
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        cleaned.append(text)
    return cleaned


def _render_profile(
    profile: dict[str, Any], *, include_lists: bool = False
) -> str:
    """Render a structured profile dict into a readable block."""
    lines: list[str] = []
    summary = str(profile.get("summary") or "").strip()
    if summary:
        lines.append(summary)
    role = str(profile.get("role") or "").strip()
    style = str(profile.get("working_style") or "").strip()
    # A profile carrying only lists would otherwise render blank.
    show_lists = include_lists or not (summary or role or style)
    interests = _clean_list(profile.get("interests")) if show_lists else []
    facts = _clean_list(profile.get("durable_facts")) if show_lists else []
    fields: list[tuple[str, str]] = []
    if role:
        fields.append(("Role", role))
    if interests:
        fields.append(("Interests", ", ".join(interests)))
    if style:
        fields.append(("Working style", style))
    if fields or facts:
        if lines:
            lines.append("")
        lines.extend(f"- **{label}:** {value}" for label, value in fields)
        lines.extend(f"- {fact}" for fact in facts)
    return "\n".join(lines).strip()


async def load_user_profile(
    *,
    memory_service: Any,
    app_name: str,
    user_id: str,
    include_lists: bool = False,
) -> str:
    """Return the user's structured profile as text, or '' if none/unavailable."""
    if not app_name or not user_id:
        return ""
    profile = await memory_adapter(memory_service).retrieve_profile(
        app_name=app_name, user_id=user_id
    )
    if not profile:
        return ""
    return _render_profile(profile, include_lists=include_lists)


def render_user_profile(state: dict | None) -> str:
    """Return a ``## User Profile`` block for state['user_profile'], else ''."""
    if not state:
        return ""
    profile = state.get("user_profile")
    if not profile or not str(profile).strip():
        return ""
    return f"## User Profile\n\n{str(profile).strip()}"


__all__ = [
    "engine_resource_name",
    "load_user_profile",
    "render_user_profile",
]
