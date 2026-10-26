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

"""Unit tests for SourceSnapshotReader over the GCS store and an in-memory fake."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from typing import Any

import pytest
from ambient_quality_agent.tools.objects.gcs_store import GcsObjectStore
from ambient_quality_agent.tools.source_code import snapshot
from ambient_quality_agent.tools.source_code.reader import SourceSnapshotReader
from ambient_quality_agent.tools.source_code.reader import (
    clear_snapshot_cache as reset_snapshots,
)

from .conftest import FakeGcsClient as FakeStorageClient

BUCKET = "test-project-observed-aqua-source"


def build_reader(
    client: FakeStorageClient, agent_key: str = ""
) -> SourceSnapshotReader:
    """Builds a reader over the source bucket of a fake client.

    Args:
        client: Fake storage client holding the bucket's objects.
        agent_key: Key of the agent whose snapshots to read; empty for
            unprefixed ones.

    Returns:
        The reader.
    """
    store = GcsObjectStore(BUCKET, client_factory=lambda: client)
    return SourceSnapshotReader(store=store, agent_key=agent_key)


def publish_snapshot(
    client: FakeStorageClient,
    revision: str,
    files: dict[str, str],
    *,
    bucket: str = BUCKET,
    agent_key: str = "",
    ignore_patterns: list[str] | None = None,
    with_manifest: bool = True,
    truncated: set[str] | None = None,
    created_at: str = "2026-09-11T13:43:00Z",
) -> None:
    """Populates an in-memory GCS client with files and manifest for a single revision snapshot.

    Args:
        client: FakeStorageClient instance to populate.
        revision: Revision identifier string.
        files: Mapping of repository-relative file paths to file contents.
        bucket: Target GCS bucket name.
        agent_key: Agent key to publish under; empty for an unprefixed
            snapshot.
        ignore_patterns: Optional list of ignore pattern strings for the manifest.
        with_manifest: Whether to generate and store a manifest.json object.
        truncated: Optional set of file paths marked as truncated in the manifest.
        created_at: Publish time the manifest records; a republished revision
            records a later one.

    Returns:
        None.
    """
    truncated = truncated or set()
    root = snapshot.build_agent_root(agent_key)
    for path, body in files.items():
        key = (bucket, snapshot.build_file_object_name(revision, path, root))
        client.contents[key] = body.encode("utf-8")
    if not with_manifest:
        return
    manifest = {
        "schema_version": 1,
        "revision": revision,
        "engine": f"projects/p/locations/us-east1/reasoningEngines/{revision}",
        "created_at": created_at,
        "agent_directory": "app",
        "ignore_patterns": ignore_patterns or [".git", "terraform/", "/tests/"],
        "files": [
            {
                "path": path,
                "size": len(body.encode("utf-8")),
                "truncated": path in truncated,
            }
            for path, body in files.items()
        ],
        "total_bytes": sum(len(b.encode("utf-8")) for b in files.values()),
        "truncated_files": len(truncated),
        "omitted_files": 0,
    }
    client.contents[
        bucket, snapshot.build_manifest_object_name(revision, root)
    ] = json.dumps(manifest).encode("utf-8")


AGENT_PY = "\n".join(f"line {n}" for n in range(1, 11))
TOOL_PY = (
    "def lookup_user(uid):\n    return None\n\n\ndef unused():\n    pass\n"
)


@pytest.fixture(autouse=True)
def _isolated_snapshot_cache() -> Iterator[None]:
    """Resets the process-wide snapshot cache before and after each test."""
    reset_snapshots()
    yield
    reset_snapshots()


@pytest.fixture
def client() -> FakeStorageClient:
    fake = FakeStorageClient()
    publish_snapshot(fake, "1", {"app/agent.py": "old\n"})
    publish_snapshot(fake, "2", {"app/agent.py": "older\n"})
    publish_snapshot(fake, "8", {"app/agent.py": "old-ish\n"})
    publish_snapshot(
        fake, "10", {"app/agent.py": AGENT_PY, "app/tool.py": TOOL_PY}
    )
    return fake


@pytest.fixture
def reader(client: FakeStorageClient) -> SourceSnapshotReader:
    """Provides a SourceSnapshotReader instance backed by the mock storage client.

    Args:
        client: FakeStorageClient test fixture.

    Returns:
        Configured SourceSnapshotReader instance.
    """
    return build_reader(client)


def test_revisions_are_listed_newest_first_over_a_sparse_range(
    reader: SourceSnapshotReader,
) -> None:
    assert reader.list_revisions() == ["10", "8", "2", "1"]
    assert reader.get_latest_revision() == "10"


def test_latest_complete_revision_skips_an_upload_still_in_flight(
    reader: SourceSnapshotReader, client: FakeStorageClient
) -> None:
    publish_snapshot(client, "11", {"app/agent.py": "x\n"}, with_manifest=False)

    assert reader.get_latest_revision() == "11"
    assert reader.get_latest_complete_revision() == "10"


def test_empty_bucket_has_no_latest_revision() -> None:
    reader = build_reader(FakeStorageClient())

    assert reader.list_revisions() == []
    assert reader.get_latest_revision() is None
    assert reader.get_latest_complete_revision() is None


def test_manifest_is_cached_per_revision(
    reader: SourceSnapshotReader, client: FakeStorageClient
) -> None:
    first = reader.load_manifest("10")
    second = reader.load_manifest("10")

    assert first is second
    manifest_key = (BUCKET, "10/manifest.json")
    assert client.downloads.count(manifest_key) == 1


def test_a_revision_without_a_manifest_reads_as_unavailable(
    reader: SourceSnapshotReader, client: FakeStorageClient
) -> None:
    # Simulates an incomplete upload where files were written but manifest upload did not complete.
    publish_snapshot(client, "11", {"app/agent.py": "x\n"}, with_manifest=False)

    assert reader.load_manifest("11") is None


def test_a_corrupt_manifest_reads_as_unavailable(
    reader: SourceSnapshotReader, client: FakeStorageClient
) -> None:
    client.contents[BUCKET, "12/manifest.json"] = b"{not json"

    assert reader.load_manifest("12") is None


def test_read_file_returns_the_first_window_of_lines(
    reader: SourceSnapshotReader,
) -> None:
    found = reader.read_file("10", "app/agent.py", offset=1, limit=3)

    assert found is not None
    assert found.lines == ("line 1", "line 2", "line 3")
    assert (found.start_line, found.end_line, found.total_lines) == (1, 3, 10)


def test_read_file_limit_past_the_end_stops_at_the_last_line(
    reader: SourceSnapshotReader,
) -> None:
    found = reader.read_file("10", "app/agent.py", offset=8, limit=500)

    assert found is not None
    assert found.lines == ("line 8", "line 9", "line 10")
    assert found.end_line == 10


def test_read_file_offset_past_the_end_returns_no_lines(
    reader: SourceSnapshotReader,
) -> None:
    found = reader.read_file("10", "app/agent.py", offset=99, limit=10)

    assert found is not None
    assert found.lines == ()
    assert found.total_lines == 10
    assert found.is_empty


def test_read_file_reports_a_body_missing_from_the_snapshot(
    reader: SourceSnapshotReader,
) -> None:
    assert reader.read_file("10", "app/never_uploaded.py") is None


def test_read_file_flags_a_file_truncated_at_publish_time(
    client: FakeStorageClient,
) -> None:
    publish_snapshot(
        client, "13", {"app/big.py": "x\n"}, truncated={"app/big.py"}
    )
    reader = build_reader(client)

    found = reader.read_file("13", "app/big.py")

    assert found is not None
    assert found.truncated_in_snapshot


def test_search_returns_matches_with_context_and_a_citation(
    reader: SourceSnapshotReader,
) -> None:
    matches, searched, truncated = reader.search(
        "10", pattern=r"def lookup_user", context_lines=1
    )

    assert searched == 2
    assert not truncated
    assert len(matches) == 1
    match = matches[0]
    assert match.path == "app/tool.py"
    assert match.line_number == 1
    assert match.before == ()
    assert match.after == ("    return None",)
    assert match.citation == "app/tool.py:1-2"


def test_search_can_be_narrowed_to_a_path_substring(
    reader: SourceSnapshotReader,
) -> None:
    matches, searched, _ = reader.search(
        "10", pattern="def ", path_contains="tool"
    )

    assert searched == 1
    assert {m.path for m in matches} == {"app/tool.py"}


def test_search_with_no_match_returns_an_empty_result_not_an_error(
    reader: SourceSnapshotReader,
) -> None:
    matches, searched, truncated = reader.search("10", pattern="nowhere_at_all")

    assert matches == []
    assert searched == 2
    assert not truncated


def test_search_on_a_revision_without_a_manifest_searches_nothing(
    reader: SourceSnapshotReader,
) -> None:
    assert reader.search("99", pattern="def ") == ([], 0, False)


def test_search_stops_at_the_match_cap(client: FakeStorageClient) -> None:
    from ambient_quality_agent.tools.source_code import reader as reader_module

    body = "match\n" * (reader_module.MAX_SEARCH_MATCHES + 5)
    publish_snapshot(client, "14", {"app/many.py": body})
    reader = build_reader(client)

    matches, _, truncated = reader.search("14", pattern="match")

    assert truncated
    assert len(matches) == reader_module.MAX_SEARCH_MATCHES


def test_an_unexpected_gcs_error_is_not_swallowed_as_absence(
    client: FakeStorageClient,
) -> None:
    from google.api_core import exceptions as api_exceptions

    class Forbidden(FakeStorageClient):
        def bucket(self, name: str) -> Any:
            raise api_exceptions.Forbidden("no access")

    reader = build_reader(Forbidden())

    with pytest.raises(api_exceptions.Forbidden):
        reader.load_manifest("10")


def test_line_numbering_follows_the_editor_not_unicode_line_breaks(
    client: FakeStorageClient,
) -> None:
    # Form feeds are treated as line breaks by str.splitlines but not standard text editors;
    # verify line splitting aligns with editor line numbering.
    publish_snapshot(
        client, "15", {"app/ff.py": "first\x0csame line\nsecond\n"}
    )
    reader = build_reader(client)

    found = reader.read_file("15", "app/ff.py")

    assert found is not None
    assert found.total_lines == 2
    assert found.lines == ("first\x0csame line", "second")


def _list_file_downloads(client: FakeStorageClient) -> list[tuple[str, str]]:
    """Lists the file bodies a fake client has served, leaving out manifests.

    Args:
        client: Fake storage client that recorded its downloads.

    Returns:
        The `(bucket, object_name)` key of each file download, in order.
    """
    return [key for key in client.downloads if "/files/" in key[1]]


def test_a_file_read_once_is_not_read_again_by_a_later_reader(
    client: FakeStorageClient,
) -> None:
    """Every tool call builds its own reader, so the files outlive a reader."""
    build_reader(client).search("10", pattern="def ")
    first_search = len(_list_file_downloads(client))

    build_reader(client).search("10", pattern="return")
    found = build_reader(client).read_file("10", "app/tool.py")

    assert first_search == 2
    assert len(_list_file_downloads(client)) == first_search
    assert found is not None and found.lines[0] == "def lookup_user(uid):"


def test_files_stay_cached_until_a_republish_replaces_the_manifest(
    client: FakeStorageClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ambient_quality_agent.tools.source_code import reader as reader_module

    # Every read goes back to the manifest, as another replica's does once its
    # cached copy expires.
    monkeypatch.setattr(reader_module, "MANIFEST_CACHE_TTL_SECONDS", 0.0)
    build_reader(client).search("10", pattern="def ")
    build_reader(client).search("10", pattern="def ")
    same_publish = len(_list_file_downloads(client))
    publish_snapshot(
        client,
        "10",
        {"app/agent.py": "def changed():\n"},
        created_at="2026-09-12T08:00:00Z",
    )

    matches, _, _ = build_reader(client).search("10", pattern="def ")

    assert same_publish == 2
    assert [match.line for match in matches] == ["def changed():"]


def test_forgetting_a_snapshot_drops_its_files(
    client: FakeStorageClient,
) -> None:
    """A republish within one second records the same `created_at`."""
    from ambient_quality_agent.tools.source_code import reader as reader_module

    build_reader(client).search("10", pattern="def ")
    publish_snapshot(client, "10", {"app/agent.py": "def changed():\n"})
    reader_module.forget_cached_snapshot(f"gs://{BUCKET}/", "10")

    matches, _, _ = build_reader(client).search("10", pattern="def ")

    assert [match.line for match in matches] == ["def changed():"]


def test_files_stay_cached_for_the_most_recently_read_revisions_only(
    client: FakeStorageClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ambient_quality_agent.tools.source_code import reader as reader_module

    monkeypatch.setattr(reader_module, "MAX_CACHED_REVISIONS", 1)
    build_reader(client).search("10", pattern="def ")
    build_reader(client).search("8", pattern="def ")
    before = len(_list_file_downloads(client))

    build_reader(client).search("10", pattern="def ")

    assert len(_list_file_downloads(client)) == before + 2


def test_files_missing_from_the_cache_are_read_concurrently(
    client: FakeStorageClient,
) -> None:
    """Read one after another, a snapshot of 2,000 files takes minutes."""
    both_in_flight = threading.Barrier(2, timeout=5)

    class GatedStore(GcsObjectStore):
        def read_bytes(self, name: str) -> bytes | None:
            if "/files/" in name:
                both_in_flight.wait()
            return super().read_bytes(name)

    store = GatedStore(BUCKET, client_factory=lambda: client)

    matches, searched, _ = SourceSnapshotReader(store=store).search(
        "10", pattern="def "
    )

    assert searched == 2
    assert len(matches) == 2


# --- the agent prefix ----------------------------------------------------- #


def test_an_agent_reads_its_own_snapshots_and_the_unprefixed_ones(
    client: FakeStorageClient,
) -> None:
    """Unprefixed snapshots stay readable beside the agent's own."""
    publish_snapshot(client, "12", {"app/agent.py": "mine\n"}, agent_key="a")
    publish_snapshot(client, "3", {"app/agent.py": "other\n"}, agent_key="b")

    reader = build_reader(client, "a")

    assert reader.list_revisions() == ["12", "10", "8", "2", "1"]
    mine = reader.read_file("12", "app/agent.py")
    legacy = reader.read_file("10", "app/agent.py")
    assert mine is not None and mine.lines == ("mine",)
    assert legacy is not None and legacy.total_lines == 10
    assert reader.build_manifest_uri("12") == (
        f"gs://{BUCKET}/a/12/manifest.json"
    )
    assert reader.build_manifest_uri("10") == f"gs://{BUCKET}/10/manifest.json"


