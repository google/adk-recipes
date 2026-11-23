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

"""The dashboard's read-only viewer mode (`app.build_read_only_dashboard_app`).

A host serves a finished investigation over a backend it provides. What these
tests pin is that the viewer reads only from that backend, refuses everything
that would write or chat, tells the browser to keep nothing, and marks its
shell for a front end with viewer mode -- and that the regular dashboard is
untouched by any of it.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from ambient_quality_shared.protocol import INVESTIGATIONS_LIST_ROUTE
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.websockets import WebSocketDisconnect

# The UI is not a package -- it is flattened to the image root -- so `app` is a
# top-level module. `pythonpath` in pyproject.toml puts the root in place.
import app as ui_app

_META = '<meta name="aqua-viewer" content="1">'
_INDEX_HTML = (
    '<!doctype html><html><head><meta charset="UTF-8"><title>v2 shell</title>'
    '</head><body><div id="root"></div></body></html>'
)
_RUN = {"investigation_id": "run-1", "status": "completed"}


class _HostBackend:
    """A host's backend: answers the investigation list, records every call."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def post_command(
        self, path: Any, payload: Any = None
    ) -> dict[str, Any]:
        self.calls.append(str(path))
        if path == INVESTIGATIONS_LIST_ROUTE:
            return {"investigations": [_RUN]}
        return {}


@pytest.fixture
def backend() -> _HostBackend:
    return _HostBackend()


@pytest.fixture
def viewer(backend: _HostBackend) -> TestClient:
    return TestClient(ui_app.build_read_only_dashboard_app(backend))


@pytest.fixture
def bundled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """The UI module reloaded over a fake bundle, restored afterwards.

    The bundle's location is fixed when the module is imported.
    """
    bundle = tmp_path / "static_v2"
    (bundle / "assets").mkdir(parents=True)
    (bundle / "assets" / "a.js").write_text("export {};", encoding="utf-8")
    (bundle / "index.html").write_text(_INDEX_HTML, encoding="utf-8")
    monkeypatch.setenv("AQA_STATIC_V2_DIR", str(bundle))
    yield importlib.reload(ui_app)
    os.environ.pop("AQA_STATIC_V2_DIR", None)
    importlib.reload(ui_app)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/investigations"),
        ("PUT", "/api/schedule"),
        ("POST", "/api/telemetry/check"),
        ("PUT", "/api/goal"),
        ("DELETE", "/api/memories/m-1"),
        ("POST", "/api/insights/ins-1/dismiss"),
        ("POST", "/a2a"),
        ("PUT", "/lha/contexts/c-1/title"),
        ("PATCH", "/api/schedule"),
        ("OPTIONS", "/api/investigations"),
    ],
)
def test_viewer_refuses_every_write(
    viewer: TestClient, backend: _HostBackend, method: str, path: str
) -> None:
    response = viewer.request(method, path, json={})

    assert response.status_code == 405
    assert response.headers["allow"] == "GET, HEAD"
    assert backend.calls == []


def test_viewer_answers_head_as_get_without_a_body(
    viewer: TestClient, backend: _HostBackend
) -> None:
    get = viewer.get("/api/investigations")
    head = viewer.head("/api/investigations")

    assert head.status_code == 200
    assert head.content == b""
    assert head.headers["content-length"] == get.headers["content-length"]
    assert head.headers["cache-control"] == "no-store"
    assert backend.calls == [str(INVESTIGATIONS_LIST_ROUTE)] * 2


def test_viewer_refuses_websockets(viewer: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect) as refused:
        with viewer.websocket_connect("/api/investigations"):
            pass

    assert refused.value.code == 1008


@pytest.mark.parametrize(
    ("path", "status"),
    [
        ("/viewer/a2a/aqa_chat/.well-known/agent-card.json", 501),
        ("/viewer/.well-known/agent-card.json", 501),
        ("/viewer/lha/contexts", 501),
        ("/viewer/api/investigations", 200),
    ],
)
def test_viewer_mounted_under_a_prefix_still_refuses_chat(
    backend: _HostBackend, path: str, status: int
) -> None:
    """Under a mount, the request's path carries the prefix."""
    host = Starlette(
        routes=[Mount("/viewer", ui_app.build_read_only_dashboard_app(backend))]
    )

    response = TestClient(host).get(path)

    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"


