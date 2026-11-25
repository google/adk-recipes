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

"""Producer interface for `extra.aqua` in ATIF v1.8 documents.

Each `TypedDict` returns a plain `dict` for `extra["aqua"]`, leaving other
producer keys in `extra` untouched. Checked statically by `ty`;
`validate_producer_aqua` enforces cross-field rules on ingestion, rejects
unknown keys, and `annotate_document` expands each
`missing` field name to `{"path": name, "reason": "not_recorded"}`.
"""

from __future__ import annotations

import typing
from collections.abc import Iterator
from typing import Any, Literal, Required, TypedDict

from aqua_atif.models import (
    AudioMediaType,
    AudioSource,
    ContentPart,
    ImageMediaType,
    ImageSource,
)
from aqua_atif.parsing import DocumentRejectedError
from aqua_atif.paths import (
    AQUA_KEY,
    Holder,
    HolderKind,
    JsonPath,
    format_location,
    list_holders,
)
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    ValidationError,
)

AQUA_KEY_IN_FILE = "only AQuA writes extra.aqua"
"""Rejection reason for `extra.aqua` on holders that accept none from input."""

PART_TYPES = ("text", "image", "audio")
IMAGE_MEDIA_TYPES: tuple[str, ...] = typing.get_args(ImageMediaType)
AUDIO_MEDIA_TYPES: tuple[str, ...] = typing.get_args(AudioMediaType)

_BRIDGE_HOLDER_RULES: dict[HolderKind, tuple[str, tuple[Any, ...]]] = {
    HolderKind.STEP: ("message", ("",)),
    HolderKind.RESULT: ("content", (None, "")),
}


class AquaErrorDict(TypedDict, total=False):
    """Failure on a trajectory, step, or tool result (`type` must be non-empty)."""

    type: Required[str]
    message: str


class FileSource(TypedDict):
    """Attachment reference (relative path or allowed `gs://` URL)."""

    media_type: str
    path: str


class FilePart(TypedDict):
    """ATIF v1.8 bridge part for media types outside text, image, and audio."""

    type: Literal["file"]
    source: FileSource


class TrajectoryAqua(TypedDict, total=False):
    """Goes on root `doc["extra"]["aqua"]`."""

    records_tool_errors: Literal[True]
    """True keeps `TOOL_ERROR_CHECK` on for 0-error runs."""

    error: AquaErrorDict


class StepAqua(TypedDict, total=False):
    """Goes on `step["extra"]["aqua"]`.

    `missing` and `message` are mutually exclusive, both require
    `step["message"] == ""`, and `message` must include at least one `FilePart`.
    """

    missing: list[Literal["message"]]
    message: list[dict[str, object] | FilePart]
    error: AquaErrorDict


class ToolCallAqua(TypedDict, total=False):
    """Goes on `tool_call["extra"]["aqua"]`. Requires `arguments == {}`."""

    missing: list[Literal["arguments"]]


class ResultAqua(TypedDict, total=False):
    """Goes on `result["extra"]["aqua"]`.

    `missing` and `content` are mutually exclusive, both require `content` to
    be empty or omitted, `content` must include at least one `FilePart`, and
    `error` conflicts with `extra.error` or `extra.is_error`.
    """

    missing: list[Literal["content"]]
    content: list[dict[str, object] | FilePart]
    error: AquaErrorDict


class _ProducerErrorModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: StrictStr = Field(min_length=1)
    message: StrictStr | None = None


class _FileSourceModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    media_type: StrictStr = Field(min_length=1)
    path: StrictStr = Field(min_length=1)


class _FilePartModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["file"]
    source: _FileSourceModel


class _TrajectoryAquaModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    records_tool_errors: Literal[True] | None = None
    error: _ProducerErrorModel | None = None


class _StepAquaModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    missing: list[Literal["message"]] | None = Field(default=None, min_length=1)
    message: list[dict[str, Any]] | None = Field(default=None, min_length=1)
    error: _ProducerErrorModel | None = None


class _ToolCallAquaModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    missing: list[Literal["arguments"]] | None = Field(
        default=None, min_length=1
    )


