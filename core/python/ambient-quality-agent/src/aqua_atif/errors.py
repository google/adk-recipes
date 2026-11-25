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

"""Tool, step and run errors: the producer conventions AQuA reads, and the one
field it writes.

ATIF v1 has no standard error field, so producers record tool failures in
`observation.results[].extra` using three conventions, checked in order:

1. `error` (string or object): carries structured `type` and `message` details
   in the same shape used for step and run errors. It counts as a failure when
   it is a non-empty string or an object with a non-empty string `type` or
   `message`.
2. `is_error` (boolean), with optional `error_type`: supports ATIF producers
   that record a boolean flag (such as Harbor's Pi and Kimi Code converters).
   It counts as a failure when `true`.
3. `aqua.error` (`{"type", "message"?}`): converters write the normalized
   error directly under `extra.aqua.error` (mutually exclusive with
   `extra.error` and `extra.is_error` on input).

`error` is checked first because it carries richer failure details than a bare
flag. AQuA evaluates production agents rather than every external Harbor
harness, so it intentionally ignores harness-specific keys that Harbor's
converters emit (such as `tool_result_is_error` from Harbor's Claude Code
converter) as well as mistyped values (such as `is_error: "yes"`). Those keys
stay untouched as the producer's own data.

A failed model call or agent step writes `extra.error` on the step, and a failed
run writes it on the root. AQuA writes its reading of every convention to
`extra.aqua.error` as `{"type", "message"?}`, and `extract_error` reads only
that field, so no other code needs to know the conventions.

What a producer wrote in a result's `extra`, and the `extra.aqua.error` that
`add_errors` adds beside it::

    {"error": "boom"}                      {"type": null, "message": "boom"}
    {"error": {"type": "TIMEOUT",          {"type": "TIMEOUT",
               "message": "30 s"}}          "message": "30 s"}
    {"error": ""}                          nothing: not a failure
    {"is_error": true,
     "error_type": "EACCES"}               {"type": "EACCES"}
    {"is_error": true}                     {"type": null}
    {"is_error": false}                    nothing: a recorded success

`message` is set only when the producer wrote one; the tool output stays in the
result's `content`. `extra.error` on a step or the root maps the same way::

    step.extra    {"error": {"type": "RESOURCE_EXHAUSTED", "message": "429"}}
      -> on the step:               extra.aqua.error = the same object

    root.extra    {"error": "exit 137"}
      -> on the root:   extra.aqua.error = {"type": null, "message": "exit 137"}
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any

from aqua_atif.paths import (
    RECORDS_TOOL_ERRORS,
    Holder,
    HolderKind,
    get_aqua,
    list_holders,
    remove_empty_aqua,
)


@dataclasses.dataclass(frozen=True)
class AquaError:
    """A failure, as AQuA records it in `extra.aqua.error`.

    Attributes:
        type: The producer's error type, or None when omitted.
        message: The producer's error message, or None when omitted. AQuA does
            not copy a tool's output here, since readers have `content`.
    """

    type: str | None = None
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Formats the error as stored in `extra.aqua.error`.

        Returns:
            `{"type": ...}`, with `message` only when it is set.
        """
        payload: dict[str, Any] = {"type": self.type}
        if self.message is not None:
            payload["message"] = self.message
        return payload


def extract_error(holder: Mapping[str, Any]) -> AquaError | None:
    """Extracts AQuA's error reading from a result, a step or a trajectory.

    Reads only `extra.aqua.error`, which `add_errors` writes for every
    producer convention, so it takes the annotated JSON, never a model built
    before annotation.

    Args:
        holder: An observation result, step or trajectory of an annotated
            document.

    Returns:
        The error, or None when the object did not fail.
    """
    extra = holder.get("extra")
    if not isinstance(extra, Mapping):
        return None
    aqua = extra.get("aqua")
    if not isinstance(aqua, Mapping):
        return None
    error = aqua.get("error")
    if not isinstance(error, Mapping):
        return None
    error_type = error.get("type")
    message = error.get("message")
    return AquaError(
        type=error_type if isinstance(error_type, str) else None,
        message=message if isinstance(message, str) else None,
    )


def add_errors(document: dict[str, Any]) -> None:
    """Writes `extra.aqua.error` on every failed result, step and trajectory.

    Covers embedded subagents. Preserves producer-written `extra.aqua.error`
    when no legacy `extra.error` or `extra.is_error` is present, and removes
    any invalid or absent error.

    Args:
        document: Root trajectory of a validated ATIF document, changed in
            place.
    """
    for holder in list_holders(document):
        if holder.kind == HolderKind.RESULT:
            error = _read_result_error(holder.value)[1]
        elif holder.kind in (HolderKind.STEP, HolderKind.TRAJECTORY):
            error = _read_step_or_trajectory_error(holder.value)
        else:
            continue
        _set_error(holder, error)


