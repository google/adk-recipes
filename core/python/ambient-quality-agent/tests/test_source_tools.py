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

"""Tests for `source_tools.get_source_snapshot`, the dashboard's Source card payload.

Offline: the reader is the real `SourceSnapshotReader` over the in-memory GCS
double the source-code tests already use, so the manifest this summarizes is
parsed from published bytes rather than handed in as a model.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from ambient_quality_agent.tools.orchestrator import source_tools
from ambient_quality_agent.tools.source_code import reader as reader_module
from google.api_core import exceptions as api_exceptions

from tests.conftest import ToolContext
from tests.test_source_code_reader import (
    BUCKET,
    FakeStorageClient,
    build_reader,
    publish_snapshot,
)


def snapshot_of(client: FakeStorageClient) -> dict[str, Any]:
    """Returns the source card payload for a bucket backed by `client`.

    Args:
        client: Storage client backing the reader.

    Returns:
        Snapshot status dictionary returned by get_source_snapshot.
    """
    return asyncio.run(source_tools.get_source_snapshot(ToolContext()))


@pytest.fixture(autouse=True)
def _isolated_manifest_cache() -> Iterator[None]:
    """The manifest cache is process-wide; a leak across tests would serve one
    test's revision to another."""
    reader_module.clear_manifest_cache()
    yield
    reader_module.clear_manifest_cache()


@pytest.fixture
def client() -> FakeStorageClient:
    """Three published revisions, sparse and out of order, as retention leaves them."""
    fake = FakeStorageClient()
    publish_snapshot(fake, "2", {"app/agent.py": "ancient\n"})
    publish_snapshot(fake, "8", {"app/agent.py": "older\n"})
    publish_snapshot(
        fake,
        "10",
        {
            "app/agent.py": "current\n",
            "app/tools/lookup.py": "def f():\n    pass\n",
        },
    )
    return fake


@pytest.fixture(autouse=True)
def wired(client: FakeStorageClient) -> Iterator[FakeStorageClient]:
    """Points the tool's reader at the in-memory bucket."""
    original = reader_module.reader_factory
    reader_module.reader_factory = lambda state: build_reader(client)
    try:
        yield client
    finally:
        reader_module.reader_factory = original


# --------------------------------------------------------------------------- #
# The published card                                                           #
# --------------------------------------------------------------------------- #


def test_the_newest_revision_is_the_one_summarized(
    client: FakeStorageClient,
) -> None:
    """Newest by revision number, not by listing order: the bucket hands them
    back lexically, where "8" sorts after "10"."""
    body = snapshot_of(client)

    assert body["available"] is True
    assert body["revision"] == "10"


def test_the_summary_is_the_manifest_s_own_counters(
    client: FakeStorageClient,
) -> None:
    body = snapshot_of(client)

    assert body["file_count"] == 2
    assert body["total_bytes"] == len(b"current\n") + len(
        b"def f():\n    pass\n"
    )
    assert body["created_at"] == "2026-09-11T13:43:00Z"
    assert body["agent_directory"] == "app"
    assert body["truncated_files"] == 0
    assert body["omitted_files"] == 0


def test_the_uri_points_at_the_manifest(client: FakeStorageClient) -> None:
    """A real object rather than the revision prefix, so the card's GCS link
    opens something instead of a browser page for a folder that may not list."""
    assert snapshot_of(client)["uri"] == f"gs://{BUCKET}/10/manifest.json"


def test_every_published_revision_is_counted(client: FakeStorageClient) -> None:
    """Including ones this summary skipped. The count answers "how much history
    is still in the bucket", which is the retention window an operator set."""
    assert snapshot_of(client)["revision_count"] == 3


def test_a_partial_upload_is_reported_as_incomplete(
    client: FakeStorageClient,
) -> None:
    """`truncated_files` and `omitted_files` are the two ways publishing can
    succeed and still leave code the dig phase cannot cite, so they reach the
    card rather than being rolled into "published"."""
    publish_snapshot(
        client,
        "11",
        {"app/huge.py": "x" * 64, "app/agent.py": "current\n"},
        truncated={"app/huge.py"},
    )

    body = snapshot_of(client)

    assert body["revision"] == "11"
    assert body["truncated_files"] == 1


# --------------------------------------------------------------------------- #
# The four ways it is unavailable                                              #
# --------------------------------------------------------------------------- #


def test_an_unconfigured_bucket_names_itself(client: FakeStorageClient) -> None:
    reader_module.reader_factory = lambda state: None

    body = snapshot_of(client)

    assert body == {
        "available": False,
        "reason": source_tools.SOURCE_UNCONFIGURED,
    }


def test_an_empty_bucket_names_the_bucket(client: FakeStorageClient) -> None:
    """The operator's next move differs from the unconfigured case -- deploy,
    rather than configure -- so the two do not share a sentence."""
    empty = FakeStorageClient()
    reader_module.reader_factory = lambda state: build_reader(empty)

    body = snapshot_of(client)

    assert body["available"] is False
    assert BUCKET in body["reason"]


def test_an_upload_that_never_finished_is_not_the_published_state() -> None:
    """The manifest is written last, so a prefix without one is an interrupted
    upload. Summarizing it would claim code is readable that is not."""
    partial = FakeStorageClient()
    publish_snapshot(partial, "4", {"app/agent.py": "ok\n"})
    publish_snapshot(
        partial, "5", {"app/agent.py": "half\n"}, with_manifest=False
    )
    reader_module.reader_factory = lambda state: build_reader(partial)

    body = asyncio.run(source_tools.get_source_snapshot(ToolContext()))

    assert body["revision"] == "4"
    assert body["revision_count"] == 2


def test_a_failed_read_is_unavailable_rather_than_an_error() -> None:
    """The card asks whether findings can cite code. A read that failed and a
    snapshot that was never published are both "no", and the card draws one
    amber state for both."""

    class Boom(FakeStorageClient):
        def list_blobs(
            self, bucket: str, prefix: str = "", delimiter: str | None = None
        ) -> Any:
            raise api_exceptions.Forbidden("403 on the source bucket")

    reader_module.reader_factory = lambda state: build_reader(Boom())

    body = asyncio.run(source_tools.get_source_snapshot(ToolContext()))

    assert body["available"] is False
    assert "403 on the source bucket" in body["reason"]
