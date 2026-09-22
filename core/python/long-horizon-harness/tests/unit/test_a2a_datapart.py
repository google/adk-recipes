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

"""Unit tests for ``horizon.a2a.datapart.unwrap_a2a_datapart_text``.

ADK wraps untyped A2A ``DataPart`` payloads (no ADK metadata-type key) in a
``<a2a_datapart_json>...</a2a_datapart_json>`` ``text/plain`` inline_data blob.
Such blobs are opaque to the model, so we unwrap them back to readable
text before they reach it.
"""

from __future__ import annotations

import json

from horizon.a2a.datapart import (
    A2A_DATA_PART_END_TAG,
    A2A_DATA_PART_START_TAG,
    unwrap_a2a_datapart_text,
)


def _wrap(json_body: bytes) -> bytes:
    return A2A_DATA_PART_START_TAG + json_body + A2A_DATA_PART_END_TAG


def test_unwraps_ui_action_to_readable_text() -> None:
    body = (
        b'{"data":{"version":"v0.9","action":{"name":"gws_write_decision",'
        b'"surfaceId":"surface-1"}},"kind":"data"}'
    )
    text = unwrap_a2a_datapart_text(_wrap(body))
    assert text is not None
    assert "gws_write_decision" in text
    assert "surface-1" in text
    assert "a2a_datapart_json" not in text


def test_returns_none_for_non_datapart_bytes() -> None:
    assert unwrap_a2a_datapart_text(b"plain bytes, no tag") is None


def test_returns_none_for_empty() -> None:
    assert unwrap_a2a_datapart_text(None) is None
    assert unwrap_a2a_datapart_text(b"") is None


def test_tolerates_malformed_json_inside_tags() -> None:
    # Garbage between the tags must not raise — degrade to None so callers
    # fall back to their generic handling.
    assert unwrap_a2a_datapart_text(_wrap(b"{not json")) is None


def _wrap_payload(payload: dict) -> bytes:
    return (
        A2A_DATA_PART_START_TAG
        + json.dumps({"data": payload}).encode("utf-8")
        + A2A_DATA_PART_END_TAG
    )


class TestSelectionPayload:
    """A highlighted selection travels as a DataPart so the chat bubble can
    draw a quote card; the model still needs prose, built here."""

    def test_renders_prose_with_a_line_range(self) -> None:
        text = unwrap_a2a_datapart_text(
            _wrap_payload(
                {
                    "lha_kind": "lha.selection",
                    "path": "/workspace/cats.md",
                    "start": 40,
                    "end": 51,
                    "start_line": 6,
                    "end_line": 8,
                    "snippet": "# dogs\nwoof",
                }
            )
        )
        assert text is not None
        assert "/workspace/cats.md, lines 6-8" in text
        assert "# dogs\nwoof" in text
        # edit has no start/end targeting: tell the model to search with
        # enough surrounding context to match only this location.
        assert "edit" in text
        assert "oldText" in text and "newText" in text
        assert "start=40" not in text and "end=51" not in text
        # The instruction is the user's own message, not this block.
        assert "replace" not in text.lower()
        # No JSON leaks through to the model.
        assert "lha_kind" not in text
        assert "[UI action]" not in text

    def test_single_line_reads_as_line_not_range(self) -> None:
        text = unwrap_a2a_datapart_text(
            _wrap_payload(
                {
                    "lha_kind": "lha.selection",
                    "path": "/workspace/cats.md",
                    "start": 40,
                    "end": 46,
                    "start_line": 6,
                    "end_line": 6,
                    "snippet": "# dogs",
                }
            )
        )
        assert text is not None
        assert "line 6" in text
        assert "lines 6" not in text

    def test_malformed_selection_falls_back_to_the_generic_dump(self) -> None:
        # Better an opaque payload than a confidently wrong anchor.
        text = unwrap_a2a_datapart_text(
            _wrap_payload(
                {"lha_kind": "lha.selection", "path": "/workspace/cats.md"}
            )
        )
        assert text is not None
        assert text.startswith("[UI action] ")

    def test_other_payloads_are_untouched(self) -> None:
        text = unwrap_a2a_datapart_text(_wrap_payload({"foo": "bar"}))
        assert text == '[UI action] {"foo": "bar"}'


class TestFileRefPayload:
    """Files dragged from the tree travel as paths, not as bytes."""

    def test_lists_the_attached_paths(self) -> None:
        text = unwrap_a2a_datapart_text(
            _wrap_payload(
                {
                    "lha_kind": "lha.fileref",
                    "paths": ["alphabet.md", "docs/plan.md"],
                }
            )
        )
        assert text is not None
        assert "alphabet.md" in text and "docs/plan.md" in text
        assert "2 workspace files" in text
        assert "[UI action]" not in text

    def test_reads_naturally_for_one_file(self) -> None:
        text = unwrap_a2a_datapart_text(
            _wrap_payload({"lha_kind": "lha.fileref", "paths": ["a.md"]})
        )
        assert text is not None
        assert "1 workspace file." in text or "1 workspace file " in text

    def test_malformed_falls_back_to_the_generic_dump(self) -> None:
        for payload in (
            {"lha_kind": "lha.fileref"},
            {"lha_kind": "lha.fileref", "paths": []},
            {"lha_kind": "lha.fileref", "paths": ["ok.md", 42]},
        ):
            text = unwrap_a2a_datapart_text(_wrap_payload(payload))
            assert text is not None
            assert text.startswith("[UI action] ")