def records_tool_errors(document: Mapping[str, Any]) -> bool:
    """Checks whether a document shows that its producer records tool failures.

    Returns True if the root trajectory declares
    `extra.aqua.records_tool_errors: true`, or if any result in the document
    has `extra.aqua.error`, a string or object `extra.error`, or a boolean
    `extra.is_error` (including `is_error: false`). Values of another type,
    such as `is_error: "yes"`, belong to the producer and are ignored.

    Args:
        document: Root trajectory of the document.

    Returns:
        Whether a result without a tool-error key can be read as a success.
    """
    extra = document.get("extra")
    root_aqua = extra.get("aqua") if isinstance(extra, Mapping) else None
    if (
        isinstance(root_aqua, Mapping)
        and root_aqua.get(RECORDS_TOOL_ERRORS) is True
    ):
        return True
    for holder in list_holders(document):
        if (
            holder.kind == HolderKind.RESULT
            and _read_result_error(holder.value)[0]
        ):
            return True
    return False


def _read_result_error(
    result: Mapping[str, Any],
) -> tuple[bool, AquaError | None]:
    """Reads the tool-error conventions on one observation result.

    Args:
        result: The observation result.

    Returns:
        `(recorded, error)`: whether any convention applies, and the error when
        the result failed.
    """
    extra = result.get("extra")
    extra = extra if isinstance(extra, Mapping) else {}
    if isinstance(extra.get("error"), (str, Mapping)):
        return True, _read_extra_error(extra)
    if isinstance(extra.get("is_error"), bool):
        if not extra["is_error"]:
            return True, None
        error_type = extra.get("error_type")
        return True, AquaError(
            type=error_type
            if isinstance(error_type, str) and error_type
            else None
        )
    producer_error = _read_aqua_error(result)
    if producer_error is not None:
        return True, producer_error
    return False, None


def _read_step_or_trajectory_error(
    holder: Mapping[str, Any],
) -> AquaError | None:
    """Reads `extra.error` or producer-written `extra.aqua.error`.

    Args:
        holder: A step or trajectory object.

    Returns:
        The normalized error, or None when absent.
    """
    extra = holder.get("extra")
    if isinstance(extra, Mapping) and isinstance(
        extra.get("error"), (str, Mapping)
    ):
        return _read_extra_error(extra)
    return _read_aqua_error(holder)


def _read_aqua_error(holder: Mapping[str, Any]) -> AquaError | None:
    """Reads a non-empty `extra.aqua.error` if present on `holder`.

    Args:
        holder: A result, step, or trajectory.

    Returns:
        The error when `extra.aqua.error` has a non-empty `type` or `message`,
        otherwise None.
    """
    error = extract_error(holder)
    if error is None:
        return None
    error_type = error.type if error.type else None
    message = error.message if error.message else None
    if not error_type and not message:
        return None
    return AquaError(type=error_type, message=message)


def _read_extra_error(extra: Any) -> AquaError | None:
    """Reads the `error` convention from an `extra` object.

    Args:
        extra: The `extra` of a result, step or trajectory.

    Returns:
        The error when `extra.error` is a non-empty string or an object with a
        non-empty string `type` or `message`; otherwise None.
    """
    if not isinstance(extra, Mapping):
        return None
    error = extra.get("error")
    if isinstance(error, str):
        return AquaError(message=error) if error else None
    if not isinstance(error, Mapping):
        return None
    error_type = error.get("type")
    message = error.get("message")
    error_type = (
        error_type if isinstance(error_type, str) and error_type else None
    )
    message = message if isinstance(message, str) and message else None
    if not error_type and not message:
        return None
    return AquaError(type=error_type, message=message)


def _set_error(holder: Holder, error: AquaError | None) -> None:
    """Writes or removes `extra.aqua.error` on a holder.

    Args:
        holder: A result, step or trajectory.
        error: The reading to write; None removes the field.
    """
    if error is not None:
        aqua = get_aqua(holder.value, create=True)
        if aqua is not None:
            aqua["error"] = error.to_dict()
        return
    aqua = get_aqua(holder.value)
    if aqua is not None and "error" in aqua:
        del aqua["error"]
        remove_empty_aqua(holder.value)
