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

"""Per-session skill telemetry — counters for the curator's promotion pass.

Tracks two signals keyed by skill name:

* ``views`` — bumped when the agent loads a skill's instructions or one of
  its bundled resource files via ``load_skill``.
* ``manages`` — bumped when the agent writes to a skill file via the
  generic ``write`` or ``edit`` tools, recognised by a
  ``.agents/skills/<name>/...`` path prefix (the layout ``skill_loader`` walks).

Cross-session lifecycle facts live in Memory Bank via
``horizon.memory.skill_curator``.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from horizon.tools import names

SKILL_TELEMETRY_STATE_KEY = "skill_telemetry"

_VIEW_TOOLS = frozenset({names.LOAD_SKILL})
_MUTATE_TOOLS = frozenset({names.WRITE, names.EDIT})
_SKILLS_PREFIX = ".agents/skills/"


def _is_success(tool_response: Any) -> bool:
    if not isinstance(tool_response, Mapping):
        return False
    if "error" in tool_response:
        return False
    if tool_response.get("success") is False:
        return False
    return True


def _empty_record() -> dict[str, Any]:
    return {"views": 0, "manages": 0, "last_used": None}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def skill_name_from_path(path: Any) -> str | None:
    """Return the skill name when ``path`` is ``.agents/skills/<name>/...``."""
    if not isinstance(path, str) or not path:
        return None
    # NOT ``lstrip('./')`` — that would strip the leading dot off ``.agents``.
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if not normalized.startswith(_SKILLS_PREFIX):
        return None
    rest = normalized[len(_SKILLS_PREFIX) :]
    name, sep, _ = rest.partition("/")
    if not sep or not name:
        return None
    return name


def _classify(
    tool_name: str, args: Mapping[str, Any]
) -> tuple[str, str] | None:
    """Return ``(bump_key, skill_name)`` for a telemetry-relevant call."""
    if tool_name in _VIEW_TOOLS:
        skill_name = args.get("skill_name") or args.get("name")
        if isinstance(skill_name, str) and skill_name:
            return "views", skill_name
        return None
    if tool_name in _MUTATE_TOOLS:
        skill_name = skill_name_from_path(args.get("path"))
        if skill_name:
            return "manages", skill_name
        return None
    return None


async def skill_telemetry_callback(
    *,
    tool: Any,
    args: Mapping[str, Any] | None,
    tool_response: Any,
    tool_context: Any,
) -> None:
    """Bump per-skill counters in ``session.state['skill_telemetry']``.

    Wire as an ``after_tool_callback``. No-ops for unrelated tools, for
    failed calls, and for mutates whose path is outside ``.agents/skills/``.
    """
    tool_name = getattr(tool, "name", "") or ""
    if tool_name not in _VIEW_TOOLS and tool_name not in _MUTATE_TOOLS:
        return None

    if not _is_success(tool_response):
        return None

    if not isinstance(args, Mapping):
        return None

    classified = _classify(tool_name, args)
    if classified is None:
        return None
    bump_key, skill_name = classified

    telem: dict[str, dict[str, Any]] = (
        tool_context.state.get(SKILL_TELEMETRY_STATE_KEY) or {}
    )
    rec = telem.get(skill_name) or _empty_record()
    rec[bump_key] = int(rec.get(bump_key, 0)) + 1
    rec["last_used"] = _now_iso()
    telem[skill_name] = rec
    tool_context.state[SKILL_TELEMETRY_STATE_KEY] = telem
    return None
