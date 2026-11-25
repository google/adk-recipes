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

"""Paths into an ATIF document, and the objects in it that can hold `extra`.

ATIF allows `extra` on the trajectory, the agent, a step, a tool call, an
observation result, a subagent reference, metrics and final metrics. AQuA
writes everything it adds under `extra.aqua` on one of these objects, so every
stage that reads or writes `extra.aqua` walks the same holders.

A `JsonPath` addresses a value from the document root as keys and list
indexes. A marker path is relative to its holder and formatted as text, such
as `message[1]` or `observation.results`.
"""

from __future__ import annotations

import dataclasses
import enum
import json
import re
from collections.abc import Iterator, Mapping
from typing import Any

JsonPath = tuple[str | int, ...]

# The key under `extra` that holds everything AQuA adds to a document.
AQUA_KEY = "aqua"

# The marker path segment for a field whose name is not an identifier.
UNNAMED_FIELD = "*"

# Keys under `extra.aqua` that annotation writes or reads.
RECORDS_TOOL_ERRORS = "records_tool_errors"

_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_INDEX_SEGMENT_RE = re.compile(r"\[(0|[1-9][0-9]*)\]")


class HolderKind(enum.StrEnum):
    """The kinds of ATIF object that can hold `extra`."""

    TRAJECTORY = "trajectory"
    AGENT = "agent"
    STEP = "step"
    TOOL_CALL = "tool_call"
    RESULT = "result"
    SUBAGENT_REF = "subagent_ref"
    METRICS = "metrics"
    FINAL_METRICS = "final_metrics"


# From each holder kind, the key sequences that lead to a nested holder. `None`
# stands for a list index.
_CHILDREN: dict[
    HolderKind, tuple[tuple[tuple[str | None, ...], HolderKind], ...]
] = {
    HolderKind.TRAJECTORY: (
        (("agent",), HolderKind.AGENT),
        (("steps", None), HolderKind.STEP),
        (("final_metrics",), HolderKind.FINAL_METRICS),
        (("subagent_trajectories", None), HolderKind.TRAJECTORY),
    ),
    HolderKind.STEP: (
        (("tool_calls", None), HolderKind.TOOL_CALL),
        (("observation", "results", None), HolderKind.RESULT),
        (("metrics",), HolderKind.METRICS),
    ),
    HolderKind.RESULT: (
        (("subagent_trajectory_ref", None), HolderKind.SUBAGENT_REF),
    ),
}


@dataclasses.dataclass(frozen=True)
class Holder:
    """One object in a document that can hold `extra`.

    Attributes:
        path: Path of the object from the document root.
        kind: Which ATIF object it is.
        value: The object itself; changes to it change the document.
        trajectory: Path of the trajectory that contains the object (the
            object's own path for a trajectory).
    """

    path: JsonPath
    kind: HolderKind
    value: dict[str, Any]
    trajectory: JsonPath


def list_holders(document: Mapping[str, Any]) -> list[Holder]:
    """Lists every object in `document` that can hold `extra`, in document order.

    Embedded subagents are included with their own holders. Values that do not
    have the shape ATIF gives them are skipped, so this is safe on a document
    that has not been validated.

    Args:
        document: Root trajectory of an ATIF document.

    Returns:
        The holders, the root trajectory first.
    """
    if not isinstance(document, dict):
        return []
    return list(_iter_holders(document, (), HolderKind.TRAJECTORY, ()))


def _iter_holders(
    value: dict[str, Any],
    path: JsonPath,
    kind: HolderKind,
    trajectory: JsonPath,
) -> Iterator[Holder]:
    """Yields `value` as a holder and then the holders nested in it.

    Args:
        value: The holder object.
        path: Path of `value` from the document root.
        kind: Kind of `value`.
        trajectory: Path of the trajectory that contains `value`.

    Yields:
        Holders in document order.
    """
    if kind == HolderKind.TRAJECTORY:
        trajectory = path
    yield Holder(path=path, kind=kind, value=value, trajectory=trajectory)
    for keys, child_kind in _CHILDREN.get(kind, ()):
        for child_path, child in _iter_children(value, path, keys):
            yield from _iter_holders(child, child_path, child_kind, trajectory)


def _iter_children(
    value: Any, path: JsonPath, keys: tuple[str | None, ...]
) -> Iterator[tuple[JsonPath, dict[str, Any]]]:
    """Yields the objects reached from `value` by following `keys`.

    Args:
        value: Object to start from.
        path: Path of `value` from the document root.
        keys: Keys to follow; `None` follows every list element.

    Yields:
        `(path, object)` for every object reached.
    """
    if not keys:
        if isinstance(value, dict):
            yield path, value
        return
    key, rest = keys[0], keys[1:]
    if key is None:
        if isinstance(value, list):
            for index, item in enumerate(value):
                yield from _iter_children(item, (*path, index), rest)
    elif isinstance(value, dict) and key in value:
        yield from _iter_children(value[key], (*path, key), rest)


def split_holder_path(path: JsonPath) -> tuple[JsonPath, HolderKind, JsonPath]:
    """Splits a path into its nearest holder and the rest of the path.

    Args:
        path: Path of any value from the document root.

    Returns:
        `(holder_path, holder_kind, relative_path)`: the path of the innermost
        holder that contains the value, its kind, and the path of the value
        from that holder.
    """
    kind = HolderKind.TRAJECTORY
    holder_end = 0
    position = 0
    while True:
        for keys, child_kind in _CHILDREN.get(kind, ()):
            if _matches_keys(path[position:], keys):
                position += len(keys)
                holder_end = position
                kind = child_kind
                break
        else:
            return path[:holder_end], kind, path[holder_end:]


