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

"""Unit tests for the Connect-Google OAuth helpers + routes (no network)."""

from __future__ import annotations

import pytest

from app.auth import oauth as g

# --- workspace scope assembly --------------------------------------------


def test_workspace_scopes_readonly():
    scopes = g.workspace_scopes(["drive", "gmail"], readonly=True)
    assert scopes == [
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/gmail.readonly",
    ]


def test_workspace_scopes_readwrite():
    scopes = g.workspace_scopes(["gmail"], readonly=False)
    assert scopes == ["https://www.googleapis.com/auth/gmail.modify"]


def test_workspace_scopes_chat_has_two():
    scopes = g.workspace_scopes(["chat"], readonly=True)
    assert scopes == [
        "https://www.googleapis.com/auth/chat.messages.readonly",
        "https://www.googleapis.com/auth/chat.spaces.readonly",
    ]


def test_workspace_scopes_directory_is_readonly_in_both_modes():
    # The People/Directory grant (sender-name resolution) is inherently read,
    # so read-write mode must not silently drop it or request a broader scope.
    expected = ["https://www.googleapis.com/auth/directory.readonly"]
    assert g.workspace_scopes(["directory"], readonly=True) == expected
    assert g.workspace_scopes(["directory"], readonly=False) == expected


def test_workspace_scopes_skips_unknown_and_empty():
    assert g.workspace_scopes(["nope"], readonly=True) == []
    assert g.workspace_scopes([], readonly=True) == []


def test_workspace_scopes_tasks():
    assert g.workspace_scopes(["tasks"], readonly=True) == [
        "https://www.googleapis.com/auth/tasks.readonly"
    ]
    assert g.workspace_scopes(["tasks"], readonly=False) == [
        "https://www.googleapis.com/auth/tasks"
    ]


def test_workspace_scopes_slides():
    assert g.workspace_scopes(["slides"], readonly=True) == [
        "https://www.googleapis.com/auth/presentations.readonly"
    ]
    assert g.workspace_scopes(["slides"], readonly=False) == [
        "https://www.googleapis.com/auth/presentations"
    ]


def test_workspace_scopes_keep():
    assert g.workspace_scopes(["keep"], readonly=True) == [
        "https://www.googleapis.com/auth/keep.readonly"
    ]
    assert g.workspace_scopes(["keep"], readonly=False) == [
        "https://www.googleapis.com/auth/keep"
    ]


def test_workspace_scopes_script():
    assert g.workspace_scopes(["script"], readonly=True) == [
        "https://www.googleapis.com/auth/script.projects.readonly"
    ]
    assert g.workspace_scopes(["script"], readonly=False) == [
        "https://www.googleapis.com/auth/script.projects"
    ]


def test_workspace_scopes_meet():
    # Meet's read-write capability is the "create" scope; there is no plain
    # `meetings.space` write scope.
    assert g.workspace_scopes(["meet"], readonly=True) == [
        "https://www.googleapis.com/auth/meetings.space.readonly"
    ]
    assert g.workspace_scopes(["meet"], readonly=False) == [
        "https://www.googleapis.com/auth/meetings.space.created"
    ]


def test_workspace_scopes_forms_carries_responses_in_both_modes():
    # Reading form responses needs a dedicated scope orthogonal to body
    # read/write, so it rides along in both modes (like Chat's two scopes).
    assert g.workspace_scopes(["forms"], readonly=True) == [
        "https://www.googleapis.com/auth/forms.body.readonly",
        "https://www.googleapis.com/auth/forms.responses.readonly",
    ]
    assert g.workspace_scopes(["forms"], readonly=False) == [
        "https://www.googleapis.com/auth/forms.body",
        "https://www.googleapis.com/auth/forms.responses.readonly",
    ]


# --- signed state (CSRF) -------------------------------------------------


def test_sign_verify_roundtrip():
    payload = {
        "user_id": "u1",
        "target": "workspace",
        "readonly": True,
        "nonce": "n",
    }
    token = g.sign_state(payload, secret="k")
    assert g.verify_state(token, secret="k") == payload


def test_verify_rejects_tamper_and_wrong_secret():
    token = g.sign_state({"user_id": "u1"}, secret="k")
    assert g.verify_state(token + "x", secret="k") is None
    assert g.verify_state("garbage", secret="k") is None
    assert g.verify_state(token, secret="other") is None


def test_verify_rejects_non_dict_payload():
    # A validly-signed but non-dict (list) payload must be rejected.
    forged = g.sign_state([1, 2], secret="k")  # type: ignore[arg-type]
    assert g.verify_state(forged, secret="k") is None


# --- auth URL ------------------------------------------------------------


def test_build_auth_url_no_offline_has_state():
    url = g.build_auth_url(
        client_id="cid",
        redirect_uri="https://h/cb",
        scopes=[g.CLOUD_PLATFORM_SCOPE],
        state="st",
    )
    assert url.startswith(g.AUTH_ENDPOINT + "?")
    assert "client_id=cid" in url
    assert "response_type=code" in url
    assert "access_type=offline" not in url
    assert "state=st" in url


# --- code exchange (stubbed HTTP) ----------------------------------------


def test_exchange_code_returns_token_and_expiry():
    def fake_post(url: str, data: dict) -> dict:
        assert data["grant_type"] == "authorization_code"
        return {"access_token": "ya29.tok", "expires_in": 3599}

    tok, exp = g.exchange_code(
        code="c",
        client_id="i",
        client_secret="s",
        redirect_uri="r",
        post=fake_post,
    )
    assert tok == "ya29.tok"
    assert exp == 3599


