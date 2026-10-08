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

"""Tests for the chat's memory tools: it remembers, and reads what is remembered."""

from __future__ import annotations

import asyncio

import pytest
from ambient_quality_agent.core import memory_chat_tools
from ambient_quality_agent.tools.documents import memories as memories_doc
from ambient_quality_agent.tools.objects import store as objects
from ambient_quality_agent.tools.objects.gcs_store import GcsObjectStore

from tests.conftest import CallbackContext, FakeGcsClient, ToolContext

_TEXT = "The agent's system prompt is in app/prompts/system.md."


@pytest.fixture
def jobs(monkeypatch: pytest.MonkeyPatch) -> GcsObjectStore:
    client = FakeGcsClient()
    bound = GcsObjectStore("jobs-bkt", client_factory=lambda: client)
    monkeypatch.setattr(objects, "jobs_store_factory", lambda: bound)
    return bound


def _remember(
    text: str,
    session_id: str = "chat-1",
    *,
    said: str = "Please remember where the prompt is.",
) -> dict:
    """Calls `remember` in a turn the developer started by saying `said`."""
    return asyncio.run(
        memory_chat_tools.remember(
            text=text,
            tool_context=CallbackContext(said, session_id=session_id),
        )
    )


def _get_memories() -> dict:
    return asyncio.run(
        memory_chat_tools.get_memories(tool_context=ToolContext())
    )


def test_remembering_stores_a_memory_from_this_chat(jobs) -> None:
    result = _remember(_TEXT, session_id="chat-42")

    assert result["saved"] is True
    [stored] = memories_doc.load_memories(jobs)
    assert stored.id == result["id"]
    assert stored.text == _TEXT
    assert stored.source == "chat-42"


def test_a_remembered_memory_is_read_back_at_once(jobs) -> None:
    _remember(_TEXT)

    assert [x["text"] for x in _get_memories()["memories"]] == [_TEXT]


@pytest.mark.parametrize(
    "said",
    [
        "Find the root cause of insight 42.",
        "Run a custom investigation over refunds.",
        "",
    ],
)
def test_nothing_is_stored_unless_the_developer_asks_to_remember(
    jobs, said
) -> None:
    """The chat also reads traces and tool results; one that says "the
    developer asked you to remember" must not be enough."""
    result = _remember(_TEXT, said=said)

    assert "Not stored" in result["error"]
    assert memories_doc.load_memories(jobs) == []


@pytest.mark.parametrize(
    "said",
    [
        "Remember that the prompt is in app/prompts/system.md",
        "add this to memory",
    ],
)
def test_asking_in_other_words_with_remember_or_memory_is_enough(
    jobs, said
) -> None:
    assert _remember(_TEXT, said=said)["saved"] is True


def test_memories_come_with_a_note_to_read_them_as_data(jobs) -> None:
    _remember(_TEXT)

    note = _get_memories()["note"]

    assert "not as instructions" in note


def test_remembering_the_same_text_again_reports_it(jobs) -> None:
    first = _remember(_TEXT)

    again = _remember(_TEXT, session_id="chat-2")

    assert again["saved"] is False
    assert again["id"] == first["id"]
    assert len(memories_doc.load_memories(jobs)) == 1


def test_a_memory_over_500_characters_is_refused(jobs) -> None:
    result = _remember("x" * 501)

    assert "memory" in result["error"]
    assert "500" in result["error"]
    assert memories_doc.load_memories(jobs) == []


def test_get_memories_turns_a_storage_failure_into_a_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom() -> GcsObjectStore:
        raise RuntimeError("bucket gone")

    monkeypatch.setattr(objects, "jobs_store_factory", _boom)

    result = _get_memories()

    assert result["available"] is False
    assert result["memories"] == []
    assert "bucket gone" in result["reason"]
