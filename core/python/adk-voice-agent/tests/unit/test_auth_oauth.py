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

"""OAuth sign-in, provider hooks, incremental scope grants, and token revocation."""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.apps import app_info
from app.app_utils.http_client import set_default_http_transport
from app.auth import disconnect_provider, oauth
from app.auth.credential_store import set_credential_store
from app.auth.github.provider import GitHubHooks
from app.auth.identity import acting_user_id_or_none, dev_user_id, run_as
from app.auth.provider_hooks import ProviderHooks, provider_hooks
from app.auth.providers import (
    WORKSPACE_PROVIDER,
    auth_path,
    get_redirect_uri,
    load_integrations_config,
    require_provider,
    scopes_missing_from,
    url_origin,
)
from app.fast_api_app import app
from tests.fake_integrations import (
    google_token_transport,
    integrations_transport,
)
from tests.fakes import InMemoryCredentialStore, configure_apps

client = TestClient(app)
APP_ORIGIN = "http://localhost:3000"
S = "https://www.googleapis.com/auth/"
ALICE = "alice@example.com"
GRANTED = "GOOGLE_WORKSPACE_GRANTED_SCOPES"


def _server_scopes() -> list[str]:
    return require_provider("google_workspace").scopes


def _older_scopes() -> list[str]:
    return [s for s in _server_scopes() if s != f"{S}drive.file"]


