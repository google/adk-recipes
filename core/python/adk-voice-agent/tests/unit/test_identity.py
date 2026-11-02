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

"""Request identity: `dev` and `iap` modes, and ADK routes bound to the signed-in user."""

from __future__ import annotations

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.app_utils.http_client import set_default_http_transport
from app.auth.identity import current_user_id
from app.fast_api_app import app
from tests.fake_integrations import iap_assertion, iap_headers

client = TestClient(app)
IAP = "X-Goog-IAP-JWT-Assertion"


@pytest.fixture(autouse=True)
def local_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("APP_URL", "K_SERVICE", "CLOUD_RUN_JOB"):
        monkeypatch.delenv(key, raising=False)


def test_dev_mode_is_the_local_user() -> None:
    assert client.get("/api/v1/me").json() == {
        "user_id": "local",
        "auth_mode": "dev",
    }


def test_dev_user_id_can_be_renamed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOICE_AGENT_DEV_USER_ID", "ada")
    assert client.get("/api/v1/me").json()["user_id"] == "ada"


def test_dev_mode_is_refused_on_cloud_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("K_SERVICE", "adk-voice-agent")

    for path in ("/api/v1/me", "/api/v1/state", "/list-apps", "/"):
        res = client.get(path)
        assert res.status_code == 500, path
        assert "VOICE_AGENT_AUTH_MODE=iap" in res.json()["detail"]
    # Setting the mode explicitly doesn't get around it.
    monkeypatch.setenv("VOICE_AGENT_AUTH_MODE", "dev")
    assert client.get("/api/v1/me").status_code == 500


def test_unknown_auth_mode_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOICE_AGENT_AUTH_MODE", "trust-me")
    res = client.get("/api/v1/me")
    assert res.status_code == 500
    assert "trust-me" in res.json()["detail"]


def test_health_answers_without_an_identity(
    monkeypatch: pytest.MonkeyPatch, iap_user: dict[str, str]
) -> None:
    monkeypatch.setenv("K_SERVICE", "adk-voice-agent")
    assert client.get("/health").status_code == 200
    assert client.get("/api/v1/me").status_code == 401


def test_iap_assertion_names_the_user(iap_user: dict[str, str]) -> None:
    res = client.get("/api/v1/me", headers=iap_headers("Ada.L@Example.com"))
    assert res.json() == {"user_id": "ada.l@example.com", "auth_mode": "iap"}


def test_iap_mode_works_on_cloud_run(
    monkeypatch: pytest.MonkeyPatch, iap_user: dict[str, str]
) -> None:
    monkeypatch.setenv("K_SERVICE", "adk-voice-agent")
    res = client.get("/api/v1/me", headers=iap_user)
    assert res.json()["user_id"] == "user@example.com"


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {IAP: ""},
        {IAP: "not-a-jwt"},
        {IAP: iap_assertion("a@example.com", audience="/projects/9/other")},
        {IAP: iap_assertion("a@example.com", expires_in_s=-3600)},
        {IAP: iap_assertion("a@example.com", issuer="https://evil.example")},
        # Signed by a key that isn't IAP's, under IAP's key id.
        {
            IAP: iap_assertion(
                "a@example.com",
                private_key=ec.generate_private_key(ec.SECP256R1()),
            )
        },
        {IAP: iap_assertion("a@example.com", key_id="unknown-key")},
    ],
    ids=[
        "missing",
        "empty",
        "garbage",
        "wrong-audience",
        "expired",
        "wrong-issuer",
        "forged-signature",
        "unknown-key",
    ],
)
def test_bad_iap_assertions_are_unauthorized(
    iap_user: dict[str, str], headers: dict[str, str]
) -> None:
    assert client.get("/api/v1/me", headers=headers).status_code == 401
    assert client.get("/api/v1/state", headers=headers).status_code == 401


def test_iap_mode_without_an_audience_is_a_server_error(
    monkeypatch: pytest.MonkeyPatch, iap_user: dict[str, str]
) -> None:
    monkeypatch.delenv("VOICE_AGENT_IAP_AUDIENCE")
    res = client.get("/api/v1/me", headers=iap_user)
    assert res.status_code == 500
    assert "VOICE_AGENT_IAP_AUDIENCE" in res.json()["detail"]


def test_unreachable_iap_keys_are_a_temporary_failure(
    iap_user: dict[str, str],
) -> None:
    set_default_http_transport(
        httpx.MockTransport(lambda request: httpx.Response(502))
    )
    assert client.get("/api/v1/me", headers=iap_user).status_code == 503


