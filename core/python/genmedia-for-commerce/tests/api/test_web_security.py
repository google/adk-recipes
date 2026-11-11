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

"""Browser-facing hardening: CORS and origin checks, the hidden ADK
session routes, ADK's answers to invalid session ids and long messages,
and the security headers."""

import importlib.util
import os
import shutil
from pathlib import Path

import pytest
from dotenv import dotenv_values
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from genmedia4commerce.api.security_headers import (
    CSP,
    HEADERS,
    SecurityHeadersMiddleware,
)
from genmedia4commerce.core.errors import ConfigError
from genmedia4commerce.studio.session_limits import MAX_MESSAGE, TOO_LONG

ROOT = Path(__file__).parents[2]
EVIL = "https://evil.example"
# A request the origin check must let through: it then fails validation.
SEARCH = ("/api/catalog/search", {"k": 0})


def _app_client(monkeypatch, origins: str | None = None) -> TestClient:
    from genmedia4commerce.api.app import create_app

    if origins is None:
        monkeypatch.delenv("ALLOW_ORIGINS", raising=False)
    else:
        monkeypatch.setenv("ALLOW_ORIGINS", origins)
    return TestClient(create_app())


def _search(client: TestClient, **headers: str):
    path, body = SEARCH
    return client.post(path, json=body, headers=headers)


# ------------------------------------------------------------------ CORS


def test_no_cross_origin_access_by_default(monkeypatch, dist):
    client = _app_client(monkeypatch)

    preflight = client.options(
        "/api/status",
        headers={"Origin": EVIL, "Access-Control-Request-Method": "POST"},
    )
    response = client.get("/api/status", headers={"Origin": EVIL})

    assert "access-control-allow-origin" not in preflight.headers
    assert "access-control-allow-origin" not in response.headers
    assert "access-control-allow-credentials" not in response.headers
    assert _search(client, Origin=EVIL).status_code == 403


def test_same_origin_posts_pass(monkeypatch, dist):
    client = _app_client(monkeypatch)

    assert _search(client, Origin="http://testserver").status_code == 422
    assert _search(client).status_code == 422  # no Origin: not a browser


def test_same_origin_posts_pass_behind_cloud_run(monkeypatch, dist):
    monkeypatch.setenv("K_SERVICE", "genmedia-for-commerce")
    client = _app_client(monkeypatch)
    origin = "https://genmedia-abc-uc.a.run.app"
    # Cloud Run ends TLS and says so in X-Forwarded-Proto.
    proxied = {
        "Host": origin.removeprefix("https://"),
        "X-Forwarded-Proto": "https",
    }

    response = _search(client, Origin=origin, **proxied)
    other = _search(client, Origin=EVIL, **proxied)

    assert response.status_code == 422
    assert other.status_code == 403


def test_listed_origins_get_cors(monkeypatch, dist):
    """``make dev``: Vite's proxy sends Host localhost:8000 and the
    browser's Origin http://localhost:3000."""
    client = _app_client(monkeypatch, "http://localhost:3000, https://a.test")
    dev = "http://localhost:3000"

    response = _search(client, Origin=dev, Host="localhost:8000")

    assert response.status_code == 422
    assert response.headers["access-control-allow-origin"] == dev
    assert _search(client, Origin=EVIL).status_code == 403


def test_the_vite_proxy_needs_its_origin_listed(monkeypatch, dist):
    """Why ``make run-backend`` sets ALLOW_ORIGINS."""
    client = _app_client(monkeypatch)

    response = _search(
        client, Origin="http://localhost:3000", Host="localhost:8000"
    )

    assert response.status_code == 403


# ------------------------------------------- gcloud run services proxy

# What make deploy sends to Cloud Run when config.env keeps the example value.
PROXY_ORIGINS = dotenv_values(ROOT / "config.env.example")["ALLOW_ORIGINS"]
RUN_APP = "genmedia-abc-uc.a.run.app"
# The proxy forwards to the run.app URL: Cloud Run's front end sees that
# Host, ends TLS and says so in X-Forwarded-Proto.
THROUGH_PROXY = {"Host": RUN_APP, "X-Forwarded-Proto": "https"}


def _cloud_run_client(monkeypatch, origins: str | None) -> TestClient:
    monkeypatch.setenv("K_SERVICE", "genmedia-for-commerce")
    return _app_client(monkeypatch, origins)


def _status_codes(client: TestClient, origin: str | None) -> list[int]:
    """The page, an asset, /health and a POST that fails validation."""
    headers = THROUGH_PROXY | ({"Origin": origin} if origin else {})
    gets = [
        client.get(path, headers=headers).status_code
        for path in ("/", "/assets/index.js", "/health")
    ]
    return [*gets, _search(client, **headers).status_code]


