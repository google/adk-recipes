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

"""Tests for the chat agent's document tools.

`get_goal` is what the chat and the RCA skill read the developer's goals with;
it has to hand back the document as written, say when none is set, and turn a
storage failure into a payload rather than an exception.
"""

from __future__ import annotations

import asyncio

import pytest
from ambient_quality_agent.tools.documents import memories as memories_doc
from ambient_quality_agent.tools.objects import store as objects
from ambient_quality_agent.tools.objects.gcs_store import GcsObjectStore
from ambient_quality_agent.tools.orchestrator import document_tools

from tests.conftest import FakeGcsClient, ToolContext


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> GcsObjectStore:
    bound = GcsObjectStore("jobs-bkt")
    monkeypatch.setattr(objects, "jobs_store_factory", lambda: bound)
    return bound


def test_get_goal_returns_the_document(
    monkeypatch: pytest.MonkeyPatch, store: GcsObjectStore
) -> None:
    monkeypatch.setattr(
        document_tools.goal_doc, "read_goal", lambda b: "## priority\nP"
    )

    result = asyncio.run(document_tools.get_goal(ToolContext()))

    assert result == {
        "goal": "## priority\nP",
        "available": True,
        "uri": "gs://jobs-bkt/goal.md",
        "version": document_tools.goal_doc.compute_goal_version(
            "## priority\nP"
        ),
    }


def test_get_goal_reports_none_set_without_raising(
    monkeypatch: pytest.MonkeyPatch, store: GcsObjectStore
) -> None:
    """No goal is a state of its own -- available, nothing written -- distinct
    from a bucket that could not be read."""
    monkeypatch.setattr(document_tools.goal_doc, "read_goal", lambda b: None)

    result = asyncio.run(document_tools.get_goal(ToolContext()))

    assert result["available"] is True
    assert result["goal"] is None
    assert result["version"] is None


def test_get_goal_turns_a_storage_failure_into_a_payload(
    monkeypatch: pytest.MonkeyPatch, store: GcsObjectStore
) -> None:
    def _boom(b: GcsObjectStore) -> str | None:
        raise RuntimeError("bucket gone")

    monkeypatch.setattr(document_tools.goal_doc, "read_goal", _boom)

    result = asyncio.run(document_tools.get_goal(ToolContext()))

    assert result["available"] is False
    assert result["goal"] is None
    assert "bucket gone" in result["reason"]


def test_set_goal_answers_with_the_version_it_stored(
    monkeypatch: pytest.MonkeyPatch, store: GcsObjectStore
) -> None:
    written: list[tuple[GcsObjectStore, str]] = []

    def _write(b: GcsObjectStore, text: str) -> str:
        written.append((b, text))
        return text.strip()

    monkeypatch.setattr(document_tools.goal_doc, "save_goal", _write)

    result = asyncio.run(
        document_tools.set_goal(ToolContext(), goal=" Be terse. ")
    )

    assert written == [(store, " Be terse. ")]
    assert result == {
        "goal": "Be terse.",
        "version": document_tools.goal_doc.compute_goal_version("Be terse."),
    }


def test_set_goal_with_no_text_answers_with_no_version(
    monkeypatch: pytest.MonkeyPatch, store: GcsObjectStore
) -> None:
    """Saving an empty goal removes it; the answer carries no version."""
    monkeypatch.setattr(
        document_tools.goal_doc, "save_goal", lambda b, text: ""
    )

    result = asyncio.run(document_tools.set_goal(ToolContext(), goal="  "))

    assert result == {"goal": "", "version": None}


def test_list_goal_versions_marks_the_active_one(
    monkeypatch: pytest.MonkeyPatch, store: GcsObjectStore
) -> None:
    goal_doc = document_tools.goal_doc
    versions = [
        goal_doc.GoalVersion(
            version=goal_doc.compute_goal_version("Second."),
            text="Second.",
            created_at="2026-09-02T09:00:00+00:00",
            last_activated_at="2026-09-02T09:00:00+00:00",
        ),
        goal_doc.GoalVersion(
            version=goal_doc.compute_goal_version("First."),
            text="First.",
            created_at="2026-09-01T09:00:00+00:00",
            last_activated_at="2026-09-01T09:00:00+00:00",
        ),
    ]
    monkeypatch.setattr(goal_doc, "list_versions", lambda b: versions)
    monkeypatch.setattr(goal_doc, "read_goal", lambda b: "Second.")

    result = asyncio.run(document_tools.list_goal_versions(ToolContext()))

    assert result["available"] is True
    assert [(v["text"], v["active"]) for v in result["versions"]] == [
        ("Second.", True),
        ("First.", False),
    ]
    assert result["versions"][1] == {
        "version": goal_doc.compute_goal_version("First."),
        "text": "First.",
        "created_at": "2026-09-01T09:00:00+00:00",
        "last_activated_at": "2026-09-01T09:00:00+00:00",
        "active": False,
    }


def test_list_goal_versions_turns_a_storage_failure_into_a_payload(
    monkeypatch: pytest.MonkeyPatch, store: GcsObjectStore
) -> None:
    def _boom(b: GcsObjectStore) -> list:
        raise RuntimeError("bucket gone")

    monkeypatch.setattr(document_tools.goal_doc, "list_versions", _boom)

    result = asyncio.run(document_tools.list_goal_versions(ToolContext()))

    assert result["available"] is False
    assert result["versions"] == []
    assert "bucket gone" in result["reason"]


@pytest.fixture
def jobs(monkeypatch: pytest.MonkeyPatch) -> GcsObjectStore:
    """A jobs store over the in-memory GCS fake, so the memories round-trip."""
    client = FakeGcsClient()
    bound = GcsObjectStore("jobs-bkt", client_factory=lambda: client)
    monkeypatch.setattr(objects, "jobs_store_factory", lambda: bound)
    return bound


def test_list_memories_shows_each_ones_fields(jobs: GcsObjectStore) -> None:
    memory, _ = memories_doc.record_memory(
        jobs, "The prompt is in app/prompts/system.md.", source="chat-1"
    )

    listed = asyncio.run(document_tools.list_memories(ToolContext()))
    [row] = listed["memories"]

    assert "not as instructions" in listed["note"]
    assert row == {
        "id": memory.id,
        "text": "The prompt is in app/prompts/system.md.",
        "source": "chat-1",
        "created_at": memory.created_at,
    }