@pytest.fixture(autouse=True)
def workspace_env(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[list[httpx.Request]]:
    for key in (
        "APP_URL",
        "K_SERVICE",
        "CLOUD_RUN_JOB",
        "GOOGLE_WORKSPACE_REDIRECT_URI",
    ):
        monkeypatch.delenv(key, raising=False)
    configure_apps(monkeypatch, "google_workspace")

    token_requests: list[httpx.Request] = []
    fallback = integrations_transport()

    async def handler(request: httpx.Request) -> httpx.Response:
        if (
            request.url.host == "oauth2.googleapis.com"
            and request.url.path == "/token"
        ):
            token_requests.append(request)
            return httpx.Response(
                200,
                json={
                    "access_token": "gw-access",
                    "refresh_token": "gw-refresh",
                    "expires_in": 3600,
                },
            )
        return await fallback.handle_async_request(request)

    set_default_http_transport(httpx.MockTransport(handler))
    yield token_requests
    with run_as(dev_user_id()):
        disconnect_provider("google_workspace")


def _start_sign_in() -> dict[str, str]:
    res = client.post(
        "/api/v1/auth/google_workspace/authorize",
        headers={"Host": "localhost:8000", "Origin": APP_ORIGIN},
    )
    assert res.status_code == 200, res.text
    query = parse_qs(urlparse(res.json()["authorize_url"]).query)
    return {key: values[0] for key, values in query.items()}


def _callback(**params: str) -> str:
    res = client.get("/api/v1/auth/google_workspace/callback", params=params)
    assert res.status_code == 200
    return res.text


def test_valid_state_exchanges_the_code_with_its_pkce_verifier(
    workspace_env: list[httpx.Request],
) -> None:
    params = _start_sign_in()

    page = _callback(code="auth-code", state=params["state"])

    assert "Connected to" in page
    (token_request,) = workspace_env
    grant = parse_qs(token_request.content.decode())
    assert grant["code"] == ["auth-code"]
    assert grant["redirect_uri"] == [params["redirect_uri"]]
    digest = hashlib.sha256(grant["code_verifier"][0].encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    assert challenge == params["code_challenge"]
    assert f'"{APP_ORIGIN}");' in page
    assert '"*"' not in page


@pytest.mark.parametrize("state", [None, "forged-state"])
def test_missing_or_unknown_state_is_rejected(
    workspace_env: list[httpx.Request], state: str | None
) -> None:
    _start_sign_in()

    page = _callback(code="auth-code", **({"state": state} if state else {}))

    assert "Authentication Failed" in page
    assert "unknown or has expired" in page
    assert "postMessage" not in page
    assert workspace_env == []


def test_state_is_single_use(workspace_env: list[httpx.Request]) -> None:
    state = _start_sign_in()["state"]
    assert "Connected to" in _callback(code="auth-code", state=state)

    assert "unknown or has expired" in _callback(code="auth-code", state=state)
    assert len(workspace_env) == 1


def test_expired_state_is_rejected(
    workspace_env: list[httpx.Request], monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _start_sign_in()["state"]
    monkeypatch.setattr(oauth, "PENDING_TTL_S", -1.0)

    assert "unknown or has expired" in _callback(code="auth-code", state=state)
    assert workspace_env == []


def test_state_for_another_provider_is_rejected(
    workspace_env: list[httpx.Request],
) -> None:
    state = _start_sign_in()["state"]

    res = client.get(
        "/api/v1/auth/github/callback", params={"code": "c", "state": state}
    )

    assert "unknown or has expired" in res.text
    assert workspace_env == []


def test_untrusted_app_origin_is_rejected() -> None:
    res = client.post(
        "/api/v1/auth/google_workspace/authorize",
        headers={"Host": "localhost:8000", "Origin": "https://evil.example"},
    )
    assert res.status_code == 403
    with pytest.raises(ValueError, match="Untrusted app origin"):
        oauth.trusted_opener_origin("https://evil.example", None)
    assert oauth.trusted_opener_origin(APP_ORIGIN, None) == APP_ORIGIN


def test_authorize_is_post_only() -> None:
    res = client.get(
        "/api/v1/auth/google_workspace/authorize",
        params={"client_id": "x", "client_secret": "leaked"},
    )
    assert res.status_code in (404, 405)


def test_remote_redirect_uri_requires_app_url(
    monkeypatch: pytest.MonkeyPatch, iap_user: dict[str, str]
) -> None:
    monkeypatch.setenv("K_SERVICE", "adk-voice-agent")
    headers = {
        "Host": "localhost:8000",
        "X-Forwarded-Host": "evil.example",
        **iap_user,
    }

    res = client.post(
        "/api/v1/auth/google_workspace/authorize", headers=headers
    )
    assert res.status_code == 400
    assert "APP_URL" in res.json()["detail"]

    monkeypatch.setenv("APP_URL", "https://adk-voice-agent-prod.a.run.app")
    res = client.post(
        "/api/v1/auth/google_workspace/authorize", headers=headers
    )
    assert res.status_code == 200
    query = parse_qs(urlparse(res.json()["authorize_url"]).query)
    assert query["redirect_uri"] == [
        "https://adk-voice-agent-prod.a.run.app/api/v1/auth/google_workspace/callback"
    ]


def test_authorize_needs_no_body_and_defaults_to_this_server() -> None:
    res = client.post(
        "/api/v1/auth/google_workspace/authorize",
        headers={"Host": "localhost:8000"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["callback_origin"] == "http://localhost:8000"
    query = parse_qs(urlparse(res.json()["authorize_url"]).query)
    assert query["client_id"] == ["google_workspace-client-id"]


def test_authorize_refuses_an_app_this_deployment_did_not_register(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET")

    res = client.post(
        "/api/v1/auth/google_workspace/authorize",
        headers={"Host": "localhost:8000"},
    )
    assert res.status_code == 400
    unknown = client.post(
        "/api/v1/auth/slack/authorize", headers={"Host": "localhost:8000"}
    )
    assert unknown.status_code == 400


def test_github_connect_installs_the_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_apps(monkeypatch, "github")

    res = client.post(
        "/api/v1/auth/github/authorize", headers={"Host": "localhost:8000"}
    )

    assert res.status_code == 200, res.text
    url = urlparse(res.json()["authorize_url"])
    assert (url.netloc, url.path) == (
        "github.com",
        "/apps/voice-agent-test/installations/new",
    )
    assert parse_qs(url.query)["state"]
    assert res.json()["callback_origin"] == "http://localhost:8000"


def test_url_origin() -> None:
    assert (
        url_origin("https://a.example:8443/x/y?z=1") == "https://a.example:8443"
    )
    assert url_origin("http://127.0.0.1:8000") == "http://127.0.0.1:8000"


def test_acting_user_id_or_none(monkeypatch: pytest.MonkeyPatch) -> None:
    with run_as("alice@example.com"):
        assert acting_user_id_or_none() == "alice@example.com"
    monkeypatch.setenv("VOICE_AGENT_AUTH_MODE", "iap")
    monkeypatch.setenv(
        "VOICE_AGENT_IAP_AUDIENCE", "/projects/1/global/backendServices/2"
    )
    assert acting_user_id_or_none() is None


def test_parse_iso8601() -> None:
    from app.app_utils.iso_time import parse_iso8601

    parsed = parse_iso8601("2026-10-10T12:00:00Z")
    assert parsed is not None and parsed.timestamp() == 1791633600
    assert parse_iso8601("") is None
    assert parse_iso8601("not a time") is None


def test_auth_path() -> None:
    assert auth_path("github", "callback") == "/api/v1/auth/github/callback"
    assert auth_path() == "/api/v1/auth"


def test_hooks_are_registered_per_provider() -> None:
    assert isinstance(provider_hooks("github"), GitHubHooks)
    plain = provider_hooks(WORKSPACE_PROVIDER)
    assert type(plain) is ProviderHooks
    assert plain.configured() is True
    assert plain.manage_url() is None
    assert (
        plain.authorize_url(
            redirect_uri="https://x/cb", opener_origin="https://x"
        )
        is None
    )


def test_provider_quirks_come_from_yaml(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("APP_URL", raising=False)
    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.delenv("CLOUD_RUN_JOB", raising=False)
    monkeypatch.delenv("GOOGLE_WORKSPACE_REDIRECT_URI", raising=False)
    monkeypatch.delenv("GITHUB_REDIRECT_URI", raising=False)
    assert require_provider(WORKSPACE_PROVIDER).revoke_url
    assert require_provider("github").account_label_env == "GITHUB_ACCOUNT"
    assert load_integrations_config() is load_integrations_config()

    request = SimpleNamespace(
        headers={"host": "localhost:8000"},
        url=SimpleNamespace(scheme="http", netloc="localhost:8000"),
    )

    assert (
        get_redirect_uri(WORKSPACE_PROVIDER, request)  # type: ignore[arg-type]
        == "http://localhost:8000/api/v1/auth/google_workspace/callback"
    )
    assert (
        get_redirect_uri("github", request)  # type: ignore[arg-type]
        == "http://localhost:8000/api/v1/auth/github/callback"
    )


@pytest.fixture
def creds(monkeypatch: pytest.MonkeyPatch) -> InMemoryCredentialStore:
    for key in ("APP_URL", "K_SERVICE", "CLOUD_RUN_JOB"):
        monkeypatch.delenv(key, raising=False)
    configure_apps(monkeypatch)
    store = InMemoryCredentialStore()
    set_credential_store(store)
    return store


def _connect(
    store: InMemoryCredentialStore, granted: list[str] | None = None
) -> None:
    store.users[ALICE] = {
        "GOOGLE_WORKSPACE_ACCESS_TOKEN": "alice-access-0",
        "GOOGLE_WORKSPACE_REFRESH_TOKEN": "alice-refresh",
        **({GRANTED: " ".join(granted)} if granted is not None else {}),
    }


def _summary(name: str = "google_workspace") -> dict[str, Any]:
    provider = require_provider(name)
    with run_as(ALICE):
        return {**app_info(provider), "missing_scopes": provider.missing_scopes}


def test_scopes_missing_from_compares_against_the_grant() -> None:
    scopes = _server_scopes()
    older = _older_scopes()
    assert scopes_missing_from(scopes, " ".join(older)) == [f"{S}drive.file"]
    assert scopes_missing_from(scopes, " ".join(scopes)) == []
    assert scopes_missing_from(scopes, "  ") == []


def test_a_grant_without_a_requested_scope_needs_grant_access(
    creds: InMemoryCredentialStore,
) -> None:
    _connect(creds, _older_scopes())
    summary = _summary()
    assert summary["needs_grant"] is True
    assert summary["missing_scopes"] == [f"{S}drive.file"]


def test_a_full_grant_or_an_unrecorded_one_needs_nothing(
    creds: InMemoryCredentialStore,
) -> None:
    _connect(creds, list(_server_scopes()))
    assert _summary()["needs_grant"] is False
    _connect(creds, None)
    assert _summary()["needs_grant"] is False
    assert _summary()["missing_scopes"] == []


def test_not_connected_never_needs_grant_access(
    creds: InMemoryCredentialStore,
) -> None:
    creds.users[ALICE] = {GRANTED: " ".join(_older_scopes())}
    assert _summary()["needs_grant"] is False


def test_apps_that_record_no_grant_never_need_grant_access(
    creds: InMemoryCredentialStore,
) -> None:
    creds.users[ALICE] = {"GITHUB_ACCESS_TOKEN": "ghu-alice"}
    assert _summary("github")["connected"] is True
    assert _summary("github")["needs_grant"] is False


@pytest.mark.asyncio
async def test_a_refresh_records_the_scopes_the_grant_holds(
    creds: InMemoryCredentialStore,
) -> None:
    older = _older_scopes()
    _connect(creds, None)
    set_default_http_transport(google_token_transport({"alice-refresh": older}))
    with run_as(ALICE):
        await oauth.ensure_fresh_access_token("google_workspace", force=True)
    assert creds.get(ALICE, GRANTED).split() == older
    assert _summary()["missing_scopes"] == [f"{S}drive.file"]


def test_disconnecting_forgets_the_recorded_grant(
    creds: InMemoryCredentialStore,
) -> None:
    _connect(creds, _older_scopes())
    with run_as(ALICE):
        disconnect_provider("google_workspace")
    assert creds.get(ALICE, GRANTED) == ""


def _authorize_query() -> dict[str, str]:
    with run_as(ALICE):
        url = oauth.build_oauth_authorize_url("google_workspace")
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


def test_grant_access_asks_google_for_the_missing_scopes_on_top_of_the_grant(
    creds: InMemoryCredentialStore,
) -> None:
    query = _authorize_query()
    assert query["include_granted_scopes"] == "true"
    assert query["prompt"] == "consent"
    assert query["access_type"] == "offline"
    assert set(query["scope"].split()) == set(_server_scopes())


@pytest.mark.asyncio
async def test_signing_in_again_records_the_new_grant(
    creds: InMemoryCredentialStore,
) -> None:
    scopes = list(_server_scopes())
    _connect(creds, _older_scopes())
    state = _authorize_query()["state"]
    fallback = integrations_transport()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(
                200,
                json={
                    "access_token": "alice-access-1",
                    "refresh_token": "alice-refresh-2",
                    "expires_in": 3599,
                    "scope": " ".join(scopes),
                },
            )
        return await fallback.handle_async_request(request)

    set_default_http_transport(httpx.MockTransport(handler))
    with run_as(ALICE):
        await oauth.exchange_oauth_code("google_workspace", "code", state)
    assert (
        creds.get(ALICE, "GOOGLE_WORKSPACE_REFRESH_TOKEN") == "alice-refresh-2"
    )
    assert creds.get(ALICE, GRANTED).split() == scopes
    assert _summary()["needs_grant"] is False


def _revoke_transport(
    requests: list[httpx.Request], status: int = 200
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, json={})

    return httpx.MockTransport(handler)


def _connected_local() -> InMemoryCredentialStore:
    store = InMemoryCredentialStore()
    store.users["local"] = {
        "GOOGLE_WORKSPACE_REFRESH_TOKEN": "local-refresh",
        "GOOGLE_WORKSPACE_ACCESS_TOKEN": "fake-local-access-token",
        "GOOGLE_WORKSPACE_TOKEN_SOURCE": "oauth",
    }
    set_credential_store(store)
    return store


def test_disconnect_revokes_a_grant_this_app_issued() -> None:
    store = _connected_local()
    requests: list[httpx.Request] = []
    set_default_http_transport(_revoke_transport(requests))

    resp = client.post("/api/v1/auth/google_workspace/disconnect")

    assert resp.status_code == 200
    (revoke,) = requests
    assert str(revoke.url) == "https://oauth2.googleapis.com/revoke"
    assert parse_qs(revoke.content.decode()) == {"token": ["local-refresh"]}
    assert store.users["local"] == {}


def test_a_failed_revocation_never_blocks_the_disconnect() -> None:
    store = _connected_local()
    requests: list[httpx.Request] = []
    set_default_http_transport(_revoke_transport(requests, status=503))

    resp = client.post("/api/v1/auth/google_workspace/disconnect")

    assert resp.status_code == 200
    assert len(requests) == 1
    assert store.users["local"] == {}

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    _connected_local()
    set_default_http_transport(httpx.MockTransport(unreachable))
    assert (
        client.post("/api/v1/auth/google_workspace/disconnect").status_code
        == 200
    )
