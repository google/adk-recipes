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

"""What the engine accepts of a source snapshot, and where it writes it.

The writes go to a `FileObjectStore`; `tests/test_object_store.py` is what
lets that stand for the source bucket too. The routes in front of these
functions are at the end.
"""

from __future__ import annotations

import base64
import gzip
import json
from pathlib import Path
from typing import Any

import pytest
from ambient_quality_agent.core import command_routes as routes
from ambient_quality_agent.standalone.files import FileObjectStore
from ambient_quality_agent.tools.objects import store as objects_store
from ambient_quality_agent.tools.source_code import snapshot, upload
from fastapi import FastAPI
from fastapi.testclient import TestClient

AGENT = "travel_agent"


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FileObjectStore:
    bound = FileObjectStore(tmp_path / "source")
    monkeypatch.setattr(objects_store, "source_store_factory", lambda: bound)
    return bound


def _encode_file(path: str, data: bytes) -> dict[str, Any]:
    return {
        "path": path,
        "content": base64.b64encode(gzip.compress(data)).decode("ascii"),
    }


def _build_manifest(
    *entries: tuple[str, int, bool], **fields: Any
) -> dict[str, Any]:
    return {
        "engine": "projects/p/locations/l/reasoningEngines/1",
        "agent_directory": "app",
        "ignore_patterns": [".git"],
        "files": [
            {"path": path, "size": size, "truncated": truncated}
            for path, size, truncated in entries
        ],
        "omitted_files": 0,
        **fields,
    }


def test_files_land_under_the_agent_and_the_revision(
    store: FileObjectStore,
) -> None:
    reply = upload.write_files(
        agent_name=AGENT,
        revision="7",
        files=[
            _encode_file("app/agent.py", b"x = 1\n"),
            _encode_file("Dockerfile", b"\xff"),
        ],
    )

    assert reply == {"agent_name": AGENT, "revision": "7", "written": 2}
    assert store.read_bytes(f"{AGENT}/7/files/app/agent.py") == b"x = 1\n"
    assert store.read_bytes(f"{AGENT}/7/files/Dockerfile") == b"\xff"


def test_a_file_of_any_type_is_written(store: FileObjectStore) -> None:
    """The CLI chooses which files to send; the engine does not second-guess it."""
    reply = upload.write_files(
        agent_name=AGENT,
        revision="7",
        files=[_encode_file("logo.png", b"\x89")],
    )

    assert reply["written"] == 1
    assert store.read_bytes(f"{AGENT}/7/files/logo.png") == b"\x89"


@pytest.mark.parametrize(
    ("agent_name", "revision"),
    [
        ("", "7"),
        ("../x", "7"),
        ("a/b", "7"),
        ("123", "7"),
        (AGENT, ""),
        (AGENT, ".."),
    ],
)
def test_a_key_that_leaves_its_prefix_is_refused(
    store: FileObjectStore, agent_name: str, revision: str
) -> None:
    reply = upload.write_files(
        agent_name=agent_name,
        revision=revision,
        files=[_encode_file("a.py", b"a")],
    )

    assert "snapshot key" in reply["error"] or "all digits" in reply["error"]
    assert store.list_names("") == []


@pytest.mark.parametrize(
    "path", ["", "../a.py", "app/../../a.py", "a/./b.py", 3]
)
def test_a_path_that_leaves_the_files_prefix_is_refused(
    store: FileObjectStore, path: Any
) -> None:
    entry = {**_encode_file("x", b"a"), "path": path}

    reply = upload.write_files(agent_name=AGENT, revision="7", files=[entry])

    assert "error" in reply
    assert store.list_names("") == []


def test_an_agent_name_may_start_with_an_underscore(
    store: FileObjectStore,
) -> None:
    reply = upload.write_files(
        agent_name="_private", revision="7", files=[_encode_file("a.py", b"a")]
    )

    assert reply["written"] == 1


def test_a_batch_of_too_many_files_is_refused_before_decoding(
    store: FileObjectStore,
) -> None:
    files = [
        _encode_file(f"f{i}.py", b"")
        for i in range(snapshot.MAX_BATCH_FILES + 1)
    ]

    reply = upload.write_files(agent_name=AGENT, revision="7", files=files)

    assert "files in one batch" in reply["error"]
    assert store.list_names("") == []


def test_content_that_is_not_base64_gzip_is_refused(
    store: FileObjectStore,
) -> None:
    raw = {"path": "a.py", "content": base64.b64encode(b"plain").decode()}

    reply = upload.write_files(agent_name=AGENT, revision="7", files=[raw])

    assert "not base64 gzip" in reply["error"]


def test_a_file_that_inflates_past_the_cap_is_refused(
    store: FileObjectStore,
) -> None:
    """A small body must not become an unbounded write."""
    bomb = _encode_file("a.py", b"\0" * (snapshot.MAX_FILE_BYTES + 1))

    reply = upload.write_files(agent_name=AGENT, revision="7", files=[bomb])

    assert "larger than" in reply["error"]
    assert store.list_names("") == []


def test_one_bad_file_writes_none_of_the_batch(store: FileObjectStore) -> None:
    reply = upload.write_files(
        agent_name=AGENT,
        revision="7",
        files=[_encode_file("a.py", b"a"), {"path": "b.py", "content": "!!"}],
    )

    assert "error" in reply
    assert store.list_names("") == []


