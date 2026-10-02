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

"""Unit tests for the SPA catch-all route (app_utils/spa.py)."""

from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from genmedia4commerce.app_utils.spa import attach_spa_routes

INDEX_HTML = "<html>genmedia</html>"
SECRET = "must-never-be-served"


@pytest.fixture
def frontend_dir(tmp_path: Path) -> Path:
    """Create tmp_path/frontend/dist plus files that live outside of it."""
    dist = tmp_path / "frontend" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text(INDEX_HTML)
    (dist / "favicon.svg").write_text("<svg/>")
    (tmp_path / "secret.txt").write_text(SECRET)
    # A sibling whose name starts with "dist": a string-prefix containment
    # check would wrongly accept it as being inside dist/.
    (tmp_path / "frontend" / "dist-private").mkdir()
    (tmp_path / "frontend" / "dist-private" / "secret.txt").write_text(SECRET)
    return dist


@pytest.fixture
def client(frontend_dir: Path) -> TestClient:
    app = FastAPI()
    attach_spa_routes(app, frontend_dir)
    return TestClient(app)


def test_serves_file_from_dist(client: TestClient) -> None:
    response = client.get("/favicon.svg")
    assert response.status_code == 200
    assert response.text == "<svg/>"


@pytest.mark.parametrize("path", ["/", "/image-vto", "/spinning/shoes"])
def test_client_side_routes_fall_back_to_index(
    client: TestClient, path: str
) -> None:
    response = client.get(path)
    assert response.status_code == 200
    assert response.text == INDEX_HTML


def test_missing_asset_returns_404(client: TestClient) -> None:
    assert client.get("/missing.js").status_code == 404


@pytest.mark.parametrize(
    "make_url",
    [
        # Literal "//" right after the host: GET //tmp/.../secret.txt
        pytest.param(lambda p: f"http://testserver/{p}", id="double-slash"),
        # "/%2F..." is decoded to "//..." before routing.
        pytest.param(lambda p: "/" + quote(p, safe=""), id="encoded-slash"),
    ],
)
def test_rejects_absolute_path(
    client: TestClient, tmp_path: Path, make_url: Callable[[str], str]
) -> None:
    """An absolute full_path must not replace dist/ when joined to it."""
    response = client.get(make_url(str(tmp_path / "secret.txt")))
    assert response.status_code == 404
    assert SECRET not in response.text


@pytest.mark.parametrize(
    "path",
    [
        # Encoded slashes keep the dot segments away from URL normalization.
        "/..%2F..%2Fsecret.txt",
        "/..%2Fdist-private%2Fsecret.txt",
    ],
)
def test_rejects_dot_dot_traversal(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 404
    assert SECRET not in response.text


def test_null_byte_returns_404(client: TestClient) -> None:
    assert client.get("/%00").status_code == 404
