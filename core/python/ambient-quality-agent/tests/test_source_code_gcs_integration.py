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

"""Integration tests for source browsing tools against live Google Cloud Storage.

Requires GCP credentials and environment configuration:

    AQA_GCS_INTEGRATION=1 GOOGLE_CLOUD_PROJECT=<project> \
        GOOGLE_API_USE_CLIENT_CERTIFICATE=false \
        uv run --frozen pytest tests/test_source_code_gcs_integration.py

Creates a temporary bucket with test revisions and ensures complete cleanup after test execution.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

os.environ.setdefault("GOOGLE_API_USE_CLIENT_CERTIFICATE", "false")

from ambient_quality_agent.tools.objects.gcs_store import GcsObjectStore
from ambient_quality_agent.tools.source_code import (
    agent_source_browsing_tools as tools,
)
from ambient_quality_agent.tools.source_code import reader as reader_module
from ambient_quality_agent.tools.source_code import snapshot
from ambient_quality_agent.tools.source_code.reader import (
    SourceSnapshotReader,
)

from tests.conftest import REAL_GOOGLE_AUTH_DEFAULT, ToolContext

# Skip integration tests unless explicitly enabled via environment variable.
pytestmark = pytest.mark.skipif(
    os.environ.get("AQA_GCS_INTEGRATION") != "1"
    or not os.environ.get("GOOGLE_CLOUD_PROJECT"),
    reason="needs AQA_GCS_INTEGRATION=1, GOOGLE_CLOUD_PROJECT and GCP credentials",
)

REGION = "us-east1"

FILES = {
    "app/agent.py": "\n".join(f"line {n}" for n in range(1, 11)),
    "app/tools/lookup.py": (
        "def lookup_user(uid):\n    return None\n\n\ndef unused():\n    pass\n"
    ),
    "README.md": "# observed agent\n",
}
IGNORE_PATTERNS = [".git", "terraform/", "/tests/"]


def _manifest(revision: str) -> bytes:
    """Generates serialized JSON manifest bytes for an integration test revision.

    Args:
        revision: Target revision identifier.

    Returns:
        UTF-8 encoded JSON bytes of the manifest.
    """
    return json.dumps(
        {
            "schema_version": snapshot.SCHEMA_VERSION,
            "revision": revision,
            "engine": f"projects/p/locations/{REGION}/reasoningEngines/{revision}",
            "created_at": "2026-09-11T13:43:00Z",
            "agent_directory": "app",
            "ignore_patterns": IGNORE_PATTERNS,
            "files": [
                {"path": path, "size": len(body.encode()), "truncated": False}
                for path, body in FILES.items()
            ],
            "total_bytes": sum(len(b.encode()) for b in FILES.values()),
            "truncated_files": 0,
            "omitted_files": 0,
        }
    ).encode("utf-8")


def _create_storage_client() -> Any:
    """Creates a Cloud Storage client with the real Application Default Credentials.

    `conftest.py` replaces `google.auth.default` with anonymous credentials,
    which cannot create a bucket.

    Returns:
        A `google.cloud.storage.Client` for `GOOGLE_CLOUD_PROJECT`.
    """
    from google.cloud import storage

    credentials, _ = REAL_GOOGLE_AUTH_DEFAULT()
    return storage.Client(
        project=os.environ["GOOGLE_CLOUD_PROJECT"], credentials=credentials
    )


@pytest.fixture(scope="module")
def bucket() -> Iterator[Any]:
    """Creates a temporary GCS bucket with sample revisions, ensuring cleanup on teardown."""
    client = _create_storage_client()
    created = client.create_bucket(
        f"aqa-rca-itest-{uuid.uuid4().hex[:12]}", location=REGION
    )
    try:
        for revision in ("2", "10"):
            for path, body in FILES.items():
                blob = created.blob(
                    snapshot.build_file_object_name(revision, path)
                )
                blob.upload_from_string(body.encode("utf-8"))
            # Upload manifest last to signal complete snapshot publishing.
            created.blob(
                snapshot.build_manifest_object_name(revision)
            ).upload_from_string(_manifest(revision))
        yield created
    finally:
        # Deletes the bucket and all contents in one call to prevent leaking test buckets.
        created.delete(force=True)


@pytest.fixture(autouse=True)
def wired(bucket: Any) -> Iterator[None]:
    """Points reader factory and tools at the temporary integration test bucket."""
    client = _create_storage_client()
    original = reader_module.reader_factory
    store = GcsObjectStore(bucket.name, client_factory=lambda: client)
    reader_module.reader_factory = lambda state: SourceSnapshotReader(
        store=store
    )
    reader_module.clear_manifest_cache()
    try:
        yield
    finally:
        reader_module.reader_factory = original
        reader_module.clear_manifest_cache()


def run(coro: Any) -> dict[str, Any]:
    """Executes an async coroutine synchronously in an event loop.

    Args:
        coro: Coroutine to execute.

    Returns:
        Result dictionary returned by the coroutine.
    """
    return asyncio.run(coro)


def test_list_revisions_reads_both_published_snapshots() -> None:
    result = run(tools.list_revisions(ToolContext()))

    assert [entry["revision"] for entry in result["revisions"]] == ["10", "2"]
    assert result["revisions"][0]["file_count"] == len(FILES)


def test_list_source_files_filters_against_real_objects() -> None:
    context = ToolContext()

    by_glob = run(tools.list_source_files(context, glob="**/tools/*.py"))
    by_substring = run(tools.list_source_files(context, path_contains="README"))

    assert by_glob["revision"] == "10"
    assert [f["path"] for f in by_glob["files"]] == ["app/tools/lookup.py"]
    assert [f["path"] for f in by_substring["files"]] == ["README.md"]


def test_search_finds_a_definition_with_context() -> None:
    result = run(
        tools.search_source(
            ToolContext(), pattern=r"def lookup_user", context_lines=1
        )
    )

    assert result["match_count"] == 1
    assert result["matches"][0]["citation"] == "app/tools/lookup.py:1-2"


def test_search_with_no_match_reports_the_files_it_read() -> None:
    result = run(
        tools.search_source(ToolContext(), pattern="absolutely_not_here")
    )

    assert result["matches"] == []
    assert result["files_searched"] == len(FILES)


def test_read_source_file_numbers_lines_and_honours_offset_and_limit() -> None:
    context = ToolContext()

    head = run(tools.read_source_file(context, path="app/agent.py", limit=2))
    tail = run(
        tools.read_source_file(
            context, path="app/agent.py", offset=9, limit=500
        )
    )

    assert head["content"].splitlines() == ["     1: line 1", "     2: line 2"]
    assert tail["content"].splitlines() == ["     9: line 9", "    10: line 10"]
    assert tail["total_lines"] == 10


def test_an_explicit_older_revision_is_read_instead_of_the_newest() -> None:
    context = ToolContext()

    result = run(
        tools.read_source_file(
            context, path="app/agent.py", limit=1, revision="2"
        )
    )

    assert (result["revision"], result["revision_source"]) == ("2", "explicit")


def test_an_excluded_path_is_distinguished_from_an_absent_one() -> None:
    context = ToolContext()

    excluded = run(tools.read_source_file(context, path="terraform/main.tf"))
    absent = run(tools.read_source_file(context, path="app/nowhere.py"))

    assert excluded["excluded_by_pattern"] == "terraform/"
    assert "excluded_by_pattern" not in absent
    assert "no such path" in absent["reason"]