class _ResultAquaModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    missing: list[Literal["content"]] | None = Field(default=None, min_length=1)
    content: list[dict[str, Any]] | None = Field(default=None, min_length=1)
    error: _ProducerErrorModel | None = None


_MODELS_BY_HOLDER: dict[HolderKind, type[BaseModel]] = {
    HolderKind.TRAJECTORY: _TrajectoryAquaModel,
    HolderKind.STEP: _StepAquaModel,
    HolderKind.TOOL_CALL: _ToolCallAquaModel,
    HolderKind.RESULT: _ResultAquaModel,
}


def validate_producer_aqua(document: dict[str, Any]) -> None:
    """Validates producer-written `extra.aqua` keys across a document.

    Args:
        document: Root trajectory of a document that Harbor's models accepted.

    Raises:
        DocumentRejectedError: On the first invalid or disallowed `extra.aqua`,
            in document order.
    """
    for holder in list_holders(document):
        extra = holder.value.get("extra")
        if not isinstance(extra, dict) or AQUA_KEY not in extra:
            continue
        _validate_holder_aqua(holder, extra)


def iter_unknown_content_part_errors(
    part: dict[str, Any], part_path: JsonPath, relative_part: JsonPath
) -> Iterator[tuple[JsonPath, JsonPath, str]]:
    """Yields unknown fields and media types on a v1.8 `ContentPart`.

    Shared by `validation._iter_unknown_parts` and `_validate_bridge_parts`.
    Assumes `part["type"]` is in `PART_TYPES` and `ContentPart.model_validate`
    succeeded.

    Args:
        part: Content part dictionary.
        part_path: Path of `part` from the document root.
        relative_part: Path of `part` from its holding step or result.

    Yields:
        `(location_path, marker_relative_path, message)` for each unknown field
        or media type.
    """
    for key in part:
        if key not in ContentPart.model_fields:
            yield (
                (*part_path, key),
                (*relative_part, key),
                "Extra inputs are not permitted",
            )
    part_type = part.get("type")
    source = part.get("source")
    if part_type == "text" or not isinstance(source, dict):
        return
    source_model, known = (
        (ImageSource, IMAGE_MEDIA_TYPES)
        if part_type == "image"
        else (AudioSource, AUDIO_MEDIA_TYPES)
    )
    media_type = source_model.model_validate(source).media_type
    if media_type not in known:
        allowed = ", ".join(f"'{item}'" for item in known[:-1])
        yield (
            (*part_path, "source", "media_type"),
            relative_part,
            f"Input should be {allowed} or '{known[-1]}'",
        )
    for key in source:
        if key not in source_model.model_fields:
            yield (
                (*part_path, "source", key),
                (*relative_part, "source", key),
                "Extra inputs are not permitted",
            )


def _validate_holder_aqua(holder: Holder, extra: dict[str, Any]) -> None:
    """Validates `extra.aqua` on one holder object.

    Args:
        holder: The object holding `extra.aqua`.
        extra: Its `extra` dictionary.

    Raises:
        DocumentRejectedError: When `extra.aqua` is disallowed or invalid.
    """
    aqua_path: JsonPath = (*holder.path, "extra", AQUA_KEY)
    aqua_loc = format_location(aqua_path)
    model = _MODELS_BY_HOLDER.get(holder.kind)
    if model is None:
        raise DocumentRejectedError(f"{aqua_loc}: {AQUA_KEY_IN_FILE}")

    aqua = extra[AQUA_KEY]
    if not isinstance(aqua, dict):
        raise DocumentRejectedError(
            f"{aqua_loc}: Input should be a valid dictionary"
        )
    if not aqua:
        raise DocumentRejectedError(f"{aqua_loc}: extra.aqua must not be empty")

    try:
        model.model_validate(aqua)
    except ValidationError as err:
        raise DocumentRejectedError(_format_aqua_error(aqua_path, err)) from err

    for key, value in aqua.items():
        if value is None:
            key_loc = format_location((*aqua_path, key))
            raise DocumentRejectedError(f"{key_loc}: Input should not be None")

    _check_cross_field_rules(holder, extra, aqua, aqua_path)


