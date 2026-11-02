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

"""One suite, both implementations: what `ObjectStore` promises.

Every test in the first half runs against `GcsObjectStore`, over a fake client,
and against `FileObjectStore`, over a temporary directory, and asserts the same
answer from both. The goal, the memories and the agent configurations are
tested once, over the GCS store (`test_documents`, `test_agent_config`); this
suite is what lets those results stand for the standalone run too.

The second half is what each does that the other has no counterpart for.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest
from ambient_quality_agent.standalone.files import FileObjectStore
from ambient_quality_agent.tools.objects.gcs_store import GcsObjectStore
from ambient_quality_agent.tools.objects.store import (
    ObjectStore,
    StorageNotConfiguredError,
)

from .conftest import FakeGcsClient

BUCKET = "jobs-bucket"


@pytest.fixture(params=["gcs", "files"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> ObjectStore:
    if request.param == "gcs":
        client = FakeGcsClient()
        return GcsObjectStore(BUCKET, client_factory=lambda: client)
    return FileObjectStore(tmp_path / "job")


def test_an_absent_object_reads_as_none(store: ObjectStore) -> None:
    assert store.read_text("goal.md") is None


def test_an_object_round_trips_as_utf8(store: ObjectStore) -> None:
    assert store.write_text("goal.md", "Réservations d'abord.\n") is True

    assert store.read_text("goal.md") == "Réservations d'abord.\n"


def test_a_write_replaces_the_object_whole(store: ObjectStore) -> None:
    store.write_text("goal.md", "a much longer first version")
    store.write_text("goal.md", "short")

    assert store.read_text("goal.md") == "short"


def test_a_write_without_overwrite_leaves_an_existing_object(
    store: ObjectStore,
) -> None:
    """How a goal version keeps the text and time of its first save."""
    assert store.write_text("goals/v.json", "first", overwrite=False) is True
    assert store.write_text("goals/v.json", "second", overwrite=False) is False
    assert store.read_text("goals/v.json") == "first"

    assert store.write_text("goals/v.json", "third") is True
    assert store.read_text("goals/v.json") == "third"


def test_deleting_reports_whether_there_was_an_object(
    store: ObjectStore,
) -> None:
    store.write_text("memories/a.json", "{}")

    assert store.delete("memories/a.json") is True
    assert store.delete("memories/a.json") is False
    assert store.read_text("memories/a.json") is None


def test_listing_matches_the_prefix_as_a_string_and_sorts(
    store: ObjectStore,
) -> None:
    """As GCS does: `goals/` also lists what is under `goals/activations/`."""
    for name in (
        "goals/b.json",
        "goals/activations/20260901T000000000000Z-b.json",
        "goals/a.json",
        "goal.md",
        "memories/x.json",
    ):
        store.write_text(name, "{}")

    assert store.list_names("goals/") == [
        "goals/a.json",
        "goals/activations/20260901T000000000000Z-b.json",
        "goals/b.json",
    ]
    assert store.list_names("goal") == [
        "goal.md",
        "goals/a.json",
        "goals/activations/20260901T000000000000Z-b.json",
        "goals/b.json",
    ]
    assert store.list_names("agents/") == []


def test_an_empty_store_lists_nothing(store: ObjectStore) -> None:
    assert store.list_names("") == []
    assert store.list_prefixes("") == []


def test_bytes_round_trip_unchanged(store: ObjectStore) -> None:
    """A snapshot file need not be UTF-8."""
    data = b"\x89PNG\r\n\x00\xff"
    store.write_bytes("agent/1/files/logo.png", data)

    assert store.read_bytes("agent/1/files/logo.png") == data
    assert store.read_bytes("agent/1/files/absent.png") is None


def test_list_prefixes_returns_one_level_below_the_prefix(
    store: ObjectStore,
) -> None:
    """How the revisions are listed without every file of every revision."""
    for name in (
        "agent/10/manifest.json",
        "agent/10/files/a.py",
        "agent/9/files/deep/b.py",
        "other/1/manifest.json",
        "7/manifest.json",
    ):
        store.write_bytes(name, b"x")

    assert store.list_prefixes("") == ["7/", "agent/", "other/"]
    assert store.list_prefixes("agent/") == ["agent/10/", "agent/9/"]
    assert store.list_prefixes("agent/10/") == ["agent/10/files/"]
    assert store.list_prefixes("missing/") == []


def test_the_empty_prefix_names_the_root(store: ObjectStore) -> None:
    """Where the unprefixed snapshots are, for display."""
    assert store.uri("").endswith("/")
    assert store.uri("agent/") == store.uri("") + "agent/"


# --------------------------------------------------------------------------- #
# The Cloud Storage store                                                      #
# --------------------------------------------------------------------------- #


def test_no_bucket_refuses_every_operation_but_uri() -> None:
    """A deployment that cannot look must not answer "nothing is there"."""

    def no_client() -> Any:
        raise AssertionError("no client should be built without a bucket")

    unconfigured = GcsObjectStore("", client_factory=no_client)

    for call in (
        lambda: unconfigured.read_text("goal.md"),
        lambda: unconfigured.write_text("goal.md", "x"),
        lambda: unconfigured.delete("goal.md"),
        lambda: unconfigured.list_names(""),
        lambda: unconfigured.read_bytes("a"),
        lambda: unconfigured.write_bytes("a", b"x"),
        lambda: unconfigured.list_prefixes(""),
    ):
        with pytest.raises(
            StorageNotConfiguredError, match="AQA_JOBS_GCS_BUCKET"
        ):
            call()
    assert unconfigured.uri("goal.md") == "gs:///goal.md"


def test_gcs_objects_carry_a_content_type_from_their_name() -> None:
    client = FakeGcsClient()
    store = GcsObjectStore(BUCKET, client_factory=lambda: client)

    store.write_text("agents/a.json", "{}")
    store.write_text("goal.md", "x")

    assert client.content_types == {
        (BUCKET, "agents/a.json"): "application/json",
        (BUCKET, "goal.md"): "text/markdown",
    }


def test_a_gcs_uri_names_the_bucket() -> None:
    store = GcsObjectStore(BUCKET)

    assert store.uri("goal.md") == f"gs://{BUCKET}/goal.md"
    assert store.uri("memories/") == f"gs://{BUCKET}/memories/"


# --------------------------------------------------------------------------- #
# The file store                                                               #
# --------------------------------------------------------------------------- #


def test_an_object_is_a_file_at_its_name(tmp_path: Path) -> None:
    """The layout matches the bucket, so the files can be read by hand."""
    store = FileObjectStore(tmp_path)

    store.write_text("memories/abc.json", '{"text": "t"}')

    assert (tmp_path / "memories" / "abc.json").read_text(encoding="utf-8") == (
        '{"text": "t"}'
    )
    assert store.uri("memories/abc.json") == str(
        tmp_path / "memories" / "abc.json"
    )
    assert store.uri("memories/") == str(tmp_path / "memories") + "/"


def test_the_directory_is_created_by_the_first_write(tmp_path: Path) -> None:
    root = tmp_path / ".aqua" / "job"
    store = FileObjectStore(root)

    assert store.read_text("goal.md") is None
    assert store.list_names("") == []
    assert not root.exists()

    store.write_text("goal.md", "x")

    assert root.is_dir()


@pytest.mark.parametrize(
    "name",
    [
        "",
        "/etc/passwd",
        "../outside.json",
        "memories/../../outside.json",
        "a//b",
        "./a",
    ],
)
def test_a_name_cannot_leave_the_root(tmp_path: Path, name: str) -> None:
    """A memory id reaches the store from a route."""
    store = FileObjectStore(tmp_path / "job")

    with pytest.raises(ValueError, match="not a valid object name"):
        store.write_text(name, "x")
    with pytest.raises(ValueError, match="not a valid object name"):
        store.read_text(name)
    with pytest.raises(ValueError, match="not a valid object name"):
        store.delete(name)
    assert not (tmp_path / "outside.json").exists()


def test_a_write_leaves_no_temporary_file_behind(tmp_path: Path) -> None:
    store = FileObjectStore(tmp_path)

    store.write_text("goals/v.json", "first", overwrite=False)
    store.write_text("goals/v.json", "second", overwrite=False)
    store.write_text("goals/v.json", "third")

    assert sorted(p.name for p in (tmp_path / "goals").iterdir()) == ["v.json"]


def test_create_if_absent_has_exactly_one_winner(tmp_path: Path) -> None:
    """Two saves of one goal text race to create its version; one keeps it.

    GCS guarantees this with a generation precondition, which the fake client
    does not model under threads, so only the file store is raced here.
    """
    store = FileObjectStore(tmp_path)
    barrier = threading.Barrier(8)
    results: dict[int, bool] = {}

    def create(i: int) -> None:
        barrier.wait()
        results[i] = store.write_text(
            "goals/v.json", f"writer {i}", overwrite=False
        )

    threads = [threading.Thread(target=create, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    [winner] = [i for i, won in results.items() if won]
    assert store.read_text("goals/v.json") == f"writer {winner}"


def test_hidden_files_are_not_listed(tmp_path: Path) -> None:
    """Where an interrupted write's temporary file would be."""
    store = FileObjectStore(tmp_path)
    store.write_text("memories/a.json", "{}")
    (tmp_path / "memories" / ".a.json.x.tmp").write_text("{", encoding="utf-8")

    assert store.list_names("memories/") == ["memories/a.json"]


def test_a_prefix_reads_as_absent_not_as_an_error(store: ObjectStore) -> None:
    """A directory in the file store, a prefix in GCS: neither is an object."""
    store.write_bytes("a/1/files/app/agent.py", b"x")

    assert store.read_bytes("a/1/files/app") is None
    assert store.read_text("a/1/files/app") is None


def test_dot_segments_are_object_names_like_any_other(
    store: ObjectStore,
) -> None:
    """A snapshot holds `.github/` and `.eslintrc.json`; GCS lists them."""
    store.write_bytes("a/1/files/.github/ci.yml", b"x")
    store.write_bytes("a/1/files/.eslintrc.json", b"y")

    assert store.list_names("a/1/files/") == [
        "a/1/files/.eslintrc.json",
        "a/1/files/.github/ci.yml",
    ]