def _matches_keys(path: JsonPath, keys: tuple[str | None, ...]) -> bool:
    """Checks whether `path` starts with `keys`.

    Args:
        path: Path segments to test.
        keys: Expected keys; `None` matches any list index.

    Returns:
        True when the first segments of `path` match `keys`.
    """
    if len(path) < len(keys):
        return False
    for segment, key in zip(path, keys, strict=False):
        if key is None:
            if not isinstance(segment, int):
                return False
        elif segment != key:
            return False
    return True


def get_value(document: Any, path: JsonPath) -> Any:
    """Gets the value at `path` in `document`.

    Args:
        document: Parsed JSON value.
        path: Keys and list indexes from the root.

    Returns:
        The value, or None when the path does not exist.
    """
    current = document
    for segment in path:
        if isinstance(segment, int):
            if not isinstance(current, list) or not 0 <= segment < len(current):
                return None
        elif not isinstance(current, dict) or segment not in current:
            return None
        current = current[segment]
    return current


def format_path(path: JsonPath) -> str:
    """Formats a relative path the way marker paths are written.

    Keys are joined with `.` and list indexes are written as `[i]`, so
    `("message", 1, "text")` becomes `message[1].text`. A key that is not an
    identifier is written as `UNNAMED_FIELD` (`*`) and ends the path, so a
    marker path never holds a key taken from trajectory content:
    `("message", 1, "x-y", "z")` becomes `message[1].*`.

    Args:
        path: Keys and list indexes.

    Returns:
        The formatted path.
    """
    text = ""
    for segment in path:
        if isinstance(segment, int):
            text += f"[{segment}]"
        elif _IDENTIFIER_RE.fullmatch(segment):
            text += f".{segment}" if text else segment
        else:
            text += f".{UNNAMED_FIELD}" if text else UNNAMED_FIELD
            break
    return text


def format_json_path(path: JsonPath) -> str:
    """Formats a path so that `parse_json_path` reads back exactly the same path.

    Identifier keys are joined with `.`, list indexes are written as `[i]`,
    and any other key is written as a JSON string in brackets, so
    `("extra", "x-request-id", 0)` becomes `extra["x-request-id"][0]`. Unlike
    `format_path`, the result can hold text from the document, so it belongs in
    stored records, not in logs.

    Args:
        path: Keys and list indexes.

    Returns:
        The formatted path; empty for the empty path.
    """
    text = ""
    for segment in path:
        if isinstance(segment, int):
            text += f"[{segment}]"
        elif _IDENTIFIER_RE.fullmatch(segment):
            text += f".{segment}" if text else segment
        else:
            text += f"[{json.dumps(segment)}]"
    return text


def parse_json_path(text: str) -> JsonPath:
    """Parses a path written by `format_json_path`.

    Args:
        text: The formatted path.

    Returns:
        The keys and list indexes.

    Raises:
        ValueError: If `text` is not in the form `format_json_path` writes.
    """
    path: list[str | int] = []
    position = 0
    decoder = json.JSONDecoder()
    while position < len(text):
        if text[position] == "[":
            if text.startswith('["', position):
                key, end = decoder.raw_decode(text, position + 1)
                if not isinstance(key, str) or not text.startswith("]", end):
                    raise ValueError("not a path")
                path.append(key)
                position = end + 1
                continue
            match = _INDEX_SEGMENT_RE.match(text, position)
            if match is None:
                raise ValueError("not a path")
            path.append(int(match.group(1)))
            position = match.end()
            continue
        start = position + 1 if path and text[position] == "." else position
        match = _IDENTIFIER_RE.match(text, start)
        if match is None or (path and start == position):
            raise ValueError("not a path")
        path.append(match.group(0))
        position = match.end()
    return tuple(path)


def format_location(path: JsonPath) -> str:
    """Formats a path from the root the way rejection reasons write it.

    Matches Pydantic's error locations, such as `steps.2.tool_call`.

    Args:
        path: Keys and list indexes.

    Returns:
        The segments joined with `.`.
    """
    return ".".join(str(segment) for segment in path)


def get_aqua(
    holder: dict[str, Any], *, create: bool = False
) -> dict[str, Any] | None:
    """Gets the `extra.aqua` object of a holder.

    Args:
        holder: An object that can hold `extra`.
        create: Whether to create `extra` and `extra.aqua` when absent. A
            non-object value in their place is replaced.

    Returns:
        The `extra.aqua` object, or None when it is absent and `create` is
        False.
    """
    extra = holder.get("extra")
    if not isinstance(extra, dict):
        if not create:
            return None
        extra = {}
        holder["extra"] = extra
    aqua = extra.get(AQUA_KEY)
    if not isinstance(aqua, dict):
        if not create:
            return None
        aqua = {}
        extra[AQUA_KEY] = aqua
    return aqua


def remove_empty_aqua(holder: dict[str, Any]) -> None:
    """Removes an empty `extra.aqua`, leaving the rest of `extra` as it was.

    Args:
        holder: An object that can hold `extra`.
    """
    extra = holder.get("extra")
    if isinstance(extra, dict) and extra.get(AQUA_KEY) == {}:
        del extra[AQUA_KEY]
