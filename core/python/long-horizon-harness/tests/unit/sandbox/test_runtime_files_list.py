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

"""End-to-end of the container-side ``GET /files/list`` endpoint.

The shim's listing is consumed by ``SandboxEnvironment.list_directory``
which backs ``GET /lha/workspace/tree`` for the sandbox backend.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

RUNTIME_DIR = (
    Path(__file__).resolve().parents[3] / "app" / "sandbox" / "runtime"
)


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    # Nest under ``ws/`` so the conftest's autouse ``scratch/`` dir at
    # ``tmp_path`` root doesn't leak into the listings.
    p = tmp_path / "ws"
    p.mkdir()
    return p


@pytest.fixture
def client(ws: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SANDBOX_WORKSPACE", str(ws))
    monkeypatch.syspath_prepend(str(RUNTIME_DIR))
    for mod in ("server", "protocol"):
        sys.modules.pop(mod, None)
    import server  # type: ignore[import-not-found]

    with TestClient(server.app) as test_client:
        yield test_client


def test_lists_files_and_dirs(ws: Path, client: TestClient) -> None:
    (ws / "a.txt").write_text("hello")
    (ws / "b.csv").write_text("a,b\n1,2\n")
    (ws / "sub").mkdir()

    r = client.get("/files/list", params={"path": str(ws)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["truncated"] is False
    by_name = {e["name"]: e for e in body["entries"]}
    assert set(by_name) == {"a.txt", "b.csv", "sub"}
    assert by_name["a.txt"]["kind"] == "file"
    assert by_name["a.txt"]["size"] == 5
    assert by_name["sub"]["kind"] == "dir"


def test_empty_dir_returns_empty(ws: Path, client: TestClient) -> None:
    r = client.get("/files/list", params={"path": str(ws)})
    assert r.status_code == 200
    assert r.json() == {"entries": [], "truncated": False}


def test_missing_path_is_404(ws: Path, client: TestClient) -> None:
    r = client.get("/files/list", params={"path": str(ws / "nope")})
    assert r.status_code == 404


def test_file_target_is_400(ws: Path, client: TestClient) -> None:
    (ws / "f.txt").write_text("x")
    r = client.get("/files/list", params={"path": str(ws / "f.txt")})
    assert r.status_code == 400


def test_escape_is_403(client: TestClient) -> None:
    r = client.get("/files/list", params={"path": "../../etc"})
    assert r.status_code == 403


def test_truncates_at_limit(ws: Path, client: TestClient) -> None:
    for i in range(7):
        (ws / f"f{i:02d}.txt").write_text("x")
    r = client.get("/files/list", params={"path": str(ws), "limit": 3})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["entries"]) == 3
    assert body["truncated"] is True
    assert [e["name"] for e in body["entries"]] == [
        "f00.txt",
        "f01.txt",
        "f02.txt",
    ]
