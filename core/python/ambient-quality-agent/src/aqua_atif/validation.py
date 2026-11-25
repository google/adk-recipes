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

"""Validation of ATIF documents against vendored Harbor v0.25.0 models.

Baseline validation is Harbor v0.25.0's `ATIF-v1.8` Pydantic models
(`aqua_atif.models`): every `1.x` document must satisfy all Harbor required
fields, field types, and cross-field validators, or `validate_document` raises
`DocumentRejectedError` and the document is `not_ingested`.

Required fields enforced by the Harbor models (missing or `null` -> rejected):

- `Trajectory`: `agent`, `steps` (at least one step); on each item of
  `subagent_trajectories`, a unique non-null `trajectory_id`.
- `Agent`: `name`, `version` (an empty string `""` passes validation and gets
  an `agent.version` `not_recorded` marker).
- `Step`: `step_id` (sequential starting at 1), `source` (`system`, `user`, or
  `agent`), `message` (string or `ContentPart` list).
- `ToolCall`: `tool_call_id`, `function_name`, `arguments` (dict; `{}` passes).
- `Observation`: `results` (list).
- `ContentPart`: `type`; `text` for `type="text"`; `source.media_type` and
  `source.path` for `type="image"` or `type="audio"`.

Harbor cross-field checks also reject a document: non-sequential `step_id`,
`source_call_id` not matching a `tool_call_id` in the same step, invalid ISO
8601 `timestamp`, agent-only fields on non-agent steps, and `metrics` or
`reasoning_content` when `llm_call_count == 0`. Missing optional fields (such
as `tool_definitions`, `timestamp`, `metrics`, `final_metrics`, or
`ObservationResult.content`) pass validation and receive `not_recorded` markers
in `annotate_document`; an omitted `schema_version` defaults silently to
`ATIF-v1.8` when `steps` is present.

AQuA applies three explicit exceptions on top of stock Harbor v0.25.0:

1. **Version rule and unknown fields.** Harbor's models list the versions they
   accept, so without a rule for newer versions each Harbor release would stop
   ingestion until AQuA updates. Every `1.x` document is checked against the
   v1.8 models, whatever version it declares, since Harbor's own converters
   declare older versions than the fields they write:

   | Declared version              | Rule                                       |
   | ----------------------------- | ------------------------------------------ |
   | `ATIF-v1.0` to `ATIF-v1.8`    | Strict: unknown fields/parts are rejected  |
   | `ATIF-v1.9` and later 1.x     | Unknown fields/parts kept in JSON, marked  |
   | `ATIF-v2.0` and later         | Rejected: "upgrade AQuA"                   |
   | A bare `1.7`                  | Read as `ATIF-v1.7`                        |
   | Any other value               | Rejected: "unknown schema_version"         |
   | None, with `steps`            | Read as `ATIF-v1.8`                        |
   | None, without `steps`         | Rejected: "not an ATIF document"           |

2. **ATIF-v1.6 subagent references.** `SubagentTrajectoryRef` may omit both
   `trajectory_id` and `trajectory_path`; `markers.find_missing_markers` marks
   it with `trajectory` (`not_recorded`) rather than rejecting the document.
3. **Producer `extra.aqua` interface.** `extra.aqua` on `agent`,
   `subagent_trajectory_ref`, `metrics`, and `final_metrics` is rejected with
   `"only AQuA writes extra.aqua"`. On a trajectory, step, tool call, or
   observation result, `validate_producer_aqua` accepts the typed producer
   fields (`missing`, `error`, `records_tool_errors`, and the `message` /
   `content` `FilePart` bridge) and rejects unknown keys, non-empty native
   fields, and conflicting error sources. AQuA expands producer `missing` names
   and writes its own annotation keys in `annotate_document`, after validation.

"Strict" means unknown fields, content part types, and media types fail
validation; values are coerced as Harbor's models coerce them (Pydantic's lax
mode: `"1"` is a valid step id). AQuA's code reads the parsed JSON, so where it
reads a value the models coerce, such as `llm_call_count`, it coerces it the
same way (`markers.read_llm_call_count`).

Each embedded subagent follows the rule for the version it declares itself.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Iterator
from typing import Any

from aqua_atif.markers import (
    MissingMarker,
    PendingMissingMarker,
    Reason,
)
from aqua_atif.models import (
    Agent,
    FinalMetrics,
    Metrics,
    Observation,
    ObservationResult,
    Step,
    SubagentTrajectoryRef,
    ToolCall,
    Trajectory,
)
from aqua_atif.parsing import DocumentRejectedError
from aqua_atif.paths import (
    HolderKind,
    JsonPath,
    format_location,
    format_path,
    list_holders,
)
from aqua_atif.producer import (
    PART_TYPES,
    iter_unknown_content_part_errors,
    validate_producer_aqua,
)
from pydantic import BaseModel, ValidationError

DEFAULT_SCHEMA_VERSION = "ATIF-v1.8"
LATEST_STRICT_MINOR = 8
NOT_ATIF = "not an ATIF document"
UNKNOWN_SCHEMA_VERSION = "unknown schema_version"

# At most nine digits per number, so a crafted version cannot reach the
# limit on converting long digit strings to int.
_VERSION_RE = re.compile(r"(?:ATIF-v)?(0|[1-9]\d{0,8})\.(0|[1-9]\d{0,8})")

MODELS_BY_KIND: dict[HolderKind, type[BaseModel]] = {
    HolderKind.TRAJECTORY: Trajectory,
    HolderKind.AGENT: Agent,
    HolderKind.STEP: Step,
    HolderKind.TOOL_CALL: ToolCall,
    HolderKind.RESULT: ObservationResult,
    HolderKind.SUBAGENT_REF: SubagentTrajectoryRef,
    HolderKind.METRICS: Metrics,
    HolderKind.FINAL_METRICS: FinalMetrics,
}


@dataclasses.dataclass(frozen=True, order=True)
class SchemaVersion:
    """A parsed ATIF schema version.

    Attributes:
        major: The major version.
        minor: The minor version.
    """

    major: int
    minor: int

    def __str__(self) -> str:
        """Formats the version as `ATIF-v<major>.<minor>`.

        Returns:
            The canonical spelling.
        """
        return f"ATIF-v{self.major}.{self.minor}"

    @property
    def is_strict(self) -> bool:
        """Whether unknown fields fail validation for this version."""
        return self.major == 1 and self.minor <= LATEST_STRICT_MINOR


def parse_schema_version(value: Any) -> SchemaVersion | None:
    """Parses a declared `schema_version`.

    Args:
        value: The declared value: `ATIF-v<major>.<minor>`, or a bare
            `<major>.<minor>`, without leading zeros and with at most nine
            digits per number.

    Returns:
        The version, or None when `value` has neither form or its major version
        is 0.
    """
    if not isinstance(value, str):
        return None
    match = _VERSION_RE.fullmatch(value)
    if match is None or match.group(1) == "0":
        return None
    return SchemaVersion(int(match.group(1)), int(match.group(2)))


def build_upgrade_reason(version: SchemaVersion) -> str:
    """Builds the rejection reason for a major version newer than AQuA reads.

    Args:
        version: The declared version.

    Returns:
        The reason, such as "ATIF-v2.0 is a newer major version than AQuA
        reads; upgrade AQuA".
    """
    return f"{version} is a newer major version than AQuA reads; upgrade AQuA"


@dataclasses.dataclass(frozen=True)
class ValidatedDocument:
    """A document that passed validation.

    Attributes:
        trajectory: The document as validated by the models. Use it to read
            the document; it is never dumped back into the stored JSON.
        schema_version: The root's version; `ATIF-v1.8` when it declares none.
        markers: `unsupported_version` markers for fields and parts that the
            v1.8 models do not define. Pass them to `annotate_document`.
    """

    trajectory: Trajectory
    schema_version: SchemaVersion
    markers: tuple[PendingMissingMarker, ...]


@dataclasses.dataclass(frozen=True)
class _UnknownItem:
    """A field or content part that the v1.8 models do not define.

    Attributes:
        location: Path from the document root, as rejection reasons write it.
        holder: Path of the object that gets the marker.
        path: MissingMarker path relative to `holder`.
        trajectory: Path of the trajectory that contains it.
        message: The rejection message in a strict document.
    """

    location: str
    holder: JsonPath
    path: str
    trajectory: JsonPath
    message: str


def validate_document(document: Any) -> ValidatedDocument:
    """Validates a parsed document against the Harbor models and version rule.

    Does not change `document`. Missing required Harbor fields, invalid field
    types, failed cross-field checks, unknown fields in `ATIF-v1.0`..`ATIF-v1.8`,
    versions `>= 2.0`, and invalid or disallowed `extra.aqua` keys all raise
    `DocumentRejectedError` so callers record the document as `not_ingested`.

    Args:
        document: The parsed JSON of a trajectory file, with any subagent
            that a source embedded from another file.

    Returns:
        The validated document and the `unsupported_version` markers validation
        found for `ATIF-v1.9+` unknown fields, content parts, or media types.

    Raises:
        DocumentRejectedError: With the rejection reason, such as "not an ATIF
            document", "unknown schema_version", the upgrade reason, a
            Pydantic error such as "agent.version: Field required" or
            "steps.2.tool_call: Extra inputs are not permitted", or an
            `extra.aqua` validation failure.
    """
    if not isinstance(document, dict):
        raise DocumentRejectedError(NOT_ATIF)
    if document.get("schema_version") is None and document.get("steps") is None:
        raise DocumentRejectedError(NOT_ATIF)
    versions = _resolve_versions(document, ())
    prepared = _drop_null_versions(document)
    try:
        trajectory = Trajectory.model_validate(prepared)
    except ValidationError as err:
        raise DocumentRejectedError(
            _format_validation_error(err, prepared)
        ) from err
    validate_producer_aqua(document)

    markers: list[PendingMissingMarker] = []
    for item in _iter_unknown_items(document):
        version = versions[item.trajectory]
        if version.is_strict:
            raise DocumentRejectedError(f"{item.location}: {item.message}")
        markers.append(
            PendingMissingMarker(
                item.holder,
                MissingMarker(
                    item.path, Reason.UNSUPPORTED_VERSION, detail=str(version)
                ),
            )
        )
    return ValidatedDocument(
        trajectory=trajectory,
        schema_version=versions[()],
        markers=tuple(markers),
    )


def _resolve_versions(
    trajectory: dict[str, Any], path: JsonPath
) -> dict[JsonPath, SchemaVersion]:
    """Resolves the version of a trajectory and of its embedded subagents.

    Args:
        trajectory: A root or embedded trajectory.
        path: Its path from the document root.

    Returns:
        The version of each trajectory, by path.

    Raises:
        DocumentRejectedError: When a declared version is unknown or a newer
            major version. For a subagent, the reason starts with its path.
    """
    declared = trajectory.get("schema_version")
    if declared is None:
        version = parse_schema_version(DEFAULT_SCHEMA_VERSION)
    else:
        version = parse_schema_version(declared)
    prefix = f"{format_location(path)}: " if path else ""
    if version is None:
        raise DocumentRejectedError(f"{prefix}{UNKNOWN_SCHEMA_VERSION}")
    if version.major > 1:
        raise DocumentRejectedError(f"{prefix}{build_upgrade_reason(version)}")
    versions = {path: version}
    subagents = trajectory.get("subagent_trajectories")
    for index, subagent in enumerate(
        subagents if isinstance(subagents, list) else ()
    ):
        if isinstance(subagent, dict):
            versions.update(
                _resolve_versions(
                    subagent, (*path, "subagent_trajectories", index)
                )
            )
    return versions


def _drop_null_versions(trajectory: dict[str, Any]) -> dict[str, Any]:
    """Copies the trajectories of a document without a null `schema_version`.

    The models fill an absent version with `ATIF-v1.8` but reject a null one,
    which the version rule reads as absent.

    Args:
        trajectory: A root or embedded trajectory.

    Returns:
        A shallow copy along the trajectory chain; other values are shared.
    """
    copied = {
        key: value
        for key, value in trajectory.items()
        if key != "schema_version" or value is not None
    }
    subagents = copied.get("subagent_trajectories")
    if isinstance(subagents, list):
        copied["subagent_trajectories"] = [
            _drop_null_versions(item) if isinstance(item, dict) else item
            for item in subagents
        ]
    return copied


def _format_validation_error(err: ValidationError, document: Any) -> str:
    """Formats the most specific Pydantic error as a rejection reason.

    A list `message` that fails inside a part also fails the string branch of
    the union, at a shorter location. The error with the longest location in
    the document is the one that names the field at fault.

    Args:
        err: The validation error.
        document: The validated input, used to drop union branch names from
            error locations.

    Returns:
        `<location>: <message>`, or the message alone for a root error.
    """
    best_location: JsonPath = ()
    best_message = ""
    for index, error in enumerate(err.errors()):
        location = _resolve_location(
            document, error.get("loc", ()), error.get("type") == "missing"
        )
        if index == 0 or len(location) > len(best_location):
            best_location = location
            best_message = str(error.get("msg", "")).removeprefix(
                "Value error, "
            )
    if not best_location:
        return best_message
    return f"{format_location(best_location)}: {best_message}"


def _resolve_location(
    document: Any, loc: tuple[Any, ...], is_missing: bool
) -> JsonPath:
    """Keeps the segments of a Pydantic location that exist in the input.

    Pydantic adds the names of union branches, such as `str` or
    `list[ContentPart]`, to error locations. A segment that the input does not
    have is such a name, unless the error is a missing field, whose name is
    the last segment.

    Args:
        document: The validated input.
        loc: The error location.
        is_missing: Whether the error is a missing required field.

    Returns:
        The location without union branch names.
    """
    location: list[str | int] = []
    current = document
    for position, segment in enumerate(loc):
        if isinstance(current, list) and isinstance(segment, int):
            if 0 <= segment < len(current):
                location.append(segment)
                current = current[segment]
                continue
        elif isinstance(current, dict) and segment in current:
            location.append(segment)
            current = current[segment]
            continue
        if (
            is_missing
            and position == len(loc) - 1
            and isinstance(current, dict)
        ):
            location.append(segment)
    return tuple(location)


def _iter_unknown_items(document: dict[str, Any]) -> Iterator[_UnknownItem]:
    """Walks a validated document for fields and parts the models do not define.

    Args:
        document: Root trajectory of a document that the models accepted.

    Yields:
        Each unknown field and content part, in document order.
    """
    for holder in list_holders(document):
        model = MODELS_BY_KIND[holder.kind]
        yield from _iter_unknown_fields(
            holder.value, model, holder.path, holder.path, (), holder.trajectory
        )
        if holder.kind == HolderKind.STEP:
            observation = holder.value.get("observation")
            if isinstance(observation, dict):
                yield from _iter_unknown_fields(
                    observation,
                    Observation,
                    (*holder.path, "observation"),
                    holder.path,
                    ("observation",),
                    holder.trajectory,
                )
            yield from _iter_unknown_parts(
                holder.value, "message", holder.path, holder.trajectory
            )
        elif holder.kind == HolderKind.RESULT:
            yield from _iter_unknown_parts(
                holder.value, "content", holder.path, holder.trajectory
            )


def _iter_unknown_fields(
    value: dict[str, Any],
    model: type[BaseModel],
    path: JsonPath,
    holder: JsonPath,
    relative: JsonPath,
    trajectory: JsonPath,
) -> Iterator[_UnknownItem]:
    """Yields the keys of an object that its model does not define.

    Args:
        value: The object.
        model: The model that defines its fields.
        path: Path of `value` from the document root.
        holder: Path of the object that gets the markers.
        relative: Path of `value` from `holder`.
        trajectory: Path of the trajectory that contains `value`.

    Yields:
        One item per unknown key.
    """
    for key in value:
        if key in model.model_fields:
            continue
        marker_path = format_path((*relative, key))
        yield _UnknownItem(
            location=format_location((*path, key)),
            holder=holder,
            path=marker_path,
            trajectory=trajectory,
            message="Extra inputs are not permitted",
        )


def _iter_unknown_parts(
    holder_value: dict[str, Any],
    field: str,
    holder: JsonPath,
    trajectory: JsonPath,
) -> Iterator[_UnknownItem]:
    """Yields unknown part types, media types and part fields of a part list.

    A part of an unknown type, or with a media type outside ATIF's lists, is
    marked as a whole, as `message[i]` or `content[i]`.

    Args:
        holder_value: The step or result.
        field: `message` or `content`.
        holder: Path of the step or result.
        trajectory: Path of the trajectory that contains it.

    Yields:
        The unknown items, in order.
    """
    parts = holder_value.get(field)
    if not isinstance(parts, list):
        return
    for index, part in enumerate(parts):
        if not isinstance(part, dict):
            continue
        part_path = (*holder, field, index)
        relative = (field, index)
        part_type = part.get("type")
        if part_type not in PART_TYPES:
            yield _UnknownItem(
                location=format_location((*part_path, "type")),
                holder=holder,
                path=format_path(relative),
                trajectory=trajectory,
                message="Input should be 'text', 'image' or 'audio'",
            )
            continue
        for loc_path, rel_path, message in iter_unknown_content_part_errors(
            part, part_path, relative
        ):
            yield _UnknownItem(
                location=format_location(loc_path),
                holder=holder,
                path=format_path(rel_path),
                trajectory=trajectory,
                message=message,
            )