def test_without_a_source_store_nothing_is_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(objects_store, "source_store_factory", lambda: None)

    reply = upload.write_files(
        agent_name=AGENT, revision="7", files=[_encode_file("a.py", b"a")]
    )

    assert "AQA_SOURCE_GCS_BUCKET" in reply["error"]


def test_commit_writes_the_manifest_with_counters_of_its_own(
    store: FileObjectStore,
) -> None:
    upload.write_files(
        agent_name=AGENT,
        revision="7",
        files=[_encode_file("a.py", b"abc"), _encode_file("b.py", b"de")],
    )

    reply = upload.commit(
        agent_name=AGENT,
        revision="7",
        manifest=_build_manifest(
            ("a.py", 3, True),
            ("b.py", 2, False),
            total_bytes=999,
            revision="forged",
        ),
    )

    stored = json.loads(store.read_text(f"{AGENT}/7/manifest.json") or "")
    assert stored["revision"] == "7"
    assert stored["total_bytes"] == 5
    assert stored["truncated_files"] == 1
    assert stored["schema_version"] == snapshot.SCHEMA_VERSION
    assert stored["created_at"]
    assert snapshot.parse_manifest(json.dumps(stored)).files[0].path == "a.py"
    assert reply["uri"] == store.uri(f"{AGENT}/7/manifest.json")
    assert reply["file_count"] == 2


def test_commit_refuses_a_manifest_listing_a_file_never_uploaded(
    store: FileObjectStore,
) -> None:
    """The manifest means the snapshot is complete, so it is not written."""
    upload.write_files(
        agent_name=AGENT, revision="7", files=[_encode_file("a.py", b"a")]
    )

    reply = upload.commit(
        agent_name=AGENT,
        revision="7",
        manifest=_build_manifest(("a.py", 1, False), ("lost.py", 1, False)),
    )

    assert "lost.py" in reply["error"]
    assert store.read_text(f"{AGENT}/7/manifest.json") is None


def test_commit_refuses_a_snapshot_over_the_total_cap(
    store: FileObjectStore,
) -> None:
    upload.write_files(
        agent_name=AGENT, revision="7", files=[_encode_file("a.py", b"a")]
    )

    reply = upload.commit(
        agent_name=AGENT,
        revision="7",
        manifest=_build_manifest(
            ("a.py", snapshot.MAX_SNAPSHOT_BYTES + 1, False)
        ),
    )

    assert "larger than" in reply["error"]


def test_commit_refuses_a_path_listed_twice(store: FileObjectStore) -> None:
    upload.write_files(
        agent_name=AGENT, revision="7", files=[_encode_file("a.py", b"a")]
    )

    reply = upload.commit(
        agent_name=AGENT,
        revision="7",
        manifest=_build_manifest(("a.py", 1, False), ("/a.py", 1, False)),
    )

    assert "more than once" in reply["error"]


# --- the routes -------------------------------------------------------------- #


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app)


def test_the_upload_route_passes_the_batch_through(
    client: TestClient, store: FileObjectStore
) -> None:
    response = client.post(
        "/source/upload",
        json={
            "agent_name": AGENT,
            "revision": "7",
            "files": [_encode_file("a.py", b"a")],
        },
    )

    assert response.status_code == 200
    assert response.json()["written"] == 1


@pytest.mark.parametrize(
    ("route", "body", "detail"),
    [
        ("/source/upload", {"agent_name": AGENT, "files": []}, "revision"),
        ("/source/upload", {"revision": "7", "files": {}}, "files"),
        ("/source/commit", {"revision": "7", "manifest": []}, "manifest"),
    ],
)
def test_a_malformed_request_is_rejected(
    client: TestClient, route: str, body: dict[str, Any], detail: str
) -> None:
    response = client.post(route, json=body)

    assert response.status_code == 400
    assert detail in response.json()["detail"]


@pytest.mark.parametrize(
    "body", [{"agent_name": "../x"}, {"agent_name": AGENT, "revision": "a/b"}]
)
def test_the_source_route_refuses_a_key_outside_the_store(
    client: TestClient, store: FileObjectStore, body: dict[str, Any]
) -> None:
    response = client.post("/source", json=body)

    assert response.status_code == 400


def test_without_an_agent_the_snapshot_is_the_watched_agents(
    client: TestClient,
    store: FileObjectStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The agent the reader reads, so a snapshot sent without a name is found."""

    class _Watched:
        observed_agent_name = "watched_agent"

    monkeypatch.setattr(
        routes.effective_config, "load", lambda state: _Watched()
    )

    response = client.post(
        "/source/upload",
        json={"revision": "7", "files": [_encode_file("a.py", b"a")]},
    )

    assert response.json()["agent_name"] == "watched_agent"
    assert store.read_bytes("watched_agent/7/files/a.py") == b"a"


def test_the_source_route_reports_a_named_agents_revision(
    client: TestClient, store: FileObjectStore
) -> None:
    upload.write_files(
        agent_name=AGENT, revision="7", files=[_encode_file("a.py", b"a")]
    )
    upload.commit(
        agent_name=AGENT,
        revision="7",
        manifest=_build_manifest(("a.py", 1, False)),
    )

    published = client.post(
        "/source", json={"agent_name": AGENT, "revision": "7"}
    ).json()
    missing = client.post(
        "/source", json={"agent_name": AGENT, "revision": "8"}
    ).json()

    assert published["available"] is True
    assert published["revision"] == "7"
    assert published["uri"] == store.uri(f"{AGENT}/7/manifest.json")
    assert missing["available"] is False
