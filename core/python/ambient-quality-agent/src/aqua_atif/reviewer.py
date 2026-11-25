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

"""The document the session reviewer reads, derived from the stored one.

The cause of missing data matters to the people who run AQuA, but it can bias
a review, so this view keeps only which data is missing.

| In the stored document              | In the reviewer's document            |
| ----------------------------------- | ------------------------------------- |
| `extra.aqua.missing` markers        | Their paths only, on the same object  |
| `extra.aqua.error`                  | Kept: the one field to read failures  |
| `extra.aqua.message` / `content`    | Kept: ordered parts for the `FilePart`|
|                                     | ATIF v1.8 bridge                      |
| `extra.aqua.records_tool_errors`    | Removed                               |
| The producer's error keys that      | Removed: a boolean `is_error` with    |
| `extra.aqua.error` was read from    | its `error_type`, and an `error` with |
|                                     | nothing but `type` and `message`      |
| Any other key in `extra`, such as   | Kept as written                       |
| an error key AQuA does not read     |                                       |
| (`tool_result_is_error`)            |                                       |
| Fields newer than v1.8              | Removed; their `unsupported_version`  |
|                                     | paths stay in `missing`               |

A root's `extra.aqua`, stored and as the reviewer reads it::

    {"records_tool_errors": true,                {"missing": [
     "missing": [                                   "agent.tool_definitions"]}
       {"path": "agent.tool_definitions",
        "reason": "not_recorded"}]}

A step's::

    {"missing": [{"path": "message[1]",          {"missing": ["message[1]"]}
                  "reason": "read_error",
                  "detail": "403"}]}

A failed result's `extra`::

    {"is_error": true,                           {"aqua": {"error":
     "error_type": "EACCES",                       {"type": "EACCES"}}}
     "aqua": {"error": {"type": "EACCES"}}}

    {"error": {"type": "TIMEOUT",                {"error": {"type": "TIMEOUT",
               "code": 504},                                "code": 504},
     "aqua": {"error": {"type": "TIMEOUT"}}}      "aqua": {"error":
                                                   {"type": "TIMEOUT"}}}

The second `error` stays because it holds a `code`, which `extra.aqua.error`
does not carry.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aqua_atif.markers import extract_markers
from aqua_atif.models import AudioSource, ContentPart, ImageSource, Observation
from aqua_atif.paths import (
    AQUA_KEY,
    Holder,
    HolderKind,
    list_holders,
)
from aqua_atif.validation import MODELS_BY_KIND, PART_TYPES
from pydantic import BaseModel

_PARTS_FIELDS = {HolderKind.STEP: "message", HolderKind.RESULT: "content"}


def build_reviewer_document(document: Mapping[str, Any]) -> dict[str, Any]:
    """Builds the document that the session reviewer reads.

    Args:
        document: A stored document (the producer's JSON plus `extra.aqua`).

    Returns:
        A new document; `document` is not changed.
    """
    view = _copy_json(document)
    for holder in list_holders(view):
        _remove_read_error_keys(holder)
        _rewrite_aqua(holder)
        _remove_unknown_fields(holder)
    return view


def _copy_json(document: Mapping[str, Any]) -> dict[str, Any]:
    """Copies a parsed JSON document without recursion.

    A document may nest 256 levels, which `copy.deepcopy` would turn into
    about 500 Python frames on top of the caller's.

    Args:
        document: The document.

    Returns:
        A copy that shares no object or list with `document`.
    """
    root: dict[str, Any] = {}
    stack: list[tuple[Any, Any]] = [(document, root)]
    while stack:
        source, target = stack.pop()
        items = (
            source.items() if isinstance(source, Mapping) else enumerate(source)
        )
        for key, value in items:
            if isinstance(value, Mapping):
                value_copy: Any = {}
            elif isinstance(value, list):
                value_copy = [None] * len(value)
            else:
                value_copy = value
            target[key] = value_copy
            if isinstance(value, (Mapping, list)):
                stack.append((value, value_copy))
    return root


def _remove_unknown_fields(holder: Holder) -> None:
    """Removes the fields that the v1.8 models do not define from one object.

    Covers the object itself, a step's `observation` and `message` parts, and
    an observation result's `content` parts. A part of an unknown type is kept
    whole, so that marker paths such as `message[2]` still point at the right
    part.

    Args:
        holder: The object, changed in place.
    """
    _remove_keys(holder.value, MODELS_BY_KIND[holder.kind])
    if holder.kind == HolderKind.STEP and isinstance(
        holder.value.get("observation"), dict
    ):
        _remove_keys(holder.value["observation"], Observation)
    parts = holder.value.get(_PARTS_FIELDS.get(holder.kind, ""))
    for part in parts if isinstance(parts, list) else ():
        if not isinstance(part, dict) or part.get("type") not in PART_TYPES:
            continue
        _remove_keys(part, ContentPart)
        source = part.get("source")
        if isinstance(source, dict):
            _remove_keys(
                source, ImageSource if part["type"] == "image" else AudioSource
            )


def _remove_keys(value: dict[str, Any], model: type[BaseModel]) -> None:
    """Removes the keys that a model does not define.

    Args:
        value: The object, changed in place.
        model: The model that defines its fields.
    """
    for key in [key for key in value if key not in model.model_fields]:
        del value[key]


# The flags on a result that `aqua_atif.errors` reads into `extra.aqua.error`.
_RESULT_ERROR_FLAGS = ("is_error", "error_type")


def _remove_read_error_keys(holder: Holder) -> None:
    """Removes the producer's error keys that `extra.aqua.error` stands for.

    The stored document keeps them; the reviewer reads failures from
    `extra.aqua.error` only. Keys AQuA did not read stay as written:
    unrecognized keys, non-boolean `is_error`, and `is_error`/`error_type` when
    `extra.error` took precedence.

    Args:
        holder: A result, step or trajectory, changed in place.
    """
    extra = holder.value.get("extra")
    if not isinstance(extra, dict):
        return
    removed: list[str] = []
    if holder.kind == HolderKind.RESULT:
        if isinstance(extra.get("error"), (str, dict)):
            pass
        elif isinstance(extra.get("is_error"), bool):
            removed.extend(key for key in _RESULT_ERROR_FLAGS if key in extra)
    elif holder.kind not in (HolderKind.STEP, HolderKind.TRAJECTORY):
        return
    if "error" in extra and _is_covered_error(extra["error"]):
        removed.append("error")
    for key in removed:
        del extra[key]
    if removed and not extra:
        del holder.value["extra"]


def _is_covered_error(error: Any) -> bool:
    """Checks whether `extra.aqua.error` carries all that an `error` says.

    Args:
        error: A producer's `extra.error`.

    Returns:
        True for null, a string, or an object whose only keys are `type` and
        `message` with string or null values.
    """
    if error is None or isinstance(error, str):
        return True
    return isinstance(error, dict) and all(
        key in ("type", "message") and (value is None or isinstance(value, str))
        for key, value in error.items()
    )


def _rewrite_aqua(holder: Holder) -> None:
    """Rewrites one object's `extra.aqua` into the reviewer's form.

    Args:
        holder: The object, changed in place.
    """
    extra = holder.value.get("extra")
    if not isinstance(extra, dict) or not isinstance(extra.get(AQUA_KEY), dict):
        return
    aqua = extra[AQUA_KEY]
    view: dict[str, Any] = {}
    paths = list(
        dict.fromkeys(marker.path for marker in extract_markers(holder.value))
    )
    if paths:
        view["missing"] = paths
    if "error" in aqua:
        view["error"] = aqua["error"]
    bridge_field = _PARTS_FIELDS.get(holder.kind)
    if bridge_field is not None and bridge_field in aqua:
        view[bridge_field] = aqua[bridge_field]
    if view:
        extra[AQUA_KEY] = view
    else:
        del extra[AQUA_KEY]
        if not extra:
            del holder.value["extra"]
