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

"""App wiring: health/status, catalog routes, the SPA, startup."""

import asyncio
import io
import os
import subprocess
import sys
import threading

import numpy as np
import pytest
from PIL import Image

from genmedia4commerce.core import catalog, gen
from genmedia4commerce.core.settings import get_settings


def test_health(client):
    assert client.get("/health").json() == {"status": "healthy"}


def test_status_reports_storage_modes_and_request_limit(client, monkeypatch):
    monkeypatch.setenv("GENMEDIA_INPUT", "gcs")
    get_settings.cache_clear()

    body = client.get("/api/status").json()

    assert body["input_mode"] == "gcs"
    assert body["output_mode"] == "inline"
    assert "collage" not in body
    assert body["request_limit_mb"] == 32


def test_catalog_search(client, catalog_file, monkeypatch):
    calls = []

    def embed(items, **kwargs):
        calls.append((items, kwargs["output_dims"]))
        vector = np.zeros((1, 256), dtype=np.float32)
        vector[0, 2] = 1.0  # the jeans
        return vector

    monkeypatch.setattr(gen, "embed", embed)
    response = client.post(
        "/api/catalog/search",
        json={"query": "jeans", "audience": ["men"], "k": 2},
    )
    items = response.json()["items"]
    assert [i["id"] for i in items] == ["pc3", "pb2"]
    assert items[0]["ref"] == "catalog:pc3"
    assert items[0]["thumb_url"] == "/api/catalog/image/pc3/0?w=256"
    assert calls == [(["jeans"], 256)]


def test_catalog_search_without_a_query_lists_the_featured(
    client, catalog_file
):
    response = client.post("/api/catalog/search", json={})
    assert [i["id"] for i in response.json()["items"]] == [
        "pa1",
        "p_glasses_4",
    ]


@pytest.mark.parametrize("k", [0, 51])
def test_catalog_search_k_out_of_range_is_422(client, k):
    response = client.post("/api/catalog/search", json={"query": "a", "k": k})

    assert response.status_code == 422


def test_catalog_image_and_thumbnail(client, catalog_file):
    response = client.get("/api/catalog/image/s_shoe/2")
    assert response.status_code == 200
    assert response.content == catalog.image("s_shoe", 2)
    assert response.headers["content-type"] == "image/webp"
    assert "max-age" in response.headers["cache-control"]
    response = client.get("/api/catalog/image/pa1/0", params={"w": 64})
    with Image.open(io.BytesIO(response.content)) as img:
        assert max(img.size) == 64


@pytest.mark.parametrize(
    "path",
    [
        "/api/catalog/image/nope/0",
        "/api/catalog/image/s_shoe/3",
        "/api/catalog/image/%2e%2e/0",
    ],
)
def test_catalog_image_not_found(client, catalog_file, path):
    assert client.get(path).status_code == 404


@pytest.mark.parametrize("query", ["w=8", "w=4096", "w=x"])
def test_catalog_image_width_is_checked(client, catalog_file, query):
    assert client.get(f"/api/catalog/image/pa1/0?{query}").status_code == 422


def test_spa_serves_index_for_routes(client):
    for path in ("/", "/try-on", "/video-try-on/step"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.text == "<html>spa</html>"


def test_spa_serves_dist_file(client):
    assert client.get("/assets/index.js").text == "js"


def test_spa_serves_nothing_outside_dist(client, dist):
    public = dist.parent / "public"
    public.mkdir()
    (public / "logo.png").write_bytes(b"public-logo")
    assert client.get("/logo.png").status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/%2e%2e/secret.txt",
        "/assets/%2e%2e/%2e%2e/secret.txt",
        "/missing.png",
        "/api/unknown",
    ],
)
def test_spa_not_found(client, dist, path):
    (dist.parent / "secret.txt").write_text("secret")

    assert client.get(path).status_code == 404


@pytest.mark.parametrize("prefix", ["/%2F", "/%2F%2F", "/assets/%2F"])
def test_spa_never_serves_an_absolute_path(client, dist, prefix):
    """``GET //etc/passwd`` (or ``/%2Fetc/passwd``) must not escape dist."""
    secret = dist.parent / "secret.txt"
    secret.write_text("secret")

    response = client.get(prefix + str(secret).lstrip("/"))

    assert response.status_code == 404
    assert "secret" not in response.text


def test_spa_absolute_path_is_not_found(client, dist):
    """``GET //etc/passwd`` reaches the route as ``/etc/passwd`` (the test
    client cannot send it: it reads ``etc`` as a host). No extension: still
    a 404, not the web app's page."""
    from genmedia4commerce.api import app as app_module

    response = asyncio.run(app_module.serve_spa("/etc/passwd"))

    assert response.status_code == 404
    assert client.get("/%2Fetc/passwd").status_code == 404