def _check_cross_field_rules(
    holder: Holder,
    extra: dict[str, Any],
    aqua: dict[str, Any],
    aqua_path: JsonPath,
) -> None:
    """Checks rules linking `extra.aqua` to fields on its holding object.

    Args:
        holder: The holding ATIF object.
        extra: Its `extra` dictionary.
        aqua: Its `extra.aqua` dictionary.
        aqua_path: Path of `extra.aqua` from the document root.

    Raises:
        DocumentRejectedError: When a cross-field rule is violated.
    """
    aqua_loc = format_location(aqua_path)
    extra_loc = format_location((*holder.path, "extra"))

    if "error" in aqua:
        if "error" in extra:
            raise DocumentRejectedError(
                f"{extra_loc}: cannot set both extra.error and extra.aqua.error"
            )
        if holder.kind == HolderKind.RESULT and isinstance(
            extra.get("is_error"), bool
        ):
            raise DocumentRejectedError(
                f"{extra_loc}: cannot set both extra.is_error and "
                "extra.aqua.error"
            )

    if holder.kind == HolderKind.TOOL_CALL:
        if "missing" in aqua and holder.value.get("arguments") != {}:
            raise DocumentRejectedError(
                f"{aqua_loc}: arguments is not empty, so it is not missing"
            )
        return

    bridge = _BRIDGE_HOLDER_RULES.get(holder.kind)
    if bridge is None:
        return
    field, empty_values = bridge
    if "missing" in aqua and field in aqua:
        raise DocumentRejectedError(
            f"{aqua_loc}: cannot set both missing and {field}"
        )
    if ("missing" in aqua or field in aqua) and holder.value.get(
        field
    ) not in empty_values:
        raise DocumentRejectedError(
            f"{aqua_loc}: {field} is not empty, so it is neither missing "
            "nor replaced"
        )
    if field in aqua:
        _validate_bridge_parts(aqua[field], (*aqua_path, field), field)


def _validate_bridge_parts(
    parts: list[dict[str, Any]],
    field_path: JsonPath,
    field: str,
) -> None:
    """Validates an `extra.aqua.message` or `extra.aqua.content` part list.

    Each entry is a `FilePart` or a v1.8 `ContentPart` (`text`, `image`,
    `audio`). At least one entry must be a `FilePart`.

    Args:
        parts: List of part dictionaries.
        field_path: Path of the part list from the document root.
        field: `message` or `content`.

    Raises:
        DocumentRejectedError: When any part is invalid or no `FilePart` is
            present.
    """
    has_file_part = False
    for index, part in enumerate(parts):
        part_path = (*field_path, index)
        part_type = part.get("type")
        if part_type == "file":
            try:
                _FilePartModel.model_validate(part)
            except ValidationError as err:
                raise DocumentRejectedError(
                    _format_aqua_error(part_path, err)
                ) from err
            has_file_part = True
            continue
        if part_type not in PART_TYPES:
            loc = format_location((*part_path, "type"))
            raise DocumentRejectedError(
                f"{loc}: Input should be 'text', 'image', 'audio' or 'file'"
            )
        try:
            ContentPart.model_validate(part)
        except ValidationError as err:
            raise DocumentRejectedError(
                _format_aqua_error(part_path, err)
            ) from err
        for loc_path, _, message in iter_unknown_content_part_errors(
            part, part_path, ()
        ):
            raise DocumentRejectedError(
                f"{format_location(loc_path)}: {message}"
            )

    if not has_file_part:
        field_loc = format_location(field_path)
        raise DocumentRejectedError(
            f"{field_loc}: every part fits ATIF's own {field}; write them there"
        )


def _format_aqua_error(prefix: JsonPath, err: ValidationError) -> str:
    """Formats the first Pydantic error under `prefix` as a rejection reason.

    Args:
        prefix: Path of the validated object from the document root.
        err: The Pydantic validation error.

    Returns:
        `<location>: <message>`.
    """
    first = err.errors()[0]
    loc = (
        *prefix,
        *(seg for seg in first.get("loc", ()) if isinstance(seg, (str, int))),
    )
    message = str(first.get("msg", "")).removeprefix("Value error, ")
    return f"{format_location(loc)}: {message}"
