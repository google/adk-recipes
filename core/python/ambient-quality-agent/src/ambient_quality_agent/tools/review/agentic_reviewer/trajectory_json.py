# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Builds the full trajectory JSON that the reviewer's tools query.

`build_trajectory_json` serializes one session into
``{"agents": [...], "turns": [...]}``, with derived fields (event IDs,
instruction outlines, decoded JSON texts) that make it easy to query. Nothing
in it is elided. `has_recorded_schemas` reports whether any tool in the
session has a recorded parameter schema, `has_tool_schema` reports it for one
agent tool entry, and `has_attachment` reports whether any turn carries an
attachment reference.

Wiring::

    build_trajectory_json()
        ──► findings.extract_trajectory_turns()
        ──► _build_instruction_outline() ──► _list_outline_entries()
        ──► _add_event_fields() ──► _add_text_fields() ──► _decode_json_text()
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any, Final

from ambient_quality_agent.tools.ingestion.message_parts import (
    is_attachment_placeholder,
)
from ambient_quality_agent.tools.insights.findings import (
    extract_trajectory_turns,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ambient_quality_agent.tools.agent_revision import AgentRevision

TEXT_WRAPPER_NOTE = "text: '…' (added by the telemetry recorder)"
"""`text_wrapper` annotation for text wrapped by the telemetry recorder."""

_WRAPPER_PREFIX = "text: '"
_WRAPPER_SUFFIX = "'"

# Omit level 5 and deeper headings from outlines because they mark detail
# rather than top-level navigable sections.
_HEADING = re.compile(r"#{1,4}\s+\S")

# Match standalone XML-style opening tags (e.g., `<ROLE>`) used as section
# headers in some instructions; closing tags are ignored.
_SECTION_TAG = re.compile(r"<[A-Za-z_][A-Za-z0-9_-]*>")

_NO_VALUE: Final = object()
"""Sentinel for text that does not decode to a JSON object or array."""


def build_trajectory_json(
    eval_case: Any, revisions: Sequence[AgentRevision]
) -> dict[str, Any]:
    """Build the trajectory JSON queried by the reviewer's tools.

    The JSON holds:

    - one entry per agent with ``agent_id``, ``instruction``,
      ``instruction_outline`` (``[offset, header_line]`` pairs, where
      ``offset`` is a character offset into ``instruction``), and
      ``tools`` (tool name when neither parameters nor description are
      recorded, otherwise the declaration dict);
    - the serialized turns, with ``event_id`` (``tN.eM``) as the first key on
      each event, and ``text_json`` on each non-thought text part that decodes
      to a JSON object or array. Agent text wrapped as ``text: '<json>'`` by
      the telemetry recorder also gets ``text_wrapper`` and is decoded without
      the wrapper.

    Args:
        eval_case: Evaluation case containing the conversation turns.
        revisions: Agent definitions active during the session.

    Returns:
        Trajectory JSON ``{"agents": [...], "turns": [...]}``.
    """
    turns = extract_trajectory_turns(eval_case)
    for turn_number, turn in enumerate(turns):
        events = turn.get("events") or []
        turn["events"] = [
            _add_event_fields(event, f"t{turn_number}.e{event_number}")
            for event_number, event in enumerate(events)
        ]
    return {
        "agents": [
            {
                "agent_id": revision.agent_id,
                "instruction": revision.instruction,
                "instruction_outline": _build_instruction_outline(
                    str(revision.instruction or "")
                ),
                "tools": [
                    tool.name
                    if tool.parameters is None and not tool.description
                    else tool.model_dump()
                    for tool in revision.tools
                ],
            }
            for revision in revisions
        ],
        "turns": turns,
    }


def has_recorded_schemas(trajectory_json: dict[str, Any]) -> bool:
    """Check whether any tool in the session has a recorded parameter schema.

    Args:
        trajectory_json: The JSON `build_trajectory_json` built.

    Returns:
        True if an agent's tool declares ``parameters``, or a function
        declaration in an event's ``active_tools`` declares ``parameters`` or
        ``parameters_json_schema``.
    """
    for agent in trajectory_json.get("agents") or []:
        for tool in agent.get("tools") or []:
            if has_tool_schema(tool):
                return True
    for turn in trajectory_json.get("turns") or []:
        for event in turn.get("events") or []:
            for tool in event.get("active_tools") or []:
                if not isinstance(tool, dict):
                    continue
                for declaration in tool.get("function_declarations") or []:
                    if isinstance(declaration, dict) and (
                        declaration.get("parameters") is not None
                        or declaration.get("parameters_json_schema") is not None
                    ):
                        return True
    return False


def has_tool_schema(tool: Any) -> bool:
    """Check whether an agent tool entry includes a parameter schema.

    Args:
        tool: Tool name or declaration dict from an agent's ``tools`` list.

    Returns:
        True if `tool` is a declaration dict with a non-None ``parameters``
        field.
    """
    return isinstance(tool, dict) and tool.get("parameters") is not None


def has_attachment(trajectory_json: dict[str, Any]) -> bool:
    """Check whether any turn carries an attachment reference from ingestion.

    Args:
        trajectory_json: The JSON `build_trajectory_json` built. Missing, None
            or malformed `events`, `content` and `parts` entries are skipped.

    Returns:
        True if a part has a `file_data` value or is an `<attachment: ...>`
        text placeholder.
    """
    for turn in trajectory_json.get("turns") or ():
        if not isinstance(turn, dict):
            continue
        for event in turn.get("events") or ():
            content = event.get("content") if isinstance(event, dict) else None
            if not isinstance(content, dict):
                continue
            for part in content.get("parts") or ():
                if not isinstance(part, dict):
                    continue
                if part.get("file_data") is not None:
                    return True
                text = part.get("text")
                if isinstance(text, str) and is_attachment_placeholder(text):
                    return True
    return False


def _build_instruction_outline(instruction: str) -> list[list[int | str]]:
    """Extract section headers from `instruction` with their character offsets.

    Args:
        instruction: Agent system instruction text.

    Returns:
        ``[offset, header_line]`` pairs in text order.
    """
    return [
        [offset, line] for offset, line in _list_outline_entries(instruction)
    ]


def _list_outline_entries(text: str) -> list[tuple[int, str]]:
    """List section-header lines and their character offsets outside code fences.

    Matches markdown headings of levels 1 to 4 and lines containing a single
    opening XML tag (e.g., `<ROLE>`). Tag lines preserve leading indentation to
    indicate nesting.

    Args:
        text: Instruction text.

    Returns:
        ``(char_offset, line)`` pairs in text order.
    """
    entries: list[tuple[int, str]] = []
    in_fence = False
    offset = 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
        elif not in_fence and _HEADING.match(line):
            entries.append((offset, stripped))
        elif not in_fence and _SECTION_TAG.fullmatch(stripped):
            entries.append((offset, line.rstrip()))
        offset += len(line)
    return entries


def _add_event_fields(event: dict[str, Any], event_id: str) -> dict[str, Any]:
    """Add ``event_id`` and derived text fields to an event.

    Args:
        event: Serialized event.
        event_id: Event identifier (``tN.eM``).

    Returns:
        Copy of `event` with ``event_id`` first and text parts enriched.
    """
    is_agent = str(event.get("author") or "") != "user"
    content = event.get("content")
    if isinstance(content, dict) and isinstance(content.get("parts"), list):
        content = {
            **content,
            "parts": [
                _add_text_fields(part, is_agent=is_agent)
                if isinstance(part, dict)
                else part
                for part in content["parts"]
            ],
        }
        event = {**event, "content": content}
    # Drops a recorded `event_id` key so it cannot overwrite the computed one.
    return {
        "event_id": event_id,
        **{key: value for key, value in event.items() if key != "event_id"},
    }


def _add_text_fields(part: dict[str, Any], *, is_agent: bool) -> dict[str, Any]:
    """Attach ``text_wrapper`` and ``text_json`` to a JSON or wrapped text part.

    Args:
        part: Serialized content part.
        is_agent: True when the event author is an agent rather than the user.

    Returns:
        Copy of `part` with ``text_wrapper`` and/or ``text_json`` when
        applicable, or `part` unchanged.
    """
    text = part.get("text")
    if not isinstance(text, str) or part.get("thought"):
        return part
    payload = text
    wrapper: dict[str, str] = {}
    stripped = text.strip()
    if (
        is_agent
        and len(stripped) >= len(_WRAPPER_PREFIX) + len(_WRAPPER_SUFFIX)
        and stripped.startswith(_WRAPPER_PREFIX)
        and stripped.endswith(_WRAPPER_SUFFIX)
    ):
        payload = stripped[len(_WRAPPER_PREFIX) : -len(_WRAPPER_SUFFIX)]
        wrapper = {"text_wrapper": TEXT_WRAPPER_NOTE}
    decoded = _decode_json_text(payload)
    if not wrapper and decoded is _NO_VALUE:
        return part
    extended = dict(part)
    extended.pop("text_wrapper", None)
    extended.pop("text_json", None)
    extended.update(wrapper)
    if decoded is not _NO_VALUE:
        extended["text_json"] = decoded
    return extended


def _decode_json_text(text: str) -> Any:
    """Decode `text` when it contains a JSON object or array.

    The telemetry wrapper quotes text in single quotes, so embedded single
    quotes may be escaped as ``\\'`` (invalid in JSON); a failed parse is
    retried with ``\\'`` unescaped.

    Args:
        text: Unwrapped text candidate.

    Returns:
        Decoded dict or list, or `_NO_VALUE` if `text` is not a JSON object or
        array.
    """
    candidate = text.strip()
    if not candidate.startswith(("{", "[")):
        return _NO_VALUE
    attempts = [candidate]
    if "\\'" in candidate:
        attempts.append(candidate.replace("\\'", "'"))
    for attempt in attempts:
        try:
            value = json.loads(attempt)
        except (ValueError, RecursionError):
            continue
        if isinstance(value, (dict, list)):
            return value
    return _NO_VALUE