def test_spa_symlink_out_of_dist_is_not_served(client, dist):
    secret = dist.parent / "secret"
    secret.write_text("secret")
    (dist / "link").symlink_to(secret)

    response = client.get("/link")

    assert "secret" not in response.text


@pytest.mark.parametrize(
    "path", ["../secret", "../dist-private/secret", "assets/../../secret"]
)
def test_spa_raw_parent_path_is_not_found(dist, path):
    """A raw ``..`` (the client cannot send one) or a sibling folder whose
    name starts like dist never resolves to a file."""
    from genmedia4commerce.api import app as app_module

    (dist.parent / "secret").write_text("secret")
    (dist.parent / "dist-private").mkdir()
    (dist.parent / "dist-private" / "secret").write_text("secret")

    response = asyncio.run(app_module.serve_spa(path))

    assert response.status_code == 404
    assert app_module._static_file(path) is None


@pytest.mark.parametrize("path", ["/%00", "/index.html%00.js", "/assets/%00"])
def test_spa_null_byte_is_not_found(client, dist, path):
    assert client.get(path).status_code == 404


def test_startup_sizes_the_thread_pool(monkeypatch):
    from genmedia4commerce.api import app as app_module

    # Each worker waits until MAX_THREADS of them run at the same time.
    barrier = threading.Barrier(app_module.MAX_THREADS, timeout=5)

    def blocked() -> str:
        barrier.wait()
        return threading.current_thread().name

    async def main():
        server = app_module._studio_mcp_server()
        server.streamable_http_app()
        async with app_module._lifespan(server)(None):
            return await asyncio.gather(
                *(asyncio.to_thread(blocked) for _ in range(barrier.parties))
            )

    names = asyncio.run(main())
    assert app_module.MAX_THREADS == 64
    assert all(name.startswith("genmedia") for name in names)


def test_shutdown_marks_the_running_studio_jobs_interrupted(monkeypatch):
    from genmedia4commerce.api import app as app_module
    from genmedia4commerce.studio import jobs

    calls = []

    async def interrupt_local() -> int:
        calls.append("interrupted")
        return 1

    monkeypatch.setattr(jobs, "interrupt_local", interrupt_local)

    async def main():
        server = app_module._studio_mcp_server()
        server.streamable_http_app()
        async with app_module._lifespan(server)(None):
            assert calls == []  # only on shutdown

    asyncio.run(main())
    assert calls == ["interrupted"]


def test_the_adk_api_serves_the_studio_agent():
    from genmedia4commerce import agent
    from genmedia4commerce.api.app import _AgentLoader

    loader = _AgentLoader()
    assert loader.list_agents() == ["genmedia4commerce"]
    app = loader.load_agent("genmedia4commerce")
    assert app is agent.app
    assert app.name == "genmedia4commerce"
    assert app.root_agent is agent.root_agent
    assert agent.root_agent.name == "genmedia_studio"
    with pytest.raises(ValueError, match="unknown agent"):
        loader.load_agent("other")


def test_fast_api_app_reexports_app(dist):
    from genmedia4commerce import fast_api_app
    from genmedia4commerce.api import app as app_module

    assert fast_api_app.app is app_module.app
    assert fast_api_app.app.title == "GenMedia for Commerce"


_GUARD = """
import socket, sys

def _blocked(*args, **kwargs):
    raise RuntimeError("network access at import time")

class _Socket:
    def __init__(self, *args, **kwargs):
        _blocked()

socket.socket = _Socket
socket.create_connection = _blocked
socket.getaddrinfo = _blocked
import genmedia4commerce.fast_api_app
"""


def test_import_without_network_or_credentials(tmp_path):
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("GOOGLE_APPLICATION_CREDENTIALS", "LOGS_BUCKET_NAME")
    }
    env.update(PROJECT_ID="x", CLOUDSDK_CONFIG=str(tmp_path / "no-gcloud"))
    result = subprocess.run(
        [sys.executable, "-c", _GUARD],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr


_NO_SHADOWING = """
import sys
from genmedia4commerce.api.app import GENMEDIA_DIR, create_app
create_app()
assert str(GENMEDIA_DIR) not in sys.path, sys.path
from mcp.server.fastmcp import FastMCP  # the library, not genmedia4commerce/mcp
import requests
assert requests.__file__.endswith("site-packages/requests/__init__.py")
"""


def test_the_app_never_puts_our_package_on_sys_path(tmp_path):
    """ADK adds its agents folder to sys.path; ours must not be
    genmedia4commerce/, whose mcp/ and requests.py shadow the libraries."""
    env = {**os.environ, "PROJECT_ID": "x"}
    env["CLOUDSDK_CONFIG"] = str(tmp_path / "no-gcloud")
    result = subprocess.run(
        [sys.executable, "-c", _NO_SHADOWING],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
