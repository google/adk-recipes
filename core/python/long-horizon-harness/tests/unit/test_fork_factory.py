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

"""Tests for the shared fork helpers at ``app/memory/_fork.py``.

Runner construction, task lifecycle, and ``run_fork`` drive-loop tests
moved to :mod:`tests.unit.test_sibling_agent_plugin` when those concerns
migrated into ``SiblingAgentPlugin``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.memory import _fork


def _make_event(text: str, author: str = "user") -> SimpleNamespace:
    return SimpleNamespace(
        author=author,
        content=SimpleNamespace(parts=[SimpleNamespace(text=text)]),
    )


# =============================================================================
# event_text + format_conversation_snapshot
# =============================================================================


def test_event_text_concatenates_parts():
    event = SimpleNamespace(
        author="user",
        content=SimpleNamespace(
            parts=[
                SimpleNamespace(text="hello "),
                SimpleNamespace(text="world"),
                SimpleNamespace(text=None),
            ]
        ),
    )
    assert _fork.event_text(event) == "hello world"


def test_event_text_handles_missing_content():
    assert _fork.event_text(SimpleNamespace(content=None)) == ""


def test_format_conversation_snapshot_default_tag():
    snapshot = _fork.format_conversation_snapshot(
        [_make_event("hi", "user"), _make_event("hello", "agent")]
    )
    assert snapshot.startswith("<CONVERSATION>")
    assert snapshot.endswith("</CONVERSATION>")
    assert "[user]\nhi" in snapshot
    assert "[agent]\nhello" in snapshot


def test_format_conversation_snapshot_custom_tag_and_empty():
    snapshot = _fork.format_conversation_snapshot([], tag="DUMP")
    assert snapshot == "<DUMP>\n(no conversation)\n</DUMP>"


# =============================================================================
# make_whitelist_callback
# =============================================================================


@pytest.mark.asyncio
async def test_whitelist_allows_known_tool():
    callback = _fork.make_whitelist_callback(
        frozenset({"add_memory"}), fork_name="flush"
    )
    result = await callback(
        tool=SimpleNamespace(name="add_memory"),
        args={},
        tool_context=None,
    )
    assert result is None


@pytest.mark.asyncio
async def test_whitelist_blocks_unknown_tool():
    callback = _fork.make_whitelist_callback(
        frozenset({"add_memory"}), fork_name="flush"
    )
    result = await callback(
        tool=SimpleNamespace(name="terminal"),
        args={},
        tool_context=None,
    )
    assert result is not None
    assert "not available in the flush fork" in result["error"]
    assert "add_memory" in result["error"]


@pytest.mark.asyncio
async def test_whitelist_extra_check_can_block():
    def reject_writes_outside_skills(name: str, args: Any):
        if name == "write_file" and not args.get("path", "").startswith(
            ".agents/skills/"
        ):
            return {"error": "write_file only allowed under .agents/skills/"}
        return None

    callback = _fork.make_whitelist_callback(
        frozenset({"write_file"}),
        fork_name="review",
        extra_check=reject_writes_outside_skills,
    )
    blocked = await callback(
        tool=SimpleNamespace(name="write_file"),
        args={"path": "secrets.txt"},
        tool_context=None,
    )
    assert blocked is not None
    assert ".agents/skills/" in blocked["error"]

    allowed = await callback(
        tool=SimpleNamespace(name="write_file"),
        args={"path": ".agents/skills/foo/notes.md"},
        tool_context=None,
    )
    assert allowed is None


# =============================================================================
# log_successful_writes
# =============================================================================


def test_log_successful_writes_logs_only_successful_responses(caplog):
    import logging

    caplog.set_level(logging.INFO, logger="app.memory._fork")
    event = SimpleNamespace(
        content=SimpleNamespace(
            parts=[
                SimpleNamespace(
                    function_response=SimpleNamespace(
                        name="add_memory",
                        response={"success": True, "message": "saved"},
                    )
                ),
                SimpleNamespace(
                    function_response=SimpleNamespace(
                        name="other", response={"success": False, "error": "x"}
                    )
                ),
                SimpleNamespace(function_response=None),
            ]
        )
    )
    _fork.log_successful_writes(event, log_prefix="review_fork")
    messages = [r.message for r in caplog.records]
    assert any("review_fork: add_memory -> saved" in m for m in messages)
    assert not any("other" in m for m in messages)


def test_log_successful_writes_handles_missing_content(caplog):
    import logging

    caplog.set_level(logging.INFO, logger="app.memory._fork")
    _fork.log_successful_writes(SimpleNamespace(content=None), log_prefix="x")
    assert not caplog.records
