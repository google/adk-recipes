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

"""Strict parsing of an untrusted trajectory file, before the models see it.

The grammar is JSON as RFC 8259 defines it, read by Python's `json` module with
its extensions to that grammar turned off:

1. Input that is not JSON, holds `NaN` or `Infinity` (including a number such
   as `1e999` that overflows to infinity), or nests deeper than
   `MAX_JSON_DEPTH` levels is rejected as "invalid JSON". RFC 8259 has no
   `NaN` or `Infinity` (section 6) and lets a parser limit nesting (section 9).
2. A lone UTF-16 surrogate in a string, which `\\ud800`-style escapes can
   produce, is replaced with U+FFFD, and the field gets an `invalid_content`
   marker. RFC 8259 allows such a string and calls the behavior of software
   that receives it unpredictable (section 8.2); I-JSON (RFC 7493) forbids it.
   Python parses it into a string that cannot be encoded as UTF-8, so without
   the replacement one such string fails every later step that writes the
   document. U+FFFD is the replacement Unicode specifies for ill-formed input.
   Replacing instead of rejecting keeps the session when a tool output has one
   cut-off character.

Where RFC 8259 leaves a choice, the parser follows Python's `json`: a name
that repeats in one object keeps its last value, and bytes may be UTF-16 or
UTF-32 as well as UTF-8.

    '{"a": "x\\ud83d"}'  ->  {"a": "x\\ufffd"} and, on the root, the marker
                             {"path": "a", "reason": "invalid_content",
                              "detail": "lone_surrogate"}
    '{"a": NaN}'          ->  DocumentRejectedError("invalid JSON")
    '{"a": 1e999}'        ->  DocumentRejectedError("invalid JSON")
"""

from __future__ import annotations

import dataclasses
import json
import math
import re
from collections.abc import Iterator
from typing import Any

from aqua_atif.markers import MissingMarker, PendingMissingMarker, Reason
from aqua_atif.paths import (
    JsonPath,
    format_path,
    split_holder_path,
)

MAX_JSON_DEPTH = 256
INVALID_JSON = "invalid JSON"

# The `detail` of the marker for a field whose lone surrogates were replaced.
LONE_SURROGATE = "lone_surrogate"

# The longest rejection reason kept; reasons can quote keys and values from the
# file, and they are stored and logged.
MAX_REASON_LENGTH = 300

_UNPRINTABLE_RE = re.compile(r"[^\x20-\x7e]")

_SURROGATE_RE = re.compile("[\ud800-\udfff]")


def bound_text(text: str, limit: int = MAX_REASON_LENGTH) -> str:
    """Bounds a value from a file before it reaches a log line or a reason.

    Characters outside printable ASCII become `?`, and a longer value is cut
    and ends with `...`, so a producer cannot flood or forge a log line or
    repeat file content at length.

    Args:
        text: The value, such as a rejection reason, trajectory id, or key.
        limit: Maximum characters to keep, `...` included.

    Returns:
        The bounded printable ASCII value.
    """
    printable = _UNPRINTABLE_RE.sub("?", text[:limit])
    if len(text) > limit:
        return printable[: limit - 3] + "..."
    return printable


class DocumentRejectedError(ValueError):
    """A document that AQuA does not ingest.

    Attributes:
        reason: Why, as stored on the document's row and logged: printable
            ASCII, at most `MAX_REASON_LENGTH` characters.
    """

    def __init__(self, reason: str) -> None:
        """Initializes the error.

        Args:
            reason: Why the document is rejected. Bounded to printable ASCII
                of at most `MAX_REASON_LENGTH` characters via `bound_text`.
        """
        reason = bound_text(reason, MAX_REASON_LENGTH)
        super().__init__(reason)
        self.reason = reason


@dataclasses.dataclass(frozen=True)
class ParsedJson:
    """A strictly parsed JSON document.

    Attributes:
        value: The parsed JSON, with lone surrogates replaced.
        markers: An `invalid_content` marker for each field in which a lone
            surrogate was replaced, on the nearest object that can hold
            `extra`. Merge them once the document is valid.
    """

    value: Any
    markers: tuple[PendingMissingMarker, ...]