def test_viewer_needs_a_backend() -> None:
    with pytest.raises(TypeError):
        ui_app.build_read_only_dashboard_app(None)


@pytest.mark.parametrize(
    "path",
    [
        "/a2a/aqa_chat/.well-known/agent-card.json",
        "/.well-known/agent-card.json",
        "/lha/contexts",
        "/lha/sessions",
        "/feedback",
    ],
)
def test_viewer_refuses_chat_even_on_get(
    viewer: TestClient, backend: _HostBackend, path: str
) -> None:
    """501 is what the front end stops retrying on (`_NOT_IMPLEMENTED_501`)."""
    response = viewer.get(path)

    assert response.status_code == 501
    assert response.headers["content-type"].startswith("application/json")
    assert backend.calls == []


def test_viewer_reads_from_the_host_backend(
    viewer: TestClient, backend: _HostBackend
) -> None:
    response = viewer.get("/api/investigations")

    assert response.status_code == 200
    assert response.json()["investigations"][0]["investigation_id"] == "run-1"
    assert backend.calls == [str(INVESTIGATIONS_LIST_ROUTE)]


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/investigations"),
        ("GET", "/healthz"),
        ("GET", "/api/nope"),
        ("POST", "/api/investigations"),
        ("GET", "/lha/contexts"),
    ],
)
def test_viewer_marks_every_response_no_store(
    viewer: TestClient, method: str, path: str
) -> None:
    response = viewer.request(method, path)

    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("path", ["/", "/insights/ins-1", "/index.html"])
def test_viewer_shell_carries_the_viewer_meta_tag(
    bundled: Any, backend: _HostBackend, path: str
) -> None:
    response = TestClient(bundled.build_read_only_dashboard_app(backend)).get(
        path
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    head = response.text.split("</head>", 1)[0]
    assert _META in head
    assert "v2 shell" in response.text


def test_viewer_marks_assets_no_store(
    bundled: Any, backend: _HostBackend
) -> None:
    response = TestClient(bundled.build_read_only_dashboard_app(backend)).get(
        "/assets/a.js"
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


def test_viewer_marks_the_real_head_not_one_in_a_comment(
    bundled: Any, backend: _HostBackend
) -> None:
    Path(bundled.V2_INDEX).write_text(
        "<!doctype html><!-- <head data-x> --><html><head><title>t</title>"
        "</head><body></body></html>",
        encoding="utf-8",
    )

    response = TestClient(bundled.build_read_only_dashboard_app(backend)).get(
        "/"
    )

    assert "<!-- <head data-x> -->" in response.text
    assert f"<head>{_META}<title>" in response.text


def test_viewer_refuses_a_shell_it_cannot_mark(
    bundled: Any, backend: _HostBackend
) -> None:
    Path(bundled.V2_INDEX).write_text(
        "<!doctype html><!-- <head> --><html><body></body></html>",
        encoding="utf-8",
    )

    response = TestClient(bundled.build_read_only_dashboard_app(backend)).get(
        "/"
    )

    assert response.status_code == 500
    assert "<body>" not in response.text
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("path", ["/", "/insights/ins-1", "/index.html"])
def test_default_shell_has_no_viewer_meta_tag(bundled: Any, path: str) -> None:
    response = TestClient(bundled.app).get(path)

    assert response.status_code == 200
    assert "aqua-viewer" not in response.text


def test_default_app_is_unchanged_beside_a_viewer(
    monkeypatch: pytest.MonkeyPatch, backend: _HostBackend
) -> None:
    """A viewer in the same process leaves the regular dashboard writing to,
    and reading from, its own backend, with no caching directive added."""
    own = _HostBackend()
    monkeypatch.setattr(ui_app, "_create_default_backend_client", lambda: own)
    TestClient(ui_app.build_read_only_dashboard_app(backend)).get(
        "/api/investigations"
    )

    default = TestClient(ui_app.app)
    read = default.get("/api/investigations")
    write = default.post("/api/investigations", json={})

    assert read.status_code == 200
    assert "cache-control" not in read.headers
    assert write.status_code != 405
    assert own.calls[0] == str(INVESTIGATIONS_LIST_ROUTE)
    assert len(own.calls) == 2
    assert backend.calls == [str(INVESTIGATIONS_LIST_ROUTE)]