def test_exchange_code_raises_without_token():
    with pytest.raises(g.OAuthError):
        g.exchange_code(
            code="c",
            client_id="i",
            client_secret="s",
            redirect_uri="r",
            post=lambda *_: {"error": "invalid_grant"},
        )


# --- persist / clear (stub store) ----------------------------------------


class _StubStore:
    def __init__(self) -> None:
        self.data: dict[str, dict[str, str]] = {}

    async def set_secret(self, user_id: str, name: str, value: str) -> None:
        self.data.setdefault(user_id, {})[name] = value

    async def delete_secret(self, user_id: str, name: str) -> bool:
        return self.data.get(user_id, {}).pop(name, None) is not None

    async def get_all(self, user_id: str) -> dict[str, str]:
        return dict(self.data.get(user_id, {}))


@pytest.mark.asyncio
async def test_persist_gcp():
    store = _StubStore()
    await g.persist_gcp(store, "u1", token="tok", expires_at=123)
    assert store.data["u1"][g.GCP_TOKEN_KEY] == "tok"
    assert store.data["u1"][g.GCP_EXPIRES_KEY] == "123"
    # GCP connection does not touch the Workspace token.
    assert g.GWS_TOKEN_KEY not in store.data["u1"]


@pytest.mark.asyncio
async def test_persist_workspace_writes_token_meta():
    store = _StubStore()
    await g.persist_workspace(
        store, "u1", token="tok", expires_at=0, surfaces=["chat"], readonly=True
    )
    u = store.data["u1"]
    assert u[g.GWS_TOKEN_KEY] == "tok"
    assert g.GCP_TOKEN_KEY not in u
    import json as _json

    assert _json.loads(u[g.GWS_META_KEY]) == {
        "surfaces": ["chat"],
        "readonly": True,
    }


@pytest.mark.asyncio
async def test_clear_connection_is_per_target():
    store = _StubStore()
    await g.persist_gcp(store, "u1", token="g", expires_at=1)
    await g.persist_workspace(
        store, "u1", token="w", expires_at=1, surfaces=["drive"], readonly=True
    )
    await g.clear_connection(store, "u1", "workspace")
    u = store.data["u1"]
    assert g.GCP_TOKEN_KEY in u  # GCP untouched
    assert g.GWS_TOKEN_KEY not in u and g.GWS_META_KEY not in u


# --- routes (TestClient) -------------------------------------------------


@pytest.fixture
def client_and_store(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.secrets.store import set_secret_store

    monkeypatch.setenv("LHA_AUTH_MODE", "dev")
    monkeypatch.setenv("LHA_DEV_USER_ID", "u-test")
    monkeypatch.setenv("LHA_GCP_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("LHA_GCP_OAUTH_CLIENT_SECRET", "sek")
    monkeypatch.setenv(
        "LHA_GCP_OAUTH_REDIRECT_URI", "https://h/lha/gcp/callback"
    )
    monkeypatch.setattr(g, "exchange_code", lambda **_: ("ya29.test", 3599))

    store = _StubStore()
    set_secret_store(store)
    app = FastAPI()
    g.attach_gcp_oauth_routes(app)
    try:
        yield TestClient(app), store
    finally:
        set_secret_store(None)


def _state(**payload):
    return g.sign_state({"nonce": "n", **payload}, secret="sek")


def test_callback_forged_state_400_no_write(client_and_store):
    client, store = client_and_store
    r = client.get(
        "/lha/gcp/callback", params={"code": "x", "state": "forged.sig"}
    )
    assert r.status_code == 400
    assert store.data == {}


def test_callback_other_user_403(client_and_store):
    client, store = client_and_store
    r = client.get(
        "/lha/gcp/callback",
        params={"code": "x", "state": _state(user_id="someone", target="gcp")},
    )
    assert r.status_code == 403
    assert store.data == {}


def test_callback_gcp_writes_gcp_token(client_and_store):
    client, store = client_and_store
    r = client.get(
        "/lha/gcp/callback",
        params={"code": "c", "state": _state(user_id="u-test", target="gcp")},
    )
    assert r.status_code == 200
    u = store.data["u-test"]
    assert u[g.GCP_TOKEN_KEY] == "ya29.test"
    assert g.GWS_TOKEN_KEY not in u


def test_callback_workspace_writes_gws_token_and_meta(client_and_store):
    client, store = client_and_store
    state = _state(
        user_id="u-test",
        target="workspace",
        surfaces=["chat", "drive"],
        readonly=True,
    )
    r = client.get("/lha/gcp/callback", params={"code": "c", "state": state})
    assert r.status_code == 200
    u = store.data["u-test"]
    assert u[g.GWS_TOKEN_KEY] == "ya29.test"
    assert g.GCP_TOKEN_KEY not in u
    import json as _json

    assert _json.loads(u[g.GWS_META_KEY])["surfaces"] == ["chat", "drive"]


def test_connect_rejects_bad_target(client_and_store):
    client, _ = client_and_store
    assert (
        client.post("/lha/gcp/connect", params={"target": "nope"}).status_code
        == 400
    )


def test_connect_workspace_requires_a_surface(client_and_store):
    client, _ = client_and_store
    r = client.post(
        "/lha/gcp/connect", params={"target": "workspace", "surfaces": ""}
    )
    assert r.status_code == 400


def test_status_reports_both_connections(client_and_store):
    client, _ = client_and_store
    r = client.get("/lha/gcp/status")
    body = r.json()
    assert body["gcp"]["connected"] is False
    assert body["workspace"]["connected"] is False
    assert "chat" in body["surfaces"]
    assert "directory" in body["surfaces"]
