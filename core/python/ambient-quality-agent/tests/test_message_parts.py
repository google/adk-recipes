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

"""Tests for `tools/ingestion/message_parts.py`."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

import pytest
from ambient_quality_agent.tools.ingestion import message_parts
from google.genai.types import Language, Outcome, Part, ToolType

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + bytes(range(40))
_PNG_B64 = base64.b64encode(_PNG_BYTES).decode()


def _build_placeholder(data: bytes, mime_type: str = "image/png") -> str:
    """Builds the attachment placeholder `parse_part` emits for inline bytes.

    Args:
        data: The attachment bytes.
        mime_type: The attachment MIME type.

    Returns:
        The expected placeholder text.
    """
    digest = hashlib.sha256(data).hexdigest()[:8]
    return f"<attachment: {mime_type}, {len(data)} bytes, sha256:{digest}>"


def _parse(part: dict[str, Any]) -> Part:
    """Parses one message part, asserting it is kept.

    Args:
        part: Raw message part.

    Returns:
        The parsed Part.
    """
    parsed = message_parts.parse_part(part, {})
    assert parsed is not None
    return parsed


@pytest.mark.parametrize(
    "part",
    [
        pytest.param(
            {
                "type": "uri",
                "uri": "gs://bucket/doc.pdf",
                "mime_type": "application/pdf",
                "modality": "application",
            },
            id="instrumentor-uri",
        ),
        pytest.param(
            {
                "type": "file_data",
                "uri": "gs://bucket/doc.pdf",
                "mime_type": "application/pdf",
            },
            id="adk-typed-file-data",
        ),
        pytest.param(
            {
                "file_data": {
                    "file_uri": "gs://bucket/doc.pdf",
                    "mime_type": "application/pdf",
                }
            },
            id="native-file-data",
        ),
        pytest.param(
            {
                "fileData": {
                    "fileUri": "gs://bucket/doc.pdf",
                    "mimeType": "application/pdf",
                }
            },
            id="camel-case-file-data",
        ),
    ],
)
def test_parse_part_keeps_referenced_files_as_native_file_data(
    part: dict[str, Any],
) -> None:
    parsed = _parse(part)

    assert parsed.file_data is not None
    assert parsed.file_data.file_uri == "gs://bucket/doc.pdf"
    assert parsed.file_data.mime_type == "application/pdf"


def test_parse_part_describes_a_uri_part_without_a_uri() -> None:
    parsed = _parse({"type": "uri", "mime_type": "application/pdf"})

    assert parsed.text == "<attachment: application/pdf>"


@pytest.mark.parametrize(
    ("part", "expected"),
    [
        pytest.param(
            {
                "type": "uri",
                "uri": f"data:image/png;base64,{_PNG_B64}",
                "mime_type": "image/jpeg",
            },
            _build_placeholder(_PNG_BYTES),
            id="base64-header-mime-wins",
        ),
        pytest.param(
            {
                "type": "uri",
                "uri": "data:,Hello%2C%20World",
                "mime_type": "text/plain",
            },
            _build_placeholder(b"Hello, World", "text/plain"),
            id="percent-encoded-part-mime",
        ),
        pytest.param(
            {
                "file_data": {
                    "file_uri": f"data:image/png;base64,{_PNG_B64}",
                    "mime_type": "image/png",
                }
            },
            _build_placeholder(_PNG_BYTES),
            id="native-file-data",
        ),
    ],
)
def test_parse_part_replaces_data_uris_with_a_placeholder(
    part: dict[str, Any], expected: str
) -> None:
    parsed = _parse(part)

    assert parsed.text == expected
    assert parsed.file_data is None
    assert _PNG_B64 not in parsed.model_dump_json()


@pytest.mark.parametrize(
    "part",
    [
        pytest.param(
            {
                "type": "blob",
                "content": _PNG_B64,
                "mime_type": "image/png",
                "modality": "image",
            },
            id="instrumentor-content",
        ),
        pytest.param(
            {"type": "blob", "data": _PNG_B64, "mime_type": "image/png"},
            id="adk-data",
        ),
        pytest.param(
            {
                "type": "blob",
                "data": base64.urlsafe_b64encode(_PNG_BYTES)
                .decode()
                .rstrip("="),
                "mime_type": "image/png",
            },
            id="url-safe-unpadded",
        ),
        pytest.param(
            {"inline_data": {"data": _PNG_B64, "mime_type": "image/png"}},
            id="native-inline-data",
        ),
        pytest.param(
            {"inlineData": {"data": _PNG_B64, "mimeType": "image/png"}},
            id="camel-case-inline-data",
        ),
    ],
)
def test_parse_part_replaces_inline_bytes_with_a_placeholder(
    part: dict[str, Any],
) -> None:
    parsed = _parse(part)

    assert parsed.text == _build_placeholder(_PNG_BYTES)
    assert parsed.inline_data is None
    dumped = parsed.model_dump_json()
    assert _PNG_B64 not in dumped
    assert _PNG_B64.rstrip("=")[:16] not in dumped


@pytest.mark.parametrize(
    ("part", "expected"),
    [
        pytest.param(
            {
                "type": "blob",
                "data": "<not serializable>",
                "mime_type": "image/png",
            },
            "<attachment: image/png, size unknown>",
            id="adk-sentinel",
        ),
        pytest.param(
            {
                "inline_data": {
                    "data": "<not serializable>",
                    "mime_type": "image/png",
                }
            },
            "<attachment: image/png, size unknown>",
            id="native-sentinel",
        ),
        pytest.param(
            {"type": "blob"},
            "<attachment: application/octet-stream, size unknown>",
            id="no-data-no-mime",
        ),
    ],
)
def test_parse_part_notes_undecodable_inline_data_as_size_unknown(
    part: dict[str, Any], expected: str
) -> None:
    assert _parse(part).text == expected


def test_parse_part_describes_an_uploaded_file_by_its_id() -> None:
    parsed = _parse(
        {
            "type": "file",
            "file_id": "files/abc123",
            "mime_type": "application/pdf",
            "modality": "application",
        }
    )

    assert parsed.text == "<attachment: application/pdf, file_id=files/abc123>"


def test_parse_part_marks_reasoning_as_thought() -> None:
    parsed = _parse({"type": "reasoning", "content": "Let me think."})

    assert parsed.text == "Let me think."
    assert parsed.thought is True


def test_parse_part_accepts_camel_case_function_calls() -> None:
    parsed = _parse(
        {"functionCall": {"name": "find_slot", "args": {"size": "19"}}}
    )

    assert parsed.function_call is not None
    assert parsed.function_call.name == "find_slot"
    assert parsed.function_call.args == {"size": "19"}


def test_parse_part_maps_a_search_tool_call_and_response_natively() -> None:
    call = _parse(
        {
            "type": "server_tool_call",
            "id": "call-1",
            "name": "google_search_web",
            "server_tool_call": {
                "type": "google_search_web",
                "arguments": {"queries": ["weather"]},
            },
        }
    )
    response = _parse(
        {
            "type": "server_tool_call_response",
            "id": "call-1",
            "server_tool_call_response": {
                "type": "google_search_web",
                "response": {"results": ["sunny"]},
            },
        }
    )

    assert call.tool_call is not None
    assert call.tool_call.id == "call-1"
    assert call.tool_call.tool_type == ToolType.GOOGLE_SEARCH_WEB
    assert call.tool_call.args == {"queries": ["weather"]}
    assert response.tool_response is not None
    assert response.tool_response.id == "call-1"
    assert response.tool_response.tool_type == ToolType.GOOGLE_SEARCH_WEB
    assert response.tool_response.response == {"results": ["sunny"]}


def test_parse_part_maps_an_unknown_server_tool_natively_without_a_type() -> (
    None
):
    parsed = _parse(
        {
            "type": "server_tool_call",
            "id": None,
            "name": "unknown",
            "server_tool_call": {"type": "unknown", "arguments": {"q": 1}},
        }
    )

    assert parsed.tool_call is not None
    assert parsed.tool_call.tool_type is None
    assert parsed.tool_call.args == {"q": 1}


def test_parse_part_maps_code_execution_natively() -> None:
    code = _parse(
        {
            "type": "server_tool_call",
            "id": "exec-1",
            "name": "code_execution",
            "server_tool_call": {
                "type": "code_execution",
                "code": "print(1)",
                "language": "PYTHON",
            },
        }
    )
    result = _parse(
        {
            "type": "server_tool_call_response",
            "id": "exec-1",
            "server_tool_call_response": {
                "type": "code_execution",
                "outcome": "OUTCOME_OK",
                "output": "1\n",
            },
        }
    )

    assert code.executable_code is not None
    assert code.executable_code.code == "print(1)"
    assert code.executable_code.language == Language.PYTHON
    assert code.executable_code.id == "exec-1"
    assert result.code_execution_result is not None
    assert result.code_execution_result.outcome == Outcome.OUTCOME_OK
    assert result.code_execution_result.output == "1\n"
    assert result.code_execution_result.id == "exec-1"


@pytest.mark.parametrize(
    "part",
    [
        pytest.param(
            {
                "type": "server_tool_call",
                "id": "ws-1",
                "name": "web_search",
                "server_tool_call": {
                    "type": "web_search",
                    "arguments": {"query": "weather"},
                },
            },
            id="call-name-not-a-tool-type",
        ),
        pytest.param(
            {
                "type": "server_tool_call_response",
                "id": "ws-1",
                "server_tool_call_response": {
                    "type": "web_search",
                    "response": {"results": []},
                },
            },
            id="response-name-not-a-tool-type",
        ),
    ],
)
def test_parse_part_keeps_a_server_tool_without_a_tool_type_as_json_text(
    part: dict[str, Any],
) -> None:
    parsed = _parse(part)

    assert parsed.tool_call is None
    assert parsed.tool_response is None
    assert parsed.text is not None
    assert json.loads(parsed.text) == part


@pytest.mark.parametrize(
    "part",
    [
        pytest.param(
            {
                "type": "server_tool_call",
                "name": "google_search_web",
                "server_tool_call": {
                    "type": "google_search_web",
                    "arguments": "not a dict",
                },
            },
            id="arguments-not-a-dict",
        ),
        pytest.param(
            {
                "type": "server_tool_call",
                "name": "code_execution",
                "server_tool_call": {
                    "type": "code_execution",
                    "code": {"source": "print(1)"},
                    "language": "PYTHON",
                },
            },
            id="code-not-a-string",
        ),
    ],
)
def test_parse_part_keeps_an_invalid_server_tool_payload_as_json_text(
    part: dict[str, Any],
) -> None:
    parsed = _parse(part)

    assert parsed.text is not None
    assert json.loads(parsed.text) == part


def test_parse_part_describes_an_uploaded_file_without_an_id() -> None:
    parsed = _parse({"type": "file", "mime_type": "application/pdf"})

    assert parsed.text == "<attachment: application/pdf>"


@pytest.mark.parametrize(
    "uri",
    [
        pytest.param("data:image/png;base64", id="no-comma"),
        pytest.param("data:image/png;base64,not*base64!", id="bad-base64"),
    ],
)
def test_parse_part_notes_an_undecodable_data_uri_as_size_unknown(
    uri: str,
) -> None:
    parsed = _parse({"type": "uri", "uri": uri})

    assert parsed.text == "<attachment: image/png, size unknown>"


@pytest.mark.parametrize(
    "part",
    [
        pytest.param(
            {
                "type": "server_tool_call",
                "id": "c1",
                "name": "code_execution",
                "server_tool_call": {
                    "type": "code_execution",
                    "arguments": {"code": "print(1)", "language": "python"},
                },
            },
            id="code-in-arguments",
        ),
        pytest.param(
            {
                "type": "server_tool_call_response",
                "id": "c1",
                "server_tool_call_response": {
                    "type": "code_execution",
                    "result": "1\n",
                    "is_error": False,
                },
            },
            id="code-result-field",
        ),
        pytest.param(
            {
                "type": "server_tool_call_response",
                "id": "u1",
                "server_tool_call_response": {
                    "type": "url_context",
                    "result": [{"url": "https://example.com"}],
                },
            },
            id="url-context-result-field",
        ),
    ],
)
def test_parse_part_keeps_server_tool_fields_without_a_native_slot_as_json(
    part: dict[str, Any],
) -> None:
    parsed = _parse(part)

    assert parsed.text is not None
    assert json.loads(parsed.text) == part


def test_parse_part_ignores_a_non_string_server_tool_name() -> None:
    parsed = _parse(
        {
            "type": "server_tool_call",
            "name": 123,
            "server_tool_call": {"type": "google_search_web", "arguments": {}},
        }
    )

    assert parsed.tool_call is not None
    assert parsed.tool_call.tool_type == ToolType.GOOGLE_SEARCH_WEB


def test_parse_part_keeps_an_invalid_typed_part_as_json_text() -> None:
    part = {"type": "uri", "uri": "gs://bucket/doc.pdf", "mime_type": 7}

    parsed = _parse(part)

    assert parsed.text is not None
    assert json.loads(parsed.text) == part


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        pytest.param('{"size": "19"}', {"size": "19"}, id="json-object-string"),
        pytest.param({"size": "19"}, {"size": "19"}, id="mapping"),
        pytest.param(None, {}, id="missing"),
    ],
)
def test_parse_part_decodes_tool_call_arguments(
    arguments: Any, expected: dict[str, Any]
) -> None:
    parsed = _parse(
        {
            "type": "tool_call",
            "id": "c1",
            "name": "find_slot",
            "arguments": arguments,
        }
    )

    assert parsed.function_call is not None
    assert parsed.function_call.name == "find_slot"
    assert parsed.function_call.args == expected


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param("not json", id="not-json"),
        pytest.param("[1, 2]", id="json-array"),
    ],
)
def test_parse_part_keeps_a_tool_call_with_non_object_arguments_as_json_text(
    arguments: str,
) -> None:
    part = {"type": "tool_call", "name": "find_slot", "arguments": arguments}

    parsed = _parse(part)

    assert parsed.function_call is None
    assert parsed.text is not None
    assert json.loads(parsed.text) == part


@pytest.mark.parametrize(
    "part",
    [
        pytest.param(
            {
                "type": "server_tool_call",
                "name": "google_search_web",
                "server_tool_call": '{"type": "google_search_web"}',
            },
            id="call-payload-string",
        ),
        pytest.param(
            {
                "type": "server_tool_call_response",
                "server_tool_call_response": '{"type": "google_search_web"}',
            },
            id="response-payload-string",
        ),
    ],
)
def test_parse_part_keeps_a_non_mapping_server_tool_payload_as_json_text(
    part: dict[str, Any],
) -> None:
    parsed = _parse(part)

    assert parsed.tool_call is None
    assert parsed.tool_response is None
    assert parsed.text is not None
    assert json.loads(parsed.text) == part


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(_build_placeholder(_PNG_BYTES), id="digest"),
        pytest.param(
            "<attachment: image/png, size unknown>", id="size-unknown"
        ),
        pytest.param(
            "<attachment: application/pdf, file_id=files/abc123>", id="file-id"
        ),
        pytest.param("<attachment: application/pdf>", id="bare"),
    ],
)
def test_is_attachment_placeholder_recognizes_every_emitted_form(
    text: str,
) -> None:
    assert message_parts.is_attachment_placeholder(text)


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("see <attachment: image/png>", id="not-at-start"),
        pytest.param("<attachment: image/png> above", id="not-at-end"),
        pytest.param("<attachments>", id="other-tag"),
        pytest.param("<attachment: >", id="no-mime-type"),
        pytest.param("<attachment: image/png, 12 bytes>", id="unknown-detail"),
    ],
)
def test_is_attachment_placeholder_rejects_other_text(text: str) -> None:
    assert not message_parts.is_attachment_placeholder(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(
            _build_placeholder(_PNG_BYTES),
            message_parts.InlineDataPlaceholder("image/png", has_digest=True),
            id="digest",
        ),
        pytest.param(
            "<attachment: image/jpeg, size unknown>",
            message_parts.InlineDataPlaceholder("image/jpeg", has_digest=False),
            id="size-unknown",
        ),
        pytest.param("<attachment: application/pdf>", None, id="bare"),
        pytest.param(
            "<attachment: application/pdf, file_id=files/abc123>",
            None,
            id="file-id",
        ),
    ],
)
def test_parse_inline_data_placeholder_reads_only_inline_data_placeholders(
    text: str, expected: message_parts.InlineDataPlaceholder | None
) -> None:
    assert (
        message_parts.parse_inline_data_placeholder(Part(text=text)) == expected
    )


def test_parse_part_decodes_server_tool_call_arguments_from_json_text() -> None:
    parsed = _parse(
        {
            "type": "server_tool_call",
            "name": "google_search_web",
            "server_tool_call": {
                "type": "google_search_web",
                "arguments": '{"queries": ["weather"]}',
            },
        }
    )

    assert parsed.tool_call is not None
    assert parsed.tool_call.args == {"queries": ["weather"]}