def test_the_example_config_lists_the_proxy_origins():
    assert PROXY_ORIGINS.split(",") == [
        "http://localhost:8080",
        "http://127.0.0.1:8080",
        r"regex:^https://8080-[a-z0-9.-]+\.cloudshell\.dev$",
    ]


def test_regex_origins_reach_adk_whole(monkeypatch):
    from genmedia4commerce.api.app import _allowed_origins

    monkeypatch.setenv("ALLOW_ORIGINS", PROXY_ORIGINS)

    assert _allowed_origins()[-1] == (
        r"regex:^https://8080-[a-z0-9.-]+\.cloudshell\.dev$"
    )


def test_an_invalid_regex_origin_refuses_to_start(monkeypatch, dist):
    with pytest.raises(ConfigError, match="not a valid regular expression"):
        _app_client(monkeypatch, "http://localhost:8080,regex:^https://(")


@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost:8080",  # the browser behind the proxy
        "http://127.0.0.1:8080",
        f"https://{RUN_APP}",  # same origin (IAP, a direct call)
        None,  # not a browser
    ],
)
def test_the_proxy_and_same_origin_reach_the_app(monkeypatch, dist, origin):
    client = _cloud_run_client(monkeypatch, PROXY_ORIGINS)

    assert _status_codes(client, origin) == [200, 200, 200, 422]


# Cloud Shell's Web Preview: https://$PORT-$WEB_HOST, where WEB_HOST is like
# cs-<id>-default.cs-<region>-<x>.cloudshell.dev.
CLOUD_SHELL = "https://8080-cs-123-default.cs-us-east1-abcd.cloudshell.dev"


def test_cloud_shell_web_preview_reaches_the_app(monkeypatch, dist):
    client = _cloud_run_client(monkeypatch, PROXY_ORIGINS)

    assert _status_codes(client, CLOUD_SHELL) == [200, 200, 200, 422]
    response = _search(client, Origin=CLOUD_SHELL, **THROUGH_PROXY)
    assert response.headers["access-control-allow-origin"] == CLOUD_SHELL


@pytest.mark.parametrize(
    "origin",
    [
        EVIL,
        "http://localhost:9999",
        # Another port of the Cloud Shell, or the right one served elsewhere
        CLOUD_SHELL.replace("8080-", "9000-"),
        CLOUD_SHELL.replace("https:", "http:"),
        f"{CLOUD_SHELL}:8443",
        "https://8080-cs-x.cloudshell.dev.evil.com",
        "https://8080-cs-x.cloudshell.devevil.com",
        "https://evil.com/https://8080-cs-x.cloudshell.dev",
    ],
)
def test_other_origins_stay_refused_on_cloud_run(monkeypatch, dist, origin):
    client = _cloud_run_client(monkeypatch, PROXY_ORIGINS)

    assert _status_codes(client, origin) == [403, 403, 403, 403]


def test_the_proxy_needs_its_origins_listed(monkeypatch, dist):
    """Why config.env.example sets ALLOW_ORIGINS: same origin only, the
    browser's localhost Origin does not match the run.app Host."""
    client = _cloud_run_client(monkeypatch, None)

    assert _status_codes(client, "http://localhost:8080") == [403] * 4
    assert _status_codes(client, f"https://{RUN_APP}") == [200, 200, 200, 422]


def _load_package_init(tmp_path: Path, config: str) -> None:
    """Run the package's ``__init__`` next to a ``config.env``."""
    package = tmp_path / "pkg"
    package.mkdir()
    shutil.copy(ROOT / "genmedia4commerce" / "__init__.py", package)
    (tmp_path / "config.env").write_text(config)
    spec = importlib.util.spec_from_file_location(
        "_init_copy", package / "__init__.py"
    )
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(importlib.util.module_from_spec(spec))


def test_make_run_backend_origins_win_over_config_env(monkeypatch, tmp_path):
    """``make run-backend`` sets ALLOW_ORIGINS in the environment; the
    package loads config.env without override, so the Vite origins stay."""
    vite = "http://localhost:3000,http://127.0.0.1:3000"
    monkeypatch.setenv("ALLOW_ORIGINS", vite)

    _load_package_init(tmp_path, f"ALLOW_ORIGINS={PROXY_ORIGINS}\n")

    assert os.environ["ALLOW_ORIGINS"] == vite


def test_config_env_origins_apply_when_the_environment_has_none(
    monkeypatch, tmp_path
):
    # setenv first, so that teardown also removes what load_dotenv sets.
    monkeypatch.setenv("ALLOW_ORIGINS", "")
    monkeypatch.delenv("ALLOW_ORIGINS")

    _load_package_init(tmp_path, f"ALLOW_ORIGINS={PROXY_ORIGINS}\n")

    assert os.environ["ALLOW_ORIGINS"] == PROXY_ORIGINS


