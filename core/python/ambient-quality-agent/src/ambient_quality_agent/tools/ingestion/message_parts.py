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

"""Convert trace message parts into genai `Part` objects.

Handles the typed parts written by the google-genai instrumentor and ADK,
and native genai parts in snake_case or camelCase. Bytes embedded directly in a
message part become text placeholders, so the eval case keeps no attachment
payloads from them. Bytes nested deeper, such as inside
``function_response.parts``, are not rewritten.

The module also defines the ``function_response`` names that ingestion assigns
to runtime failures; the review rules refer to them by name.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import re
import urllib.parse
from collections.abc import Mapping
from typing import Any, NamedTuple

from google.genai.types import (
    CodeExecutionResult,
    ExecutableCode,
    FileData,
    Part,
    ToolCall,
    ToolResponse,
    ToolType,
)
from pydantic import ValidationError

logger = logging.getLogger(__name__)

_PART_FIELDS = frozenset(Part.model_fields)
# Native parts arrive snake_case (Python ADK) or camelCase (ADK Go / TS, REST).
_PART_INPUT_KEYS = _PART_FIELDS | frozenset(
    field.alias for field in Part.model_fields.values() if field.alias
)
_TOOL_TYPE_VALUES = frozenset(tool_type.value for tool_type in ToolType)
_SERVER_TOOL_CALL = "server_tool_call"
_SERVER_TOOL_RESPONSE = "server_tool_call_response"
_CODE_EXECUTION = "code_execution"
# Server-tool payload fields each native type carries. A payload holding
# anything else is kept as JSON text so that field is not lost.
_CODE_FIELDS = frozenset({"type", "code", "language"})
_CODE_RESULT_FIELDS = frozenset({"type", "outcome", "output"})
_TOOL_CALL_FIELDS = frozenset({"type", "arguments"})
_TOOL_RESPONSE_FIELDS = frozenset({"type", "response"})
_DEFAULT_MIME_TYPE = "application/octet-stream"
_DATA_URI_PREFIX = "data:"
_PLACEHOLDER_MIME_TYPE = r"(?P<mime_type>[^,>]+)"
_INLINE_DATA_DETAIL = r"(?P<digest>\d+ bytes, sha256:[0-9a-f]{8})|size unknown"
_INLINE_DATA_PLACEHOLDER = re.compile(
    rf"<attachment: {_PLACEHOLDER_MIME_TYPE}, (?:{_INLINE_DATA_DETAIL})>"
)
# Every placeholder form this module emits: inline data, an uploaded file's ID,
# or a bare MIME type.
_ATTACHMENT_PLACEHOLDER = re.compile(
    rf"<attachment: {_PLACEHOLDER_MIME_TYPE}"
    rf"(?:, (?:{_INLINE_DATA_DETAIL}|file_id=.+))?>"
)

LLM_ERROR_RESPONSE = "llm_error"
AGENT_ERROR_RESPONSE = "agent_error"
INVOCATION_ERROR_RESPONSE = "invocation_error"
RUNTIME_ERROR_RESPONSES = frozenset(
    {LLM_ERROR_RESPONSE, AGENT_ERROR_RESPONSE, INVOCATION_ERROR_RESPONSE}
)
"""``function_response`` names ingestion assigns to a failure the agent runtime
recorded, not a tool result. The session review rules refer to them by name."""


class InlineDataPlaceholder(NamedTuple):
    """The details an inline-data placeholder records about its attachment."""

    mime_type: str
    has_digest: bool
    """Whether the bytes were decoded, so the placeholder names their size and
    digest."""


def parse_part(part: Any, tool_call_ids: dict[str, str]) -> Part | None:
    """Convert one trace message part into a genai `Part`.

    Priority order:

    1. OTEL-specific shapes keyed by ``type`` (see `_parse_typed_part`). An
       unrecognized ``type`` falls through to step 2.
    2. Gemini API parts in snake_case or camelCase, validated by the
       SDK's `Part` model (unknown fields ignored), supporting any part field
       (``text``, ``function_call``, ``function_response``, ``file_data``,
       ``executable_code``, ``thought``, ...). Inline data and ``data:`` file
       URIs become a text placeholder (see `_build_inline_data_placeholder`).
    3. Flat ``{"content": "..."}`` text fallback.

    Args:
        part: Raw part object or mapping.
        tool_call_ids: Tool call ID to function name map, updated in place
            by each ``tool_call`` part. Share one map across a span's messages
            so a later ``tool_call_response`` that carries only the ID
            recovers its name.

    Returns:
        SDK Part object, or None if part cannot be parsed.
    """
    if not isinstance(part, Mapping):
        return None

    part_type = part.get("type")
    if part_type is not None:
        try:
            typed = _parse_typed_part(part, part_type, tool_call_ids)
        except ValidationError:
            logger.warning("Failed to parse typed part; keeping it as text.")
            return _part_to_json_text(part)
        if typed is not None:
            return typed

    # 2. Gemini API part.
    known = {
        k: v for k, v in part.items() if k in _PART_INPUT_KEYS and v is not None
    }
    if known:
        # Checked on the raw mapping: `Part` rejects data that is not base64,
        # such as ADK's "<not serializable>" sentinel, and would drop the part.
        binary = _build_native_binary_placeholder(known)
        if binary is not None:
            return binary
        try:
            parsed = Part.model_validate(known)
        except ValidationError:
            logger.warning("Failed to parse native Part from trace payload.")
        else:
            if _has_part_data(parsed):
                return parsed

    # 3. Flat OTEL text: {"content": "..."} with no "type".
    if part.get("content") is not None and part_type is None:
        return Part(text=str(part["content"]))
    return None


def _parse_typed_part(
    part: Mapping[str, Any], part_type: Any, tool_call_ids: dict[str, str]
) -> Part | None:
    """Convert an OTEL part keyed by ``type`` into a genai `Part`.

    Covers the google-genai instrumentor shapes (``text``, ``uri``, ``blob``,
    ``file``, ``reasoning``, ``tool_call``, ``tool_call_response``,
    ``server_tool_call``, ``server_tool_call_response``) and ADK's
    ``file_data``. A ``tool_call`` records its ``id`` -> name so a later
    ``tool_call_response`` (which carries only the id) can recover the
    function name.

    Args:
        part: Raw part mapping.
        part_type: The part's ``type`` value.
        tool_call_ids: Mapping of tool call IDs to tool names.

    Returns:
        SDK Part object, or None if the type is unrecognized or the part has
        no usable payload.
    """
    if part_type == "text" and part.get("content") is not None:
        return Part(text=str(part["content"]))
    if part_type == "tool_call":
        name = part.get("name") or ""
        call_id = part.get("id")
        if call_id is not None and name:
            tool_call_ids[str(call_id)] = name
        return Part.from_function_call(
            name=name, args=_parse_json_arguments(part.get("arguments")) or {}
        )
    if part_type == "tool_call_response":
        # Prefer an explicit name; else recover it from the matching
        # tool_call's id. The payload lives under ``response`` (OTEL) or
        # ``result``.
        name = part.get("name") or ""
        call_id = part.get("id")
        if not name and call_id is not None:
            name = tool_call_ids.get(str(call_id), "")
        payload = part.get("response")
        if payload is None:
            payload = part.get("result")
        return Part.from_function_response(
            name=name, response=_value_to_response_dict(payload)
        )
    if part_type in ("uri", "file_data"):
        return _parse_uri_part(part.get("uri"), part.get("mime_type"))
    if part_type == "blob":
        # The instrumentor stores the bytes under ``content``, ADK under
        # ``data``.
        data = part.get("content")
        if data is None:
            data = part.get("data")
        return _build_inline_data_placeholder(data, part.get("mime_type"))
    if part_type == "file":
        mime_type = part.get("mime_type") or _DEFAULT_MIME_TYPE
        file_id = part.get("file_id")
        if file_id is None:
            return build_attachment_placeholder(mime_type)
        return Part(text=f"<attachment: {mime_type}, file_id={file_id}>")
    if part_type == "reasoning" and part.get("content") is not None:
        return Part(text=str(part["content"]), thought=True)
    if part_type in (_SERVER_TOOL_CALL, _SERVER_TOOL_RESPONSE):
        return _parse_server_tool_part(part, part_type)
    return None


def _parse_uri_part(uri: Any, mime_type: Any) -> Part:
    """Convert a referenced attachment into a ``file_data`` `Part`.

    Args:
        uri: The attachment URI, such as ``gs://...`` or a ``data:`` URI.
        mime_type: The attachment's MIME type, if known.

    Returns:
        A native ``file_data`` Part, an inline-data placeholder for a
        ``data:`` URI, or a bare attachment placeholder when the URI is
        missing.
    """
    if not isinstance(uri, str) or not uri:
        return build_attachment_placeholder(mime_type or _DEFAULT_MIME_TYPE)
    if _is_data_uri(uri):
        return _data_uri_to_placeholder(uri, mime_type)
    return Part(file_data=FileData(file_uri=uri, mime_type=mime_type or None))


def _parse_server_tool_part(part: Mapping[str, Any], payload_key: str) -> Part:
    """Convert an OTEL server-tool call or response into a genai `Part`.

    Args:
        part: Raw part mapping.
        payload_key: The key holding the tool payload: ``server_tool_call`` or
            ``server_tool_call_response``.

    Returns:
        A native ``executable_code``, ``code_execution_result``,
        ``tool_call`` or ``tool_response`` Part, or the part as JSON text when
        no native field can carry it without loss.
    """
    is_response = payload_key == _SERVER_TOOL_RESPONSE
    inner = part.get(payload_key)
    if inner is None:
        inner = {}
    elif not isinstance(inner, Mapping):
        return _part_to_json_text(part)
    # The instrumentor derives a call's top-level name from its tool type, but
    # writes no top-level name on a response.
    names = [inner.get("type"), part.get("name")]
    if not is_response:
        names.reverse()
    name = next((n for n in names if isinstance(n, str) and n), "")
    is_code_execution = _CODE_EXECUTION in names
    if is_code_execution:
        fields = _CODE_RESULT_FIELDS if is_response else _CODE_FIELDS
    else:
        fields = _TOOL_RESPONSE_FIELDS if is_response else _TOOL_CALL_FIELDS
    has_extra_fields = any(
        value is not None for key, value in inner.items() if key not in fields
    )
    if has_extra_fields or not (
        is_code_execution or _is_native_tool_name(name)
    ):
        return _part_to_json_text(part)

    part_id = part.get("id")
    part_id = None if part_id is None else str(part_id)
    try:
        if is_code_execution and is_response:
            return Part(
                code_execution_result=CodeExecutionResult(
                    outcome=inner.get("outcome"),
                    output=inner.get("output"),
                    id=part_id,
                )
            )
        if is_code_execution:
            return Part(
                executable_code=ExecutableCode(
                    code=inner.get("code"),
                    language=inner.get("language"),
                    id=part_id,
                )
            )
        if is_response:
            return Part(
                tool_response=ToolResponse(
                    id=part_id,
                    tool_type=_resolve_tool_type(name),
                    response=_value_to_response_dict(inner.get("response")),
                )
            )
        return Part(
            tool_call=ToolCall(
                id=part_id,
                tool_type=_resolve_tool_type(name),
                args=_parse_json_arguments(inner.get("arguments")),
            )
        )
    except ValidationError:
        logger.warning("Failed to parse server tool part; keeping it as text.")
        return _part_to_json_text(part)


def _is_native_tool_name(name: str) -> bool:
    """Whether a `ToolCall` / `ToolResponse` can represent a server tool name.

    Those types record only a `ToolType`, so any other name would be lost.

    Args:
        name: The server tool name reported by the instrumentor.

    Returns:
        True if the name is empty, ``unknown``, or a `ToolType` value.
    """
    return name in ("", "unknown") or name.upper() in _TOOL_TYPE_VALUES


def _resolve_tool_type(name: str) -> ToolType | None:
    """Map a server tool name to its `ToolType`.

    Args:
        name: The server tool name, e.g. ``google_search_web``.

    Returns:
        The matching ToolType, or None if the name is not one.
    """
    value = name.upper()
    return ToolType(value) if value in _TOOL_TYPE_VALUES else None


def _part_to_json_text(part: Mapping[str, Any]) -> Part:
    """Keep a part that has no lossless native form as JSON text.

    Args:
        part: Raw part mapping.

    Returns:
        A text Part holding the part serialized as JSON.
    """
    return Part(text=json.dumps(dict(part), default=str))


def _build_native_binary_placeholder(fields: Mapping[str, Any]) -> Part | None:
    """Replace a native part's embedded bytes with a placeholder.

    Args:
        fields: The part's non-null `Part` fields, in either spelling.

    Returns:
        An inline-data placeholder for ``inline_data`` or a ``data:`` file
        URI, or None if the part embeds no bytes.
    """
    inline = extract_field(fields, "inline_data", "inlineData")
    if isinstance(inline, Mapping):
        return _build_inline_data_placeholder(
            inline.get("data"), extract_field(inline, "mime_type", "mimeType")
        )
    file_data = extract_field(fields, "file_data", "fileData")
    if isinstance(file_data, Mapping):
        uri = extract_field(file_data, "file_uri", "fileUri")
        if isinstance(uri, str) and _is_data_uri(uri):
            return _data_uri_to_placeholder(
                uri, extract_field(file_data, "mime_type", "mimeType")
            )
    return None


def extract_field(mapping: Mapping[str, Any], *keys: str) -> Any:
    """Return the first non-null value among alternate spellings of a key.

    Args:
        mapping: Mapping to read.
        *keys: Candidate keys, in priority order.

    Returns:
        The first non-null value, or None.
    """
    for key in keys:
        value = mapping.get(key)
        if value is not None:
            return value
    return None


def _is_data_uri(uri: str) -> bool:
    """Whether a URI embeds its payload as an RFC 2397 ``data:`` URI.

    Args:
        uri: URI to inspect.

    Returns:
        True if the URI uses the ``data:`` scheme.
    """
    return uri[: len(_DATA_URI_PREFIX)].lower() == _DATA_URI_PREFIX


def _data_uri_to_placeholder(uri: str, mime_type: Any) -> Part:
    """Replace a ``data:[<mediatype>][;base64],<payload>`` URI with a placeholder.

    Args:
        uri: The ``data:`` URI.
        mime_type: The part's MIME type, used when the URI names none.

    Returns:
        An inline-data placeholder Part.
    """
    header, separator, payload = uri[len(_DATA_URI_PREFIX) :].partition(",")
    params = header.split(";")
    data_mime_type = params[0] or mime_type
    if not separator:
        return _build_inline_data_placeholder(None, data_mime_type)
    if params[-1].lower() == "base64":
        data = _decode_inline_data(payload)
    else:
        data = urllib.parse.unquote_to_bytes(payload)
    return _build_inline_data_placeholder(data, data_mime_type)


def _build_inline_data_placeholder(data: Any, mime_type: Any) -> Part:
    """Describe inline bytes as text, keeping the payload out of the eval case.

    The digest keeps two different attachments of the same type and size
    apart, because `trace_converter._add_contents` deduplicates messages by
    their JSON. Data that cannot be decoded, such as every blob ADK writes to
    ``gen_ai.*`` message attributes, has no digest, so such attachments are
    told apart only by their message.

    Args:
        data: The bytes, or their base64 text.
        mime_type: The data's MIME type, if known.

    Returns:
        A text Part naming the MIME type, size and a short SHA-256 digest, or
        noting an unknown size when the data cannot be decoded.
    """
    mime_type = mime_type or _DEFAULT_MIME_TYPE
    decoded = _decode_inline_data(data)
    if decoded is None:
        return Part(text=f"<attachment: {mime_type}, size unknown>")
    digest = hashlib.sha256(decoded).hexdigest()[:8]
    return Part(
        text=f"<attachment: {mime_type}, {len(decoded)} bytes, sha256:{digest}>"
    )


def build_attachment_placeholder(mime_type: str) -> Part:
    """Build the placeholder for an attachment known only by its MIME type.

    Args:
        mime_type: The attachment's MIME type.

    Returns:
        A text Part naming only the MIME type.
    """
    return Part(text=f"<attachment: {mime_type}>")


def is_attachment_placeholder(text: str) -> bool:
    """Whether text is an attachment placeholder this module emits.

    Args:
        text: Part text to inspect.

    Returns:
        True if the text is exactly a placeholder for inline data, an uploaded
        file's ID, or a bare MIME type.
    """
    return bool(_ATTACHMENT_PLACEHOLDER.fullmatch(text))


def parse_inline_data_placeholder(part: Part) -> InlineDataPlaceholder | None:
    """Parse a placeholder from `_build_inline_data_placeholder`.

    Args:
        part: Parsed part.

    Returns:
        The placeholder's MIME type and whether it carries a digest, or None
        if the part's text is not exactly an inline-data placeholder.
    """
    if part.text is None:
        return None
    match = _INLINE_DATA_PLACEHOLDER.fullmatch(part.text)
    if match is None:
        return None
    return InlineDataPlaceholder(
        mime_type=match["mime_type"], has_digest=match["digest"] is not None
    )


def _decode_inline_data(data: Any) -> bytes | None:
    """Decode inline data that may arrive as bytes or base64 text.

    Tolerates missing ``=`` padding and the URL-safe alphabet.

    Args:
        data: Raw bytes, or standard or URL-safe base64 text.

    Returns:
        The decoded bytes, or None if the data is missing or not base64,
        such as ADK's ``"<not serializable>"`` sentinel.
    """
    if isinstance(data, bytes | bytearray):
        return bytes(data)
    if not isinstance(data, str):
        return None
    text = data + "=" * (-len(data) % 4)
    altchars = b"-_" if "-" in text or "_" in text else None
    try:
        return base64.b64decode(text, altchars=altchars, validate=True)
    except (binascii.Error, ValueError):
        return None


def _has_part_data(part: Part) -> bool:
    """Whether a parsed `Part` carries any populated data field.

    Args:
        part: SDK Part to inspect.

    Returns:
        True if any field other than thought_signature is populated.
    """
    return any(
        getattr(part, field) is not None
        for field in _PART_FIELDS
        if field != "thought_signature"
    )


def _parse_json_arguments(arguments: Any) -> Any:
    """Decode tool-call arguments that arrive as a JSON object string.

    Args:
        arguments: Raw ``arguments`` value of a ``tool_call`` part.

    Returns:
        The decoded dict when ``arguments`` is a string holding a JSON object;
        otherwise ``arguments`` unchanged.
    """
    if not isinstance(arguments, str):
        return arguments
    try:
        decoded = json.loads(arguments)
    except json.JSONDecodeError:
        return arguments
    return decoded if isinstance(decoded, dict) else arguments


def _value_to_response_dict(value: Any) -> dict[str, Any]:
    """Coerce a function-response value into a dict payload.

    Args:
        value: Raw response payload.

    Returns:
        Dictionary suitable for a function response.
    """
    if isinstance(value, Mapping):
        return dict(value)
    if value is None:
        return {}
    return {"output": value}