def test_the_agents_own_snapshot_wins_over_an_unprefixed_one(
    client: FakeStorageClient,
) -> None:
    publish_snapshot(client, "10", {"app/agent.py": "mine\n"}, agent_key="a")

    reader = build_reader(client, "a")

    found = reader.read_file("10", "app/agent.py")
    assert found is not None and found.lines == ("mine",)
    manifest = reader.load_manifest("10")
    assert manifest is not None
    assert [f.path for f in manifest.files] == ["app/agent.py"]


def test_a_keyed_agent_never_reads_its_bare_names_snapshots() -> None:
    """The bare name can be the key of the same agent in AQuA's own project."""
    client = FakeStorageClient()
    key = "acme-prod__travel_agent"
    publish_snapshot(client, "12", {"a.py": "prod\n"}, agent_key=key)
    publish_snapshot(client, "40", {"a.py": "own\n"}, agent_key="travel_agent")
    publish_snapshot(
        client,
        "30",
        {"a.py": "staging\n"},
        agent_key="acme-staging__travel_agent",
    )
    publish_snapshot(client, "5", {"a.py": "unprefixed\n"})

    reader = build_reader(client, key)

    assert reader.list_revisions() == ["12", "5"]
    assert reader.get_latest_complete_revision() == "12"
    assert reader.read_file("40", "a.py") is None
    prod = reader.read_file("12", "a.py")
    assert prod is not None and prod.lines == ("prod",)
    assert reader.location == f"gs://{BUCKET}/{key}/"