def test_identity_does_not_outlive_the_request() -> None:
    assert client.get("/api/v1/me").status_code == 200
    with pytest.raises(RuntimeError):
        current_user_id()


# ADK routes take the user id from the client; it must be the signed-in user.


def test_adk_session_routes_are_bound_to_the_user() -> None:
    created = client.post(
        "/apps/app/users/local/sessions", json={"session_id": "s-own"}
    )
    assert created.status_code == 200
    assert client.get("/apps/app/users/local/sessions").status_code == 200

    for res in (
        client.get("/apps/app/users/someone-else/sessions"),
        client.post(
            "/apps/app/users/someone-else/sessions", json={"session_id": "s1"}
        ),
        client.get("/apps/app/users/someone-else/sessions/s-own"),
        client.delete("/apps/app/users/someone-else/sessions/s-own"),
        client.get("/apps/app/users/someone-else/sessions/s-own/artifacts"),
        client.patch(
            "/apps/app/users/someone-else/memory", json={"session_id": "s"}
        ),
    ):
        assert res.status_code == 403, res.request.url
        assert "signed-in user" in res.json()["detail"]


def test_adk_session_routes_use_the_iap_email(iap_user: dict[str, str]) -> None:
    own = client.post(
        "/apps/app/users/user@example.com/sessions",
        json={"session_id": "s-iap"},
        headers=iap_user,
    )
    assert own.status_code == 200
    other = client.get("/apps/app/users/local/sessions/s-iap", headers=iap_user)
    assert other.status_code == 403


@pytest.mark.parametrize("path", ["/run", "/run_sse"])
def test_run_bodies_are_bound_to_the_user(path: str) -> None:
    session = {"app_name": "app", "session_id": "missing-session"}
    for body in (
        {**session, "user_id": "someone-else"},
        {**session, "userId": "someone-else"},
        # A duplicate key can't smuggle another user past the check.
        '{"app_name": "app", "session_id": "s", "user_id": "local", "user_id": "x"}',
    ):
        kwargs = {"content": body} if isinstance(body, str) else {"json": body}
        res = client.post(
            path, headers={"Content-Type": "application/json"}, **kwargs
        )
        assert res.status_code == 403, body

    # The user's own id passes through to ADK, which looks up the session (absent
    # here); a `user_id` nested in the payload isn't the request's user.
    own = client.post(
        path,
        json={
            **session,
            "user_id": "local",
            "state_delta": {"user_id": "someone-else"},
        },
    )
    assert own.status_code != 403


def test_run_live_is_bound_to_the_user() -> None:
    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect(
            "/run_live?app_name=app&user_id=someone-else&session_id=s"
        ):
            pass
    assert closed.value.code == 1008
    assert "signed-in user" in closed.value.reason

    # A second `user_id` can't override the first.
    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect(
            "/run_live?app_name=app&user_id=local&user_id=x&session_id=s"
        ):
            pass
    assert closed.value.code == 1008


def test_run_live_needs_an_identity(iap_user: dict[str, str]) -> None:
    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect(
            "/run_live?app_name=app&user_id=user@example.com&session_id=s"
        ):
            pass
    assert closed.value.code == 1008
    assert "X-Goog-IAP-JWT-Assertion" in closed.value.reason


def test_service_account_identity_rejected_off_mcp_paths(
    iap_user: dict[str, str],
) -> None:
    sa_headers = iap_headers("computer@proj.iam.gserviceaccount.com")
    resp = client.get("/api/v1/state", headers=sa_headers)
    assert resp.status_code == 403
    assert "Service account identities" in resp.json()["detail"]

    approve = client.post(
        "/api/v1/threads/t1/approve",
        json={"approved": True},
        headers=sa_headers,
    )
    assert approve.status_code == 403

    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect(
            "/run_live?app_name=app&user_id=computer@proj.iam.gserviceaccount.com&session_id=s",
            headers=sa_headers,
        ):
            pass
    assert closed.value.code == 1008

    # /api/v1/mcp passes IdentityMiddleware and fails on missing MCP bearer token (401, not 403).
    mcp_resp = client.post("/api/v1/mcp", json={}, headers=sa_headers)
    assert mcp_resp.status_code == 401


def test_host_docker_internal_allowed_only_on_mcp_path() -> None:
    docker_host = {"Host": "host.docker.internal:8000"}
    state_resp = client.get("/api/v1/state", headers=docker_host)
    assert state_resp.status_code == 400

    mcp_resp = client.post("/api/v1/mcp", json={}, headers=docker_host)
    assert mcp_resp.status_code == 401