def parse_document(raw: bytes | str) -> ParsedJson:
    """Parses an untrusted trajectory file strictly.

    Args:
        raw: The file's bytes (UTF-8, UTF-16, or UTF-32), or its text.

    Returns:
        The parsed value and the markers for replaced surrogates.

    Raises:
        DocumentRejectedError: With the reason "invalid JSON" when `raw` is not
            JSON, holds `NaN` or `Infinity` or a number that overflows to it,
            or nests deeper than `MAX_JSON_DEPTH`.
    """
    try:
        value = json.loads(
            raw,
            parse_constant=_reject_constant,
            parse_float=_parse_finite_float,
        )
    except (ValueError, RecursionError) as err:
        raise DocumentRejectedError(INVALID_JSON) from err
    replaced: list[JsonPath] = []
    value = _sanitize(value, replaced)
    markers: dict[tuple[JsonPath, str], PendingMissingMarker] = {}
    for path in replaced:
        holder, _, relative = split_holder_path(path)
        field = format_path(relative)
        markers.setdefault(
            (holder, field),
            PendingMissingMarker(
                holder,
                MissingMarker(field, Reason.INVALID_CONTENT, LONE_SURROGATE),
            ),
        )
    return ParsedJson(value=value, markers=tuple(markers.values()))


def _reject_constant(name: str) -> Any:
    """Rejects `NaN`, `Infinity` and `-Infinity`, which JSON does not allow.

    Args:
        name: The constant the parser found.

    Raises:
        ValueError: Always.
    """
    raise ValueError(f"{name} is not valid JSON")


def _parse_finite_float(text: str) -> float:
    """Parses a JSON number that has a fraction or exponent.

    Args:
        text: The number as written.

    Returns:
        The number.

    Raises:
        ValueError: When it overflows to infinity, which JSON cannot hold.
    """
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"{text[:20]} overflows")
    return value


def _replace_surrogate_keys(
    container: dict[str, Any], path: JsonPath, replaced: list[JsonPath]
) -> None:
    """Replaces lone surrogates in the keys of a dict, in place.

    Args:
        container: The JSON object to check.
        path: Path of `container` from the root.
        replaced: Receives the path of each key whose surrogates were replaced.
    """
    if not any(_SURROGATE_RE.search(key) for key in container):
        return
    items = list(container.items())
    container.clear()
    for key, item in items:
        clean_key = _SURROGATE_RE.sub("\ufffd", key)
        if clean_key != key:
            replaced.append((*path, clean_key))
        container[clean_key] = item


def _sanitize(root: Any, replaced: list[JsonPath]) -> Any:
    """Checks the nesting depth and replaces lone surrogates in strings.

    Walks the value with its own stack, since recursion over `MAX_JSON_DEPTH`
    levels on top of a caller's stack can pass Python's recursion limit.

    Args:
        root: A parsed JSON value.
        replaced: Receives the path of each string, or of the value under each
            key, in which a surrogate was replaced, in document order.

    Returns:
        `root`, with lone surrogates replaced. Containers are changed in place.

    Raises:
        DocumentRejectedError: When a container nests deeper than
            `MAX_JSON_DEPTH`.
    """
    if isinstance(root, str):
        if _SURROGATE_RE.search(root):
            replaced.append(())
            return _SURROGATE_RE.sub("\ufffd", root)
        return root
    if not isinstance(root, (dict, list)):
        return root
    if isinstance(root, dict):
        _replace_surrogate_keys(root, (), replaced)
    # Each entry holds a container, an iterator over its keys or indexes, its
    # path, and its nesting level (root is level 1), so strings and nested
    # containers are visited in document order with O(depth) stack space.
    root_keys: Iterator[str | int] = (
        iter(root) if isinstance(root, dict) else iter(range(len(root)))
    )
    pending: list[tuple[Any, Iterator[str | int], JsonPath, int]] = [
        (root, root_keys, (), 1)
    ]
    while pending:
        container, keys, path, depth = pending[-1]
        key = next(keys, None)
        if key is None:
            pending.pop()
            continue
        item = container[key]
        if isinstance(item, str):
            if _SURROGATE_RE.search(item):
                replaced.append((*path, key))
                container[key] = _SURROGATE_RE.sub("\ufffd", item)
        elif isinstance(item, (dict, list)):
            child_depth = depth + 1
            if child_depth > MAX_JSON_DEPTH:
                raise DocumentRejectedError(INVALID_JSON)
            child_path = (*path, key)
            if isinstance(item, dict):
                _replace_surrogate_keys(item, child_path, replaced)
                child_keys: Iterator[str | int] = iter(item)
            else:
                child_keys = iter(range(len(item)))
            pending.append((item, child_keys, child_path, child_depth))
    return root
