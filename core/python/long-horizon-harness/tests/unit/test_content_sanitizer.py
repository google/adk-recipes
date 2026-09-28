# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""sanitize_content_for_memory strips inline_data parts whose mime the Memory
Bank extractor can't ingest, so the nightly dream pass can't POST an
unsupported mimeType (e.g. application/json) into memories.generate."""

from __future__ import annotations

from google.genai.types import Blob, Content, Part


def _json_part() -> Part:
    return Part(inline_data=Blob(mime_type="application/json", data=b'{"a":1}'))


def test_json_inline_data_becomes_text_placeholder():
    from app.memory.content_sanitizer import sanitize_content_for_memory

    content = Content(
        role="user", parts=[Part(text="see attached"), _json_part()]
    )
    out = sanitize_content_for_memory(content)

    assert out.parts[0].text == "see attached"
    assert out.parts[1].inline_data is None
    assert out.parts[1].text is not None
    assert "application/json" in out.parts[1].text


def test_text_parts_pass_through_unchanged():
    from app.memory.content_sanitizer import sanitize_content_for_memory

    content = Content(role="user", parts=[Part(text="hello")])
    out = sanitize_content_for_memory(content)

    assert out.parts[0].text == "hello"
    assert out.parts[0].inline_data is None


def test_image_inline_data_is_preserved():
    from app.memory.content_sanitizer import sanitize_content_for_memory

    img = Part(inline_data=Blob(mime_type="image/png", data=b"\x89PNG"))
    content = Content(role="user", parts=[img])
    out = sanitize_content_for_memory(content)

    assert out.parts[0].inline_data is not None
    assert out.parts[0].inline_data.mime_type == "image/png"


def test_pdf_inline_data_is_preserved():
    from app.memory.content_sanitizer import sanitize_content_for_memory

    pdf = Part(inline_data=Blob(mime_type="application/pdf", data=b"%PDF-"))
    content = Content(role="user", parts=[pdf])
    out = sanitize_content_for_memory(content)

    assert out.parts[0].inline_data is not None
    assert out.parts[0].inline_data.mime_type == "application/pdf"


def test_csv_and_octet_stream_are_degraded():
    from app.memory.content_sanitizer import sanitize_content_for_memory

    parts = [
        Part(inline_data=Blob(mime_type="text/csv", data=b"a,b")),
        Part(
            inline_data=Blob(mime_type="application/octet-stream", data=b"\x00")
        ),
    ]
    out = sanitize_content_for_memory(Content(role="user", parts=parts))

    assert all(p.inline_data is None for p in out.parts)
    assert all(p.text for p in out.parts)


def test_none_content_returns_none():
    from app.memory.content_sanitizer import sanitize_content_for_memory

    assert sanitize_content_for_memory(None) is None


def test_content_without_parts_is_returned_as_is():
    from app.memory.content_sanitizer import sanitize_content_for_memory

    content = Content(role="user", parts=None)
    out = sanitize_content_for_memory(content)
    assert out is content


def test_function_response_part_passes_through():
    from google.genai.types import FunctionResponse

    from app.memory.content_sanitizer import sanitize_content_for_memory

    fr = Part(
        function_response=FunctionResponse(name="t", response={"ok": True})
    )
    out = sanitize_content_for_memory(Content(role="user", parts=[fr]))
    assert out.parts[0].function_response is not None