def test_an_agents_cached_manifest_is_not_another_agents() -> None:
    """Two agents' snapshots of one revision number are cached apart.

    Both the manifests and the file bodies, which share a path here.
    """
    client = FakeStorageClient()
    publish_snapshot(
        client, "7", {"prod.py": "x\n", "agent.py": "prod\n"}, agent_key="p__a"
    )
    publish_snapshot(
        client,
        "7",
        {"staging.py": "x\n", "agent.py": "staging\n"},
        agent_key="s__a",
    )
    prod_reader = build_reader(client, "p__a")
    staging_reader = build_reader(client, "s__a")

    prod = prod_reader.load_manifest("7")
    staging = staging_reader.load_manifest("7")
    prod_file = prod_reader.read_file("7", "agent.py")
    staging_file = staging_reader.read_file("7", "agent.py")

    assert prod is not None
    assert sorted(f.path for f in prod.files) == ["agent.py", "prod.py"]
    assert staging is not None
    assert sorted(f.path for f in staging.files) == ["agent.py", "staging.py"]
    assert prod_file is not None and prod_file.lines == ("prod",)
    assert staging_file is not None and staging_file.lines == ("staging",)


def test_another_agents_prefix_is_not_a_revision() -> None:
    client = FakeStorageClient()
    publish_snapshot(client, "5", {"a.py": "x\n"}, agent_key="other_agent")

    assert build_reader(client, "a").list_revisions() == []
    assert build_reader(client).list_revisions() == []


def test_a_path_leaving_the_snapshot_reads_as_absent_from_a_directory(
    tmp_path: Any,
) -> None:
    """As GCS answers it: no such object, rather than an error."""
    from ambient_quality_agent.standalone.files import FileObjectStore

    store = FileObjectStore(tmp_path)
    store.write_bytes("a/10/files/app/agent.py", b"x\n")
    reader = SourceSnapshotReader(store=store, agent_key="a")

    assert reader.read_file("10", "../../../secret") is None
    found = reader.read_file("10", "app/agent.py")
    assert found is not None and found.lines == ("x",)
