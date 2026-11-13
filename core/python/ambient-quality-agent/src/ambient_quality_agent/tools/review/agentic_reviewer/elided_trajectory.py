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

"""Renders the elided view of the trajectory JSON for the first message.

`render_elided_trajectory` writes the trajectory JSON with one agent and one
event per line. Values over the `INLINE_*_CHARS` limits become
``{"$elided": ...}`` stubs and repeated texts become ``{"$same_as": ...}``
pointers, so the model reads the left-out content through its tools.

Wiring::

    render_elided_trajectory() ──► _TrajectoryElider.elide_agent(),
                                   elide_event(), elide_other()
                                     ──► _build_stub()
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any

from ambient_quality_agent.tools.review.agentic_reviewer.trajectory_json import (
    has_tool_schema,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

INLINE_INSTRUCTION_CHARS = 4_000
"""Max length of an agent instruction shown in full in the first message."""

INLINE_THOUGHT_CHARS = 1_000
"""Max length of a thought text shown in full in the first message."""

INLINE_ARGS_CHARS = 2_000
"""Max compact-JSON length of tool-call arguments shown in full."""

INLINE_RESULT_CHARS = 300
"""Max compact-JSON length of a tool result shown in full."""

INLINE_TOOL_CHARS = 2_000
"""Max compact-JSON length of a tool declaration shown in full."""

INLINE_OTHER_CHARS = 2_000
"""Max compact-JSON length of any other value shown in full."""

STUB_KEYS = 20
"""Max object keys listed in an elided object stub."""

SAME_AS_MIN_CHARS = 100
"""Minimum text length to replace repeated occurrences with a ``$same_as``
pointer; shorter strings are repeated inline because a pointer saves little."""


def render_elided_trajectory(trajectory_json: dict[str, Any]) -> str:
    """Render `trajectory_json` as valid JSON with one agent and one event per line.

    Large values are replaced with stubs (see `_TrajectoryElider`).

    Args:
        trajectory_json: Trajectory JSON built by
            `trajectory_json.build_trajectory_json`.

    Returns:
        Formatted JSON string without a trailing newline.
    """
    elider = _TrajectoryElider(trajectory_json)
    agents = [
        json.dumps(elider.elide_agent(agent), ensure_ascii=False)
        for agent in trajectory_json.get("agents") or []
    ]
    lines = ['{"agents": [']
    lines += _format_json_lines(agents, indent=" ")
    lines += ["],", '"turns": [']
    turns = list(trajectory_json.get("turns") or [])
    for turn_number, turn in enumerate(turns):
        header = "".join(
            f"{json.dumps(key)}: "
            f"{json.dumps(elider.elide_other(value), ensure_ascii=False)}, "
            for key, value in turn.items()
            if key != "events"
        )
        lines.append(f' {{{header}"events": [')
        events = [
            json.dumps(elider.elide_event(event), ensure_ascii=False)
            for event in turn.get("events") or []
        ]
        lines += _format_json_lines(events, indent="  ")
        lines.append(" ]}" + ("," if turn_number < len(turns) - 1 else ""))
    lines.append("]}")
    return "\n".join(lines)


def _build_stub(value: Any) -> dict[str, Any]:
    """Build an ``$elided`` stub summarizing a value's type and size.

    Args:
        value: Elided value.

    Returns:
        ``{"$elided": "string, N chars"}``,
        ``{"$elided": "object, N chars", "keys": [...]}``,
        ``{"$elided": "array, K items, N chars"}``, or
        ``{"$elided": "<type>, N chars"}``, where ``N`` is the string length or
        compact-JSON character length.
    """
    if isinstance(value, str):
        return {"$elided": f"string, {len(value)} chars"}
    size = len(_format_compact_json(value))
    if isinstance(value, dict):
        return {
            "$elided": f"object, {size} chars",
            "keys": list(value)[:STUB_KEYS],
        }
    if isinstance(value, list):
        return {"$elided": f"array, {len(value)} items, {size} chars"}
    return {"$elided": f"{type(value).__name__}, {size} chars"}


class _TrajectoryElider:
    """Elides large values in a trajectory JSON and deduplicates repeated texts."""

    def __init__(self, trajectory_json: dict[str, Any]) -> None:
        """Initialize the elider for `trajectory_json`.

        Args:
            trajectory_json: Trajectory JSON built by
                `trajectory_json.build_trajectory_json`.
        """
        self._block_instructions = {
            str(agent.get("agent_id") or ""): str(
                agent.get("instruction") or ""
            )
            for agent in trajectory_json.get("agents") or []
        }
        # First event ID of each seen text, tracked separately for user and agent authors.
        self._seen_texts: dict[bool, dict[str, str]] = {False: {}, True: {}}

    def elide_agent(self, agent: dict[str, Any]) -> dict[str, Any]:
        """Elide an agent entry.

        Args:
            agent: Agent dict from the trajectory JSON.

        Returns:
            Agent dict with long instructions replaced by a stub and outline,
            and tools elided via `_elide_tool`.
        """
        elided: dict[str, Any] = {}
        instruction = agent.get("instruction")
        is_long = (
            isinstance(instruction, str)
            and len(instruction) > INLINE_INSTRUCTION_CHARS
        )
        for key, value in agent.items():
            if key == "agent_id":
                elided[key] = value
            elif key == "instruction":
                elided[key] = _build_stub(value) if is_long else value
            elif key == "instruction_outline":
                if is_long:
                    elided[key] = value
            elif key == "tools":
                elided[key] = [self._elide_tool(tool) for tool in value or []]
            else:
                elided[key] = self.elide_other(value)
        return elided

    def _elide_tool(self, tool: Any) -> Any:
        """Elide an entry from an agent's tool list.

        Args:
            tool: Tool name or declaration dict.

        Returns:
            Tool name when no schema is recorded; full ``name``,
            ``description``, and ``parameters`` dict when within
            `INLINE_TOOL_CHARS`; otherwise ``{"name": ..., "$elided": ...}``.
        """
        if not isinstance(tool, dict):
            return tool
        if not has_tool_schema(tool):
            return tool.get("name")
        declaration = {
            key: tool[key]
            for key in ("name", "description", "parameters")
            if key in tool
        }
        size = len(_format_compact_json(declaration))
        if size <= INLINE_TOOL_CHARS:
            return declaration
        return {"name": tool.get("name"), "$elided": f"object, {size} chars"}

    def elide_event(self, event: dict[str, Any]) -> dict[str, Any]:
        """Elide an event entry.

        Args:
            event: Event dict from the trajectory JSON.

        Returns:
            Event dict with content parts, active tools, and state delta elided.
        """
        author = str(event.get("author") or "")
        event_id = str(event.get("event_id") or "")
        elided: dict[str, Any] = {}
        for key, value in event.items():
            if key in ("event_id", "author"):
                elided[key] = value
            elif key == "content" and isinstance(value, dict):
                elided[key] = self._elide_content(value, author, event_id)
            elif key == "active_tools":
                elided[key] = _list_active_tool_names(value)
            elif key == "state_delta" and isinstance(value, dict):
                elided[key] = self._elide_state_delta(value, author)
            else:
                elided[key] = self.elide_other(value)
        return elided

    def _elide_content(
        self, content: dict[str, Any], author: str, event_id: str
    ) -> dict[str, Any]:
        """Elide an event's content dict.

        Args:
            content: Event content dict.
            author: Event author.
            event_id: Event identifier (``tN.eM``).

        Returns:
            Content dict with ``role`` preserved and ``parts`` elided.
        """
        elided: dict[str, Any] = {}
        for key, value in _order_keys(content, "role"):
            if key == "role":
                elided[key] = value
            elif key == "parts" and isinstance(value, list):
                elided[key] = [
                    self._elide_part(part, author, event_id)
                    if isinstance(part, dict)
                    else self.elide_other(part)
                    for part in value
                ]
            else:
                elided[key] = self.elide_other(value)
        return elided

    def _elide_part(
        self, part: dict[str, Any], author: str, event_id: str
    ) -> dict[str, Any]:
        """Elide a single content part.

        Args:
            part: Content part dict.
            author: Event author.
            event_id: Event identifier (``tN.eM``).

        Returns:
            Part dict with fields elided according to their type.
        """
        text = part.get("text")
        is_thought = bool(part.get("thought"))
        repeat_of = None
        if (
            isinstance(text, str)
            and not is_thought
            and len(text) >= SAME_AS_MIN_CHARS
        ):
            seen = self._seen_texts[author != "user"]
            repeat_of = seen.get(text)
            if repeat_of is None:
                seen[text] = event_id
        elided: dict[str, Any] = {}
        for key, value in part.items():
            if key == "text" and isinstance(value, str):
                if is_thought:
                    elided[key] = (
                        value
                        if len(value) <= INLINE_THOUGHT_CHARS
                        else _build_stub(value)
                    )
                elif repeat_of is not None:
                    elided[key] = {"$same_as": repeat_of}
                elif "text_json" in part:
                    elided[key] = _build_stub(value)
                else:
                    elided[key] = value
            elif key == "text_json":
                if repeat_of is None:
                    elided[key] = value
            elif key in ("thought", "text_wrapper"):
                elided[key] = value
            elif key == "thought_signature":
                # Opaque model API token; carries no session evidence.
                elided[key] = _build_stub(value)
            elif key == "function_call" and isinstance(value, dict):
                elided[key] = self._elide_call(value)
            elif key == "function_response" and isinstance(value, dict):
                elided[key] = {
                    name: (
                        self._elide_result(item)
                        if name == "response"
                        else self.elide_other(item)
                    )
                    for name, item in _order_keys(value, "name")
                }
            else:
                elided[key] = self.elide_other(value)
        return elided

    def _elide_result(self, response: Any) -> Any:
        """Elide a function response payload.

        Args:
            response: ``response`` value from a ``function_response`` part.

        Returns:
            `response` in full when within `INLINE_RESULT_CHARS` of compact
            JSON, else a stub.
        """
        size = len(_format_compact_json(response))
        if size <= INLINE_RESULT_CHARS:
            return response
        return _build_stub(response)

    def _elide_call(self, call: dict[str, Any]) -> dict[str, Any]:
        """Elide a function call dict.

        Args:
            call: ``function_call`` dict.

        Returns:
            Function call dict with ``args`` kept in full up to
            `INLINE_ARGS_CHARS` of compact JSON or replaced by a stub.
        """
        elided: dict[str, Any] = {}
        for key, value in _order_keys(call, "name"):
            if key == "args":
                size = len(_format_compact_json(value))
                elided[key] = (
                    value if size <= INLINE_ARGS_CHARS else _build_stub(value)
                )
            else:
                elided[key] = self.elide_other(value)
        return elided

    def _elide_state_delta(
        self, state_delta: dict[str, Any], author: str
    ) -> dict[str, Any]:
        """Elide an event's ``state_delta`` dict.

        Args:
            state_delta: Event state delta.
            author: Event author.

        Returns:
            State delta with ``system_instruction`` replaced by a stub that
            includes the length of the prefix shared with `author`'s block
            instruction.
        """
        elided: dict[str, Any] = {}
        for key, value in state_delta.items():
            if key == "system_instruction" and isinstance(value, str):
                elided[key] = {
                    **_build_stub(value),
                    "prefix_shared_with_block_instruction": len(
                        os.path.commonprefix(
                            [self._block_instructions.get(author, ""), value]
                        )
                    ),
                }
            else:
                elided[key] = self.elide_other(value)
        return elided

    def elide_other(self, value: Any) -> Any:
        """Elide a fallback value not covered by a specific rule.

        Args:
            value: Arbitrary JSON-serializable value.

        Returns:
            `value` in full when within `INLINE_OTHER_CHARS` of compact JSON,
            else a stub.
        """
        if len(_format_compact_json(value)) <= INLINE_OTHER_CHARS:
            return value
        return _build_stub(value)


def _order_keys(mapping: dict[str, Any], first: str) -> list[tuple[str, Any]]:
    """Return `mapping` items with `first` placed at the front if present.

    Args:
        mapping: Dictionary to order.
        first: Key to place first.

    Returns:
        Key-value pairs with `first` leading and remaining keys in original
        order.
    """
    return sorted(mapping.items(), key=lambda item: item[0] != first)


def _format_json_lines(items: Sequence[str], *, indent: str) -> list[str]:
    """Indent serialized JSON items and append trailing commas to all but the last.

    Args:
        items: Serialized JSON strings.
        indent: Leading indentation for each line.

    Returns:
        Indented lines, each except the last ending in a comma.
    """
    return [
        f"{indent}{item}" + ("," if index < len(items) - 1 else "")
        for index, item in enumerate(items)
    ]


def _list_active_tool_names(active_tools: Any) -> list[str]:
    """List the function names an event's ``active_tools`` declares.

    Args:
        active_tools: Serialized ``active_tools`` value: a list of tools, each
            with ``function_declarations``.

    Returns:
        Declared function names.
    """
    names = []
    for tool in active_tools or []:
        if not isinstance(tool, dict):
            continue
        for declaration in tool.get("function_declarations") or []:
            if isinstance(declaration, dict) and declaration.get("name"):
                names.append(str(declaration["name"]))
    return names


def _format_compact_json(value: Any) -> str:
    """Encode a value as JSON without whitespace, keeping non-ASCII text.

    Args:
        value: Any JSON-serializable value; others are encoded with `str`.

    Returns:
        The JSON text.
    """
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), default=str
    )
