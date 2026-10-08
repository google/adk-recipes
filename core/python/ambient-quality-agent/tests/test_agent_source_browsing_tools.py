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

"""Unit tests for agent source browsing tools: revision resolution, file listing, search, and pagination."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from ambient_quality_agent.tools.source_code import (
    agent_source_browsing_tools as tools,
)
from ambient_quality_agent.tools.source_code import reader as reader_module
from google.api_core import exceptions as api_exceptions

from tests.conftest import ToolContext
from tests.test_source_code_reader import (
    BUCKET,
    FakeStorageClient,
    build_reader,
    publish_snapshot,
)

AGENT_PY = "\n".join(f"line {n}" for n in range(1, 11))
TOOL_PY = (
    "def lookup_user(uid):\n    return None\n\n\ndef unused():\n    pass\n"
)


def run(coro: Any) -> dict[str, Any]:
    """Executes an async tool coroutine synchronously in an event loop.

    Args:
        coro: Coroutine to execute.

    Returns:
        Result dictionary returned by the tool.
    """
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _isolated_manifest_cache() -> Iterator[None]:
    """Clears the manifest cache before and after each test for test isolation."""
    reader_module.clear_manifest_cache()
    yield
    reader_module.clear_manifest_cache()


@pytest.fixture
def client() -> FakeStorageClient:
    """Provides a fake GCS client populated with sample revision snapshots.

    Returns:
        Configured FakeStorageClient instance.
    """
    fake = FakeStorageClient()
    publish_snapshot(fake, "2", {"app/agent.py": "ancient\n"})
    publish_snapshot(fake, "8", {"app/agent.py": "older\n"})
    publish_snapshot(
        fake, "10", {"app/agent.py": AGENT_PY, "app/tools/lookup.py": TOOL_PY}
    )
    return fake


@pytest.fixture(autouse=True)
def wired(client: FakeStorageClient) -> Iterator[FakeStorageClient]:
    """Injects a fake reader factory backed by the test storage client.

    Args:
        client: FakeStorageClient test fixture.

    Yields:
        The configured FakeStorageClient instance.
    """
    original = reader_module.reader_factory
    reader_module.reader_factory = lambda state: build_reader(client)
    try:
        yield client
    finally:
        reader_module.reader_factory = original


@pytest.fixture
def unconfigured() -> Iterator[None]:
    """Simulates an unconfigured snapshot bucket where reader factory returns None.

    Yields:
        None while the reader factory is overridden.
    """
    original = reader_module.reader_factory
    reader_module.reader_factory = lambda state: None
    try:
        yield
    finally:
        reader_module.reader_factory = original


# --------------------------------------------------------------------------- #
# Revision resolution                                                          #
# --------------------------------------------------------------------------- #


def test_the_revision_never_travels_through_session_state() -> None:
    """Callers pass the revision as an argument, so no RCA state key may reappear."""
    assert not [name for name in vars(tools) if name.startswith("RCA_")]


def test_without_an_explicit_revision_the_newest_snapshot_is_used_and_named() -> (
    None
):
    result = run(tools.list_source_files(ToolContext()))

    assert result["revision"] == "10"
    assert result["revision_source"] == "latest_fallback"


def test_an_explicit_revision_wins_over_the_newest_snapshot() -> None:
    result = run(tools.list_source_files(ToolContext(), revision="8"))

    assert result["revision"] == "8"
    assert result["revision_source"] == "explicit"


def test_every_tool_names_the_revision_it_answered_from() -> None:
    context = ToolContext()

    answers = [
        run(tools.list_source_files(context, revision="10")),
        run(tools.search_source(context, pattern="line 1", revision="10")),
        run(
            tools.read_source_file(context, path="app/agent.py", revision="10")
        ),
    ]

    assert [a["revision"] for a in answers] == ["10"] * 3
    assert all(a["revision_source"] for a in answers)


def test_an_explicit_revision_is_reported_as_explicit_by_every_tool_taking_one() -> (
    None
):
    context = ToolContext()

    answers = [
        run(tools.list_source_files(context, revision="8")),
        run(tools.search_source(context, pattern="older", revision="8")),
        run(tools.read_source_file(context, path="app/agent.py", revision="8")),
    ]

    assert [a["revision_source"] for a in answers] == ["explicit"] * 3


def test_reading_a_revision_leaves_no_trace_in_session_state() -> None:
    """Revision is an argument, so a read must not make the next call behave differently."""
    context = ToolContext()

    run(tools.list_source_files(context, revision="8"))

    assert context.state == {}


def test_list_revisions_resolves_no_revision_of_its_own() -> None:
    """Listing snapshots inspects no specific revision, so no current revision is resolved."""
    result = run(tools.list_revisions(ToolContext()))

    assert "revision" not in result
    assert "revision_source" not in result


# --------------------------------------------------------------------------- #
# Refusals: no snapshot at all                                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("unconfigured")
def test_an_unconfigured_bucket_returns_a_worded_refusal_not_an_empty_list() -> (
    None
):
    result = run(tools.list_source_files(ToolContext()))

    assert result["available"] is False
    assert "files" not in result
    assert "no source snapshot store is configured" in result["reason"].lower()


def test_an_empty_bucket_returns_a_worded_refusal() -> None:
    empty = FakeStorageClient()
    reader_module.reader_factory = lambda state: build_reader(empty)

    result = run(tools.list_revisions(ToolContext()))

    assert result["available"] is False
    assert "no source snapshot has been published" in result["reason"].lower()


def test_a_revision_with_no_manifest_refuses_rather_than_reading_empty(
    client: FakeStorageClient,
) -> None:
    publish_snapshot(client, "11", {"app/agent.py": "x\n"}, with_manifest=False)

    result = run(tools.list_source_files(ToolContext(), revision="11"))

    assert result["available"] is False
    assert "manifest" in result["reason"]
    # Both causes are named: a bucket-retention deletion and an interrupted upload
    # are indistinguishable from here, and guessing one would misattribute the other.
    assert "retention" in result["reason"]
    assert "did not finish" in result["reason"]


def test_the_fallback_skips_an_upload_in_flight_for_the_newest_complete_one(
    client: FakeStorageClient,
) -> None:
    publish_snapshot(client, "11", {"app/agent.py": "x\n"}, with_manifest=False)

    result = run(tools.list_source_files(ToolContext()))

    assert (result["revision"], result["revision_source"]) == (
        "10",
        "latest_fallback",
    )


def test_a_manifest_entry_whose_body_is_missing_reports_an_incomplete_upload(
    client: FakeStorageClient,
) -> None:
    del client.contents[BUCKET, "10/files/app/agent.py"]

    result = run(tools.read_source_file(ToolContext(), path="app/agent.py"))

    assert result["found"] is False
    assert "upload was incomplete" in result["reason"]


# --------------------------------------------------------------------------- #
# Listing                                                                      #
# --------------------------------------------------------------------------- #


def test_list_revisions_orders_non_contiguous_revisions_numerically() -> None:
    result = run(tools.list_revisions(ToolContext()))

    assert [entry["revision"] for entry in result["revisions"]] == [
        "10",
        "8",
        "2",
    ]
    assert result["revisions"][0]["file_count"] == 2


def test_list_revisions_flags_an_incomplete_snapshot(
    client: FakeStorageClient,
) -> None:
    publish_snapshot(client, "11", {"app/agent.py": "x\n"}, with_manifest=False)

    result = run(tools.list_revisions(ToolContext()))

    incomplete = next(e for e in result["revisions"] if e["revision"] == "11")
    assert incomplete["complete"] is False


def test_list_source_files_filters_by_glob_and_substring() -> None:
    context = ToolContext()

    by_glob = run(tools.list_source_files(context, glob="**/tools/*.py"))
    by_substring = run(tools.list_source_files(context, path_contains="agent"))

    assert [f["path"] for f in by_glob["files"]] == ["app/tools/lookup.py"]
    assert [f["path"] for f in by_substring["files"]] == ["app/agent.py"]


def test_a_double_star_glob_also_covers_files_at_the_repository_root(
    client: FakeStorageClient,
) -> None:
    publish_snapshot(client, "12", {"setup.py": "x\n", "app/agent.py": "y\n"})

    result = run(
        tools.list_source_files(ToolContext(), glob="**/*.py", revision="12")
    )

    assert [f["path"] for f in result["files"]] == ["setup.py", "app/agent.py"]


def test_list_source_files_reports_the_patterns_that_were_excluded() -> None:
    result = run(tools.list_source_files(ToolContext()))

    assert result["ignore_patterns"] == [".git", "terraform/", "/tests/"]
    assert result["agent_directory"] == "app"


# --------------------------------------------------------------------------- #
# Search                                                                       #
# --------------------------------------------------------------------------- #


def test_search_returns_context_and_a_ready_made_citation() -> None:
    result = run(
        tools.search_source(
            ToolContext(), pattern=r"def lookup_user", context_lines=1
        )
    )

    assert result["match_count"] == 1
    match = result["matches"][0]
    assert match["path"] == "app/tools/lookup.py"
    assert match["line_number"] == 1
    assert match["after"] == ["    return None"]
    assert match["citation"] == "app/tools/lookup.py:1-2"


def test_search_with_no_match_is_an_empty_match_list_not_a_refusal() -> None:
    result = run(tools.search_source(ToolContext(), pattern="does_not_occur"))

    assert result["matches"] == []
    assert result["match_count"] == 0
    assert result["files_searched"] == 2


def test_search_rejects_an_invalid_regular_expression() -> None:
    result = run(tools.search_source(ToolContext(), pattern="("))

    assert "invalid regular expression" in result["error"]


def test_search_requires_a_pattern() -> None:
    result = run(tools.search_source(ToolContext(), pattern=""))

    assert "pattern is required" in result["error"]


# --------------------------------------------------------------------------- #
# Reading, and the three-way absence                                           #
# --------------------------------------------------------------------------- #


def test_read_source_file_numbers_the_lines_it_returns() -> None:
    result = run(
        tools.read_source_file(
            ToolContext(), path="app/agent.py", offset=1, limit=3
        )
    )

    assert result["found"] is True
    assert result["content"].splitlines() == [
        "     1: line 1",
        "     2: line 2",
        "     3: line 3",
    ]
    assert (result["start_line"], result["end_line"]) == (1, 3)
    assert result["total_lines"] == 10


def test_read_source_file_numbers_from_a_mid_file_offset() -> None:
    result = run(
        tools.read_source_file(
            ToolContext(), path="app/agent.py", offset=9, limit=50
        )
    )

    assert result["content"].splitlines() == [
        "     9: line 9",
        "    10: line 10",
    ]
    assert result["end_line"] == 10


def test_read_source_file_past_the_end_returns_nothing_to_quote() -> None:
    result = run(
        tools.read_source_file(ToolContext(), path="app/agent.py", offset=99)
    )

    assert result["found"] is True
    assert result["content"] == ""
    assert result["line_count"] == 0
    assert result["total_lines"] == 10
    # Empty window omits start_line and end_line to avoid an invalid range.
    assert "start_line" not in result
    assert "end_line" not in result
    assert result["requested_offset"] == 99
    assert "past the end" in result["reason"]


def test_an_excluded_path_names_the_pattern_that_hid_it() -> None:
    result = run(
        tools.read_source_file(ToolContext(), path="terraform/main.tf")
    )

    assert result["found"] is False
    assert result["excluded_by_pattern"] == "terraform/"
    assert "excluded by pattern `terraform/`" in result["reason"]


def test_an_absent_path_says_the_repository_has_no_such_path() -> None:
    result = run(tools.read_source_file(ToolContext(), path="app/nowhere.py"))

    assert result["found"] is False
    assert "excluded_by_pattern" not in result
    assert "no such path" in result["reason"]


def test_read_source_file_requires_a_path() -> None:
    result = run(tools.read_source_file(ToolContext(), path=""))

    assert "path is required" in result["error"]


class _BrokenReader:
    """Snapshot reader whose listing call always fails with a chosen exception."""

    location = f"gs://{BUCKET}/"

    def __init__(self, error: Exception) -> None:
        self._error = error

    def list_revisions(self) -> list[str]:
        raise self._error


def test_a_gcs_failure_is_reported_as_a_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        reader_module,
        "reader_factory",
        lambda state: _BrokenReader(api_exceptions.Forbidden("no access")),
    )

    result = run(tools.list_revisions(ToolContext()))

    assert result["available"] is False
    assert "no access" in result["reason"]


def test_a_programming_error_propagates_rather_than_reading_as_a_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        reader_module,
        "reader_factory",
        lambda state: _BrokenReader(AttributeError("bucket")),
    )

    with pytest.raises(AttributeError):
        run(tools.list_revisions(ToolContext()))


def test_the_four_tools_are_exported_for_the_chat_agent() -> None:
    assert tools.AGENT_SOURCE_BROWSING_TOOLS == [
        tools.list_revisions,
        tools.list_source_files,
        tools.search_source,
        tools.read_source_file,
    ]