@pytest.mark.parametrize("origins", ["*", "https://a.test,*", " * "])
def test_a_wildcard_origin_refuses_to_start(monkeypatch, dist, origins):
    with pytest.raises(ConfigError, match="not '\\*'"):
        _app_client(monkeypatch, origins)


# --------------------------------------------------------- session routes

SESSIONS = "/apps/genmedia4commerce/users/studio/sessions"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", SESSIONS),
        ("GET", f"{SESSIONS}/"),
        ("HEAD", SESSIONS),
        ("DELETE", f"{SESSIONS}/abc"),
        ("DELETE", f"{SESSIONS}/abc/"),
    ],
)
def test_session_list_and_delete_are_not_found(client, method, path):
    response = client.request(method, path)

    assert response.status_code == 404


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", f"{SESSIONS}/abc"),
        ("POST", SESSIONS),
        ("POST", f"{SESSIONS}/abc"),
        ("PATCH", f"{SESSIONS}/abc"),
        ("GET", f"{SESSIONS}/abc/artifacts"),
        ("DELETE", f"{SESSIONS}/abc/artifacts/a.png"),
    ],
)
def test_other_session_routes_stay(method, path):
    from genmedia4commerce.api.app import _is_hidden

    assert not _is_hidden(method, path)


def test_an_invalid_session_id_is_a_client_error(client, storage):
    created = client.post(f"{SESSIONS}/a.b", json={})
    assert created.status_code == 400
    assert "studio_id" in created.json()["detail"]
    assert client.get(f"{SESSIONS}/a.b").status_code == 404
    assert not storage.objects


def _run(text: str, **extra) -> dict:
    return {
        "app_name": "genmedia4commerce",
        "user_id": "studio",
        "session_id": "a.b",
        "new_message": {"role": "user", "parts": [{"text": text}]},
        **extra,
    }


@pytest.mark.parametrize("path", ["/run", "/run_sse"])
def test_the_adk_api_refuses_a_too_long_message(client, path):
    too_long = _run("x" * (MAX_MESSAGE - 9), streaming=False)
    too_long["new_message"]["parts"].append({"text": "0123456789"})
    response = client.post(path, json=too_long)

    assert response.status_code == 400
    assert response.json() == {"detail": TOO_LONG}


def test_the_adk_api_runs_a_message_within_the_limit(client, storage):
    response = client.post("/run", json=_run("x" * MAX_MESSAGE))

    assert response.status_code == 404  # past the limit, to ADK: no session
    assert not storage.objects


# ------------------------------------------------------- security headers


def test_responses_carry_the_security_headers(client):
    for path in ("/health", "/", "/assets/index.js", "/api/unknown"):
        response = client.get(path)
        for name, value in HEADERS.items():
            assert response.headers[name] == value, (path, name)


def test_the_policy_is_enforced_and_allows_the_app():
    assert set(HEADERS) >= {"Content-Security-Policy", "X-Frame-Options"}
    assert "Content-Security-Policy-Report-Only" not in HEADERS
    # Each directive's sources, compared whole.
    directives = {
        name: sources.split()
        for name, sources in (d.split(" ", 1) for d in CSP.split("; "))
    }
    assert directives["script-src"] == ["'self'"]
    assert directives["style-src"] == [
        "'self'",
        "'unsafe-inline'",
        "https://fonts.googleapis.com",
    ]
    assert directives["font-src"] == ["'self'", "https://fonts.gstatic.com"]
    assert directives["frame-ancestors"] == ["'none'"]
    assert directives["connect-src"] == ["'self'"]
    assert "'unsafe-eval'" not in CSP.split()


def _tiny_client() -> TestClient:
    async def ok(_request):
        return PlainTextResponse(
            "ok", headers={"X-Frame-Options": "SAMEORIGIN"}
        )

    routes = [Route(p, ok) for p in ("/page", "/mcp", "/mcp/x", "/mcpx")]
    app = Starlette(routes=routes)
    app.add_middleware(SecurityHeadersMiddleware)
    return TestClient(app)


def test_mcp_responses_are_left_alone():
    client = _tiny_client()

    for path in ("/mcp", "/mcp/x"):
        response = client.get(path)
        assert "content-security-policy" not in response.headers
        assert "x-content-type-options" not in response.headers
    assert "x-content-type-options" in client.get("/mcpx").headers


def test_a_header_the_route_set_is_kept():
    response = _tiny_client().get("/page")

    assert response.headers["x-frame-options"] == "SAMEORIGIN"
    assert response.headers["x-content-type-options"] == "nosniff"
