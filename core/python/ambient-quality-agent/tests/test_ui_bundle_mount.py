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

"""How the dashboard serves its front end, and what it does with no bundle.

The bundle is built into the image by the Node stage in ``ui/Dockerfile``, so
these tests fake it with a directory of the same shape rather than requiring
``npm run build`` to have run. What they pin is the routing, which is where the
mistakes are:

  * a missing bundle says how to build one and leaves the API answering, rather
    than failing at import or 404ing a page nobody can diagnose;
  * the catch-all does not shadow ``/api/*``, which it would if it were declared
    anywhere but last;
  * ``/v2/...`` still resolves, because permalinks built by the front end use it.
"""

from __future__ import annotations

import asyncio
import importlib
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

# The UI is not a package -- it is flattened to the image root -- so `app` is a
# top-level module. `pythonpath` in pyproject.toml puts the root in place.
import app as ui_app

_INDEX_HTML = "<!doctype html><title>v2 shell</title><div id=root></div>"


def _build_bundle(root: Path) -> Path:
    """Creates a directory structure matching `npm run build` output.

    Args:
        root: Base directory where static_v2 will be created.

    Returns:
        Path to the created static bundle directory.
    """
    bundle = root / "static_v2"
    (bundle / "assets").mkdir(parents=True)
    (bundle / "index.html").write_text(_INDEX_HTML, encoding="utf-8")
    (bundle / "assets" / "index-abc123.js").write_text(
        "console.log(1)", encoding="utf-8"
    )
    (bundle / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    (bundle / "apple-touch-icon.png").write_bytes(b"\x00")
    return bundle


def _reload(monkeypatch: pytest.MonkeyPatch, **env: str) -> Any:
    """Re-imports the UI module under a given environment.

    Routing is configured at module import time based on bundle availability,
    so reloading is required to test both states.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        **env: Environment variables to set before reloading.

    Returns:
        The reloaded UI application module.
    """
    for key in ("AQA_STATIC_V2_DIR",):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return importlib.reload(ui_app)


@pytest.fixture(autouse=True)
def _restore_module() -> Iterator[None]:
    """Put the module back as the rest of the suite expects to find it."""
    yield
    for key in ("AQA_STATIC_V2_DIR",):
        os.environ.pop(key, None)
    importlib.reload(ui_app)


def _get(module: Any, path: str, **kwargs: Any) -> httpx.Response:
    async def run() -> httpx.Response:
        transport = httpx.ASGITransport(app=module.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            return await client.get(path, **kwargs)

    return asyncio.run(run())


# --------------------------------------------------------------------------- #
# With a bundle, and without one                                               #
# --------------------------------------------------------------------------- #


def test_a_bundle_is_served(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Having the bundle is the whole condition; there is no flag."""
    bundle = _build_bundle(tmp_path)
    module = _reload(monkeypatch, AQA_STATIC_V2_DIR=str(bundle))

    assert module.BUNDLE_AVAILABLE is True
    assert "v2 shell" in _get(module, "/").text


def test_no_bundle_says_how_to_build_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Returns 503 with build instructions when bundle is missing.

    A 404 would read as a routing bug, and raising at import would take the
    API down with the page and break checkouts that have not built the front end.
    """
    module = _reload(monkeypatch, AQA_STATIC_V2_DIR=str(tmp_path / "absent"))

    assert module.BUNDLE_AVAILABLE is False
    response = _get(module, "/")
    assert response.status_code == 503
    assert "npm --prefix ui/web run build" in response.text


def test_no_bundle_leaves_the_api_answering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The page is missing, not the service. `/healthz` is what Cloud Run
    probes, and answering it is why a missing bundle is not fatal."""
    module = _reload(monkeypatch, AQA_STATIC_V2_DIR=str(tmp_path / "absent"))

    assert _get(module, "/healthz").json() == {"status": "ok"}
    assert (
        _get(module, "/api/insights")
        .headers["content-type"]
        .startswith("application/json")
    )


def test_no_bundle_says_so_in_the_log_too(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Whoever hits this in a container reads the log, not the page.

    The bundle is gitignored, so a checkout has one only if somebody ran the
    build -- which makes this the first thing anyone hits.
    """
    missing = tmp_path / "absent"
    with caplog.at_level("WARNING"):
        _reload(monkeypatch, AQA_STATIC_V2_DIR=str(missing))

    assert "No built front end" in caplog.text
    assert str(missing) in caplog.text
    # The remedy, not just the diagnosis.
    assert "npm --prefix ui/web run build" in caplog.text


# --------------------------------------------------------------------------- #
# Serving the bundle                                                           #
# --------------------------------------------------------------------------- #


@pytest.fixture
def served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    bundle = _build_bundle(tmp_path)
    return _reload(monkeypatch, AQA_STATIC_V2_DIR=str(bundle))


def test_root_serves_the_shell(served: Any) -> None:
    assert served.BUNDLE_AVAILABLE is True
    response = _get(served, "/")
    assert response.status_code == 200
    assert "v2 shell" in response.text


def test_hashed_assets_are_served(served: Any) -> None:
    response = _get(served, "/assets/index-abc123.js")
    assert response.status_code == 200
    assert response.text == "console.log(1)"


@pytest.mark.parametrize("path", ["/favicon.svg", "/apple-touch-icon.png"])
def test_other_bundle_files_are_served(served: Any, path: str) -> None:
    """Not just /assets: Vite copies public/, with the favicons, to the root."""
    assert _get(served, path).status_code == 200


@pytest.mark.parametrize(
    "path",
    ["/insights", "/insights/ins-1", "/investigations/run-1", "/c", "/config"],
)
def test_client_routes_fall_through_to_the_shell(
    served: Any, path: str
) -> None:
    """The router owns paths the server has never heard of."""
    response = _get(served, path)
    assert response.status_code == 200
    assert "v2 shell" in response.text


# --------------------------------------------------------------------------- #
# What the catch-all must not swallow                                          #
# --------------------------------------------------------------------------- #


def test_api_routes_are_not_shadowed(served: Any) -> None:
    """The regression this file exists for.

    Registered before the API routes, the catch-all matches `/api/insights`
    first and answers every dashboard call with the HTML shell.
    """
    response = _get(served, "/api/insights")
    assert "v2 shell" not in response.text
    # No agent is configured here, so the handler answers with its own error
    # payload -- but it answers, in JSON, which is what proves the catch-all did
    # not take the route. A page of HTML would be the failure.
    assert response.headers["content-type"].startswith("application/json")
    assert "error" in response.json()


def test_unknown_api_path_is_a_404_not_the_shell(served: Any) -> None:
    """A mistyped endpoint answering 200 with HTML reads to `fetch` as success."""
    response = _get(served, "/api/no-such-endpoint")
    assert response.status_code == 404
    assert "v2 shell" not in response.text


@pytest.mark.parametrize(
    "path",
    [
        "/lha/contexts",
        "/lha/sessions",
        "/a2a",
        "/.well-known/agent-card.json",
        "/feedback",
    ],
)
def test_data_prefixes_never_answer_with_the_shell(
    served: Any, path: str
) -> None:
    """Found by running the thing: these fell through and answered HTML.

    The invariant, whether or not a real route claims the path: a request the
    front end makes for *data* gets data back. 200 plus a `<!doctype html>`
    reads to `fetch` as success and then fails somewhere unrelated, which is a
    genuinely horrible thing to debug.

    Parametrized over both kinds on purpose -- `/a2a` and the agent card are
    proxied for real, the other three are not yet -- because the property has to
    survive a route being added, which is precisely when it would be lost.
    """
    response = _get(served, path)
    assert "v2 shell" not in response.text
    assert response.headers["content-type"].startswith("application/json")


@pytest.mark.parametrize(
    "path", ["/lha", "/feedback/anything", "/.well-known/openid-configuration"]
)
def test_prefixes_we_do_not_serve_yet_are_501(served: Any, path: str) -> None:
    """501 specifically, because that is the contract the client is written to.

    It short-circuits on a 501 and retries against anything else, so a 404 here
    would leave the conversation list retrying a route that is never coming.

    These are the gaps the real routes leave: `/lha/*` and `/feedback` are
    answered above the catch-all (`test_ui_lha.py` pins those), but bare
    `/lha`, anything under `/feedback/` and the rest of `/.well-known/` still
    fall through to here.
    """
    response = _get(served, path)
    assert response.status_code == 501
    assert "not available in AQuA" in response.json()["detail"]


def test_healthz_still_answers(served: Any) -> None:
    response = _get(served, "/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_the_old_dashboard_is_gone(served: Any) -> None:
    """The `/static/*` path is unmapped and falls through to the shell.

    It is an unclaimed path, so the catch-all answers it with
    the shell and the router renders its not-found page -- the
    assertion verifies that no JavaScript comes back.
    """
    response = _get(served, "/static/app.js")
    assert "console.log" not in response.text
    assert "v2 shell" in response.text


# --------------------------------------------------------------------------- #
# /v2 permalinks                                                               #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ("/v2", "/"),
        ("/v2/", "/"),
        ("/v2/insights/ins-1", "/insights/ins-1"),
        ("/v2/investigations/run-1", "/investigations/run-1"),
    ],
)
def test_v2_permanently_redirects_to_the_root(
    served: Any, requested: str, expected: str
) -> None:
    """`insight-detail.tsx` builds `${origin}/v2/insights/<id>` permalinks."""
    response = _get(served, requested)
    assert response.status_code == 308
    assert response.headers["location"] == expected


def test_v2_redirect_keeps_the_query_string(served: Any) -> None:
    response = _get(served, "/v2/c?id=conv-1")
    assert response.status_code == 308
    assert response.headers["location"] == "/c?id=conv-1"
