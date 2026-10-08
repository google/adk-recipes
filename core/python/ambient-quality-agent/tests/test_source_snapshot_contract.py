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

"""A snapshot from the CLI's packer, through the real routes, to the reader.

`ambient_quality_cli.source_snapshot` packs and sends; the engine's `source/*`
routes and `tools.source_code.upload` write; `tools.source_code.reader` reads.
The CLI ships without the agent's code, so the size limits are duplicated; the
first tests pin them together. The rest send a snapshot end to end, over both
stores, so that a key, a field or an encoding that drifts on one side fails
here.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from ambient_quality_agent.core import command_routes as routes
from ambient_quality_agent.standalone.files import FileObjectStore
from ambient_quality_agent.tools.objects import store as objects_store
from ambient_quality_agent.tools.objects.gcs_store import GcsObjectStore
from ambient_quality_agent.tools.source_code import reader as source_reader
from ambient_quality_agent.tools.source_code import snapshot
from ambient_quality_cli import source_snapshot
from ambient_quality_shared.protocol import (
    SOURCE_COMMIT_ROUTE,
    SOURCE_UPLOAD_ROUTE,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from .conftest import FakeGcsClient

AGENT = "travel_agent"
REVISION = "10"
ENGINE = "projects/p/locations/us-east1/reasoningEngines/42"


def test_per_file_cap_agrees() -> None:
    assert source_snapshot.PER_FILE_LIMIT == snapshot.MAX_FILE_BYTES


def test_total_cap_agrees() -> None:
    assert source_snapshot.TOTAL_LIMIT == snapshot.MAX_SNAPSHOT_BYTES


def test_a_batch_leaves_room_under_the_body_cap() -> None:
    """The JSON around a batch's content must fit in what the cap leaves."""
    assert source_snapshot.BATCH_BYTES <= routes._MAX_BODY_BYTES - 64 * 1024
    assert source_snapshot.MAX_BODY_BYTES == routes._MAX_BODY_BYTES


def test_a_batch_holds_no_more_files_than_the_engine_takes() -> None:
    assert source_snapshot.BATCH_FILES == snapshot.MAX_BATCH_FILES


@pytest.fixture(params=["files", "gcs"])
def store(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[objects_store.ObjectStore]:
    """The source store the routes write to, bound as the provider."""
    if request.param == "files":
        bound: objects_store.ObjectStore = FileObjectStore(tmp_path / "source")
    else:
        client = FakeGcsClient()
        bound = GcsObjectStore("source-bucket", client_factory=lambda: client)
    monkeypatch.setattr(objects_store, "source_store_factory", lambda: bound)
    source_reader.clear_manifest_cache()
    yield bound
    source_reader.clear_manifest_cache()


@pytest.fixture
def post() -> Callable[[str, dict[str, Any]], dict[str, Any]]:
    """Sends a command route request to the real router."""
    app = FastAPI()
    app.include_router(routes.router)
    client = TestClient(app)

    def send(route: str, body: dict[str, Any]) -> dict[str, Any]:
        response = client.post(f"/{route}", json=body)
        assert response.status_code == 200, response.text
        return response.json()

    return send


def _create_project(root: Path, files: dict[str, bytes]) -> Path:
    for relative, data in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (root / ".gcloudignore").write_text("terraform/\n", encoding="utf-8")
    return root


def _publish(post: Any, root: Path) -> dict[str, Any]:
    return source_snapshot.upload(
        post,
        agent_name=AGENT,
        revision=REVISION,
        snapshot=source_snapshot.pack(
            root, engine=ENGINE, agent_directory="app"
        ),
        upload_route=SOURCE_UPLOAD_ROUTE,
        commit_route=SOURCE_COMMIT_ROUTE,
    )


def test_the_manifest_fields_the_cli_sends_are_reader_fields(
    tmp_path: Path,
) -> None:
    pytest.importorskip("pathspec")
    packed = source_snapshot.pack(
        _create_project(tmp_path, {"a.py": b"a"}),
        engine=ENGINE,
        agent_directory="app",
    )

    assert set(packed.manifest) <= set(snapshot.SourceManifest.model_fields)
    assert set(packed.manifest["files"][0]) == set(
        snapshot.SnapshotFileEntry.model_fields
    )


def test_a_snapshot_round_trips_through_the_routes(
    store: objects_store.ObjectStore, post: Any, tmp_path: Path
) -> None:
    """Includes a file that does not compress, so one batch nears the body
    cap, and an image, which is not sent at all."""
    pytest.importorskip("pathspec")
    noise = os.urandom(snapshot.MAX_FILE_BYTES + 10)
    root = _create_project(
        tmp_path,
        {
            "app/agent.py": b"def run():\n    return 'ok'\n",
            "app/big.txt": noise,
            "app/logo.png": b"\x89PNG",
            ".github/workflows/ci.yml": b"on: push\n",
            **{f"app/tools/t{i}.py": b"x = %d\n" % i for i in range(250)},
            "terraform/main.tf": b"ignored",
        },
    )

    summary = _publish(post, root)

    assert summary["file_count"] == 253
    assert summary["truncated_files"] == 1
    assert summary["uri"] == store.uri(f"{AGENT}/{REVISION}/manifest.json")

    reader = source_reader.SourceSnapshotReader(store=store, agent_name=AGENT)
    assert reader.list_revisions() == [REVISION]
    manifest = reader.load_manifest(REVISION)
    assert manifest is not None
    assert manifest.revision == REVISION
    assert manifest.engine == ENGINE
    assert manifest.agent_directory == "app"
    assert "terraform/" in manifest.ignore_patterns
    assert manifest.created_at
    assert manifest.get_entry("terraform/main.tf") is None
    assert manifest.get_entry("app/logo.png") is None
    assert manifest.find_excluding_pattern("app/logo.png") == "*.png"
    big = manifest.get_entry("app/big.txt")
    assert big is not None and big.truncated
    assert 0 < big.size < snapshot.MAX_FILE_BYTES
    assert (
        store.read_bytes(f"{AGENT}/{REVISION}/files/app/big.txt")
        == (noise[: big.size])
    )
    found = reader.read_file(REVISION, "app/agent.py")
    assert found is not None
    assert found.lines == ("def run():", "    return 'ok'")


def test_publishing_a_revision_again_drops_the_files_it_no_longer_has(
    store: objects_store.ObjectStore, post: Any, tmp_path: Path
) -> None:
    pytest.importorskip("pathspec")

    _publish(
        post, _create_project(tmp_path / "first", {"a.py": b"a", "b.py": b"b"})
    )
    first = source_reader.SourceSnapshotReader(store=store, agent_name=AGENT)
    assert first.load_manifest(REVISION) is not None
    _publish(post, _create_project(tmp_path / "second", {"a.py": b"a2"}))

    prefix = f"{AGENT}/{REVISION}/files/"
    assert store.list_names(prefix) == [f"{prefix}a.py"]
    assert store.read_bytes(f"{prefix}a.py") == b"a2"
    # Read back through the reader, whose manifest cache must not serve the
    # first publish.
    reader = source_reader.SourceSnapshotReader(store=store, agent_name=AGENT)
    manifest_read = reader.load_manifest(REVISION)
    assert manifest_read is not None
    assert [f.path for f in manifest_read.files] == ["a.py"]
    manifest = json.loads(
        store.read_text(f"{AGENT}/{REVISION}/manifest.json") or "{}"
    )
    assert [entry["path"] for entry in manifest["files"]] == ["a.py"]
