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

"""GitHub API helpers, GitHub App connect flow, per-user installs, and user-to-server tokens."""

from __future__ import annotations

import base64
import os
import re
from collections.abc import Iterator
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from app.app_utils.http_client import set_default_http_transport
from app.auth import providers
from app.auth.credential_store import set_credential_store, user_credential
from app.auth.github.api import (
    GITHUB_API_VERSION,
    github_headers,
    parse_github_remote,
)
from app.auth.github.installation_repos import (
    installation_repos,
    refresh_installation_repos,
)
from app.auth.github.provider import reset_github_caches
from app.auth.github.sandbox_token import (
    NOT_CONNECTED_NOTICE,
    sandbox_github_token,
)
from app.auth.identity import run_as
from app.auth.oauth import ensure_fresh_access_token
from app.fast_api_app import app
from app.workers.manifest import known_repos, repo_source
from app.workers.workspace import GIT_IDENTITY_EMAIL, GIT_IDENTITY_NAME, git_env
from tests.fake_integrations import integrations_transport
from tests.fakes import (
    FakeSecretManagerClient,
    InMemoryCredentialStore,
    app_client_env,
    configure_apps,
)

ALICE, BOB = "alice@example.com", "bob@example.com"
APP_URL = "https://adk-voice-agent-prod.a.run.app"
CLIENT = app_client_env("github")
INSTALLATIONS = {
    "ghu_local_1": ("octo", {9: ["octo/app", "octo/lib"]}),
    "ghu_local_2": ("octo", {9: ["octo/app", "octo/lib"]}),
    "ghu_alice": ("alice", {1: ["alice/web"]}),
    "ghu_bob": ("bob", {2: ["bob-org/api", "bob-org/web"]}),
}


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for key in ("APP_URL", "K_SERVICE", "CLOUD_RUN_JOB", "GITHUB_REDIRECT_URI"):
        monkeypatch.delenv(key, raising=False)
    reset_github_caches()
    providers._states.clear()
    yield
    reset_github_caches()
    providers._states.clear()


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/alice/app.git", ("alice", "app")),
        ("https://github.com/alice/app", ("alice", "app")),
        ("https://github.com/alice/app/", ("alice", "app")),
        ("https://x-access-token:t@github.com/alice/app.git", ("alice", "app")),
        ("http://github.com/alice/app.git", ("alice", "app")),
        ("git@github.com:alice/app.git", ("alice", "app")),
        ("ssh://git@github.com/alice/app.git", ("alice", "app")),
        ("  https://github.com/Alice/My.App.git  ", ("Alice", "My.App")),
        ("https://gitlab.com/alice/app.git", None),
        ("https://github.com/alice", None),
        ("https://github.com/alice/app/tree/main", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_github_remote(
    url: str | None, expected: tuple[str, str] | None
) -> None:
    assert parse_github_remote(url) == expected


def test_github_headers() -> None:
    assert github_headers() == {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }
    assert github_headers("t")["Authorization"] == "Bearer t"


def test_git_env_sets_the_identity_and_basic_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in (
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
    ):
        monkeypatch.delenv(key, raising=False)
    env = git_env()
    assert (
        env["GIT_AUTHOR_NAME"] == env["GIT_COMMITTER_NAME"] == GIT_IDENTITY_NAME
    )
    assert (
        env["GIT_AUTHOR_EMAIL"]
        == env["GIT_COMMITTER_EMAIL"]
        == GIT_IDENTITY_EMAIL
    )
    assert env["GIT_CONFIG_COUNT"] == "4"
    assert "Authorization:" not in " ".join(env.values())
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Me")
    authed = git_env("ghs_x")
    assert authed["GIT_AUTHOR_NAME"] == "Me"
    basic = base64.b64encode(b"x-access-token:ghs_x").decode()
    assert authed["GIT_CONFIG_COUNT"] == "5"
    assert authed["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert authed["GIT_CONFIG_VALUE_0"] == f"Authorization: Basic {basic}"


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    seen: list[httpx.Request] = []
    fallback = integrations_transport()
    refreshes = iter(range(2, 100))

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        host, path = request.url.host, request.url.path
        if host == "github.com" and path == "/login/oauth/access_token":
            form = parse_qs(request.content.decode())
            assert form["client_id"] == [CLIENT["GITHUB_CLIENT_ID"]]
            assert form["client_secret"] == [CLIENT["GITHUB_CLIENT_SECRET"]]
            if form["grant_type"] == ["authorization_code"]:
                assert form["code"] == ["user-code"]
                n = 1
            else:
                assert form["refresh_token"] == ["ghr_local_1"]
                n = next(refreshes)
            return httpx.Response(
                200,
                json={
                    "access_token": f"ghu_local_{n}",
                    "refresh_token": f"ghr_local_{n}",
                    "expires_in": 28800,
                    "refresh_token_expires_in": 15897600,
                    "token_type": "bearer",
                },
            )
        if host == "api.github.com":
            token = request.headers.get("authorization", "").removeprefix(
                "Bearer "
            )
            if token in INSTALLATIONS:
                login, installs = INSTALLATIONS[token]
                if path == "/user":
                    return httpx.Response(200, json={"login": login})
                if path == "/user/installations":
                    return httpx.Response(
                        200,
                        json={"installations": [{"id": i} for i in installs]},
                    )
                if m := re.fullmatch(
                    r"/user/installations/(\d+)/repositories", path
                ):
                    return httpx.Response(
                        200,
                        json={
                            "repositories": [
                                {
                                    "name": full.split("/")[1],
                                    "full_name": full,
                                    "clone_url": f"https://github.com/{full}.git",
                                    "default_branch": "main",
                                }
                                for full in installs.get(int(m.group(1)), [])
                            ]
                        },
                    )
        return await fallback.handle_async_request(request)

    set_default_http_transport(httpx.MockTransport(handler))
    return seen


@pytest.fixture
def app_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    configure_apps(monkeypatch, "github")


@pytest.fixture
def deployed(
    monkeypatch: pytest.MonkeyPatch, iap_user: dict[str, str]
) -> FakeSecretManagerClient:
    from app.auth.credential_store import set_secret_manager_client

    client = FakeSecretManagerClient()
    set_secret_manager_client(client, "test-project")
    monkeypatch.setenv("K_SERVICE", "adk-voice-agent")
    monkeypatch.setenv("APP_URL", APP_URL)
    return client


def _state(url: str) -> str:
    return parse_qs(urlparse(url).query)["state"][0]


def _connect(client: TestClient) -> None:
    started = client.post("/api/v1/auth/github/authorize")
    assert started.status_code == 200, started.text
    url = started.json()["authorize_url"]
    assert url.startswith(
        "https://github.com/apps/voice-agent-test/installations/new?state="
    )
    done = client.get(
        "/api/v1/auth/github/callback",
        params={
            "code": "user-code",
            "state": _state(url),
            "installation_id": "9",
            "setup_action": "install",
        },
    )
    assert '"ok": true' in done.text, done.text


def test_installing_the_app_stores_a_user_to_server_token(
    app_configured: None, github: list[httpx.Request]
) -> None:
    client = TestClient(app)
    _connect(client)

    assert user_credential("GITHUB_ACCESS_TOKEN") == "ghu_local_1"
    assert user_credential("GITHUB_REFRESH_TOKEN") == "ghr_local_1"
    assert user_credential("GITHUB_ACCOUNT") == "@octo"

    gh = next(
        a
        for a in client.get("/api/v1/config").json()["apps"]
        if a["name"] == "github"
    )
    assert gh["connected"] is True
    assert gh["account_label"] == "@octo"
    assert gh["manage_url"] == (
        "https://github.com/apps/voice-agent-test/installations/new"
    )
    names = [r["name"] for r in known_repos()]
    assert {"app", "lib"} <= set(names)
    assert repo_source("lib")[0] == "https://github.com/octo/lib.git"


@pytest.mark.asyncio
async def test_user_to_server_tokens_refresh_and_rotate(
    app_configured: None, github: list[httpx.Request]
) -> None:
    _connect(TestClient(app))

    assert (
        await ensure_fresh_access_token("github", force=True) == "ghu_local_2"
    )
    assert user_credential("GITHUB_REFRESH_TOKEN") == "ghr_local_2"


def test_change_repos_without_a_sign_in_never_exchanges_the_code(
    app_configured: None, github: list[httpx.Request]
) -> None:
    done = TestClient(app).get(
        "/api/v1/auth/github/callback",
        params={"code": "attacker-code", "setup_action": "update"},
    )
    assert "Repositories Updated" in done.text
    assert not any(r.url.path == "/login/oauth/access_token" for r in github)


def test_without_a_slug_github_is_not_offered(
    app_configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_APP_SLUG")
    client = TestClient(app)

    assert client.post("/api/v1/auth/github/authorize").status_code == 400
    names = [a["name"] for a in client.get("/api/v1/config").json()["apps"]]
    assert "github" not in names


def _manifest_repo(name: str, git_url: str) -> None:
    with open(os.environ["VOICE_AGENT_WORKSPACES_LOCAL_YAML"], "a") as f:
        f.write(f"repositories:\n  - name: {name}\n    git_url: {git_url}\n")


@pytest.mark.asyncio
async def test_the_sandbox_gets_the_users_app_token_without_a_scope_check(
    app_configured: None, github: list[httpx.Request]
) -> None:
    _manifest_repo("app", "https://github.com/octo/app.git")
    _connect(TestClient(app))
    github.clear()

    token = await sandbox_github_token("app")

    assert token.token == "ghu_local_1"
    assert token.expires_at is not None
    assert not any(r.url.host == "api.github.com" for r in github)


@pytest.mark.asyncio
async def test_users_who_havent_connected_get_no_token_and_are_told_why(
    app_configured: None, github: list[httpx.Request]
) -> None:
    _manifest_repo("app", "https://github.com/octo/app.git")

    token = await sandbox_github_token("app")

    assert (token.token, token.notice) == ("", NOT_CONNECTED_NOTICE)
    assert not any(r.url.host == "api.github.com" for r in github)


@pytest.mark.asyncio
async def test_two_users_reach_only_their_own_installation_repos(
    deployed: FakeSecretManagerClient,
    app_configured: None,
    github: list[httpx.Request],
) -> None:
    store = InMemoryCredentialStore()
    for user, token in ((ALICE, "ghu_alice"), (BOB, "ghu_bob")):
        store.set_many(user, {"GITHUB_ACCESS_TOKEN": token})
    set_credential_store(store)

    with run_as(ALICE):
        await refresh_installation_repos()
        alice = {r["name"] for r in known_repos()}
        assert (await sandbox_github_token("web")).token == "ghu_alice"
    with run_as(BOB):
        await refresh_installation_repos()
        bob = {r["name"] for r in known_repos()}
        assert repo_source("web")[0] == "https://github.com/bob-org/web.git"
        assert (await sandbox_github_token("web")).token == "ghu_bob"
    with run_as("carol@example.com"):
        assert await refresh_installation_repos() == []
        carol = {r["name"] for r in known_repos()}

    assert "web" in alice and "api" not in alice
    assert {"api", "web"} <= bob
    assert not {"api", "web"} & carol
    assert (
        installation_repos(ALICE)[0]["git_url"]
        == "https://github.com/alice/web.git"
    )
