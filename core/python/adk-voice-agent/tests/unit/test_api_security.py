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

"""Security posture of the HTTP surface: deployed-mode guards, CORS, and the web UI mount."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.fast_api_app import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def local_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "APP_URL",
        "K_SERVICE",
        "CLOUD_RUN_JOB",
        "VOICE_AGENT_WEB_DIST",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def web_dist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>voice-agent-ui</html>")
    (dist / "assets" / "app.js").write_text("console.log('voice_agent')")
    (tmp_path / "outside.txt").write_text("outside-secret")
    monkeypatch.setenv("VOICE_AGENT_WEB_DIST", str(dist))
    return dist


def test_no_route_takes_a_token_or_an_oauth_client_from_a_request(
    monkeypatch: pytest.MonkeyPatch, iap_user: dict[str, str]
) -> None:
    monkeypatch.setenv("K_SERVICE", "adk-voice-agent")
    monkeypatch.setenv("APP_URL", "https://adk-voice-agent-prod.a.run.app")
    client = TestClient(app, headers=iap_user)

    gone = [
        client.post("/api/v1/auth/github/detect-cli"),
        client.post("/api/v1/auth/google_workspace/token", json={"token": "x"}),
        client.patch("/api/v1/integrations/github", json={"enabled": False}),
    ]
    assert all(r.status_code in (404, 405) for r in gone), [
        r.status_code for r in gone
    ]
    # An unregistered app can't be registered by posting a client.
    res = client.post(
        "/api/v1/auth/google_workspace/authorize",
        json={"client_id": "replaced", "client_secret": "leaked"},
    )
    assert res.status_code == 400
    assert "GOOGLE_OAUTH_CLIENT_ID" not in os.environ


def test_disconnect_changes_only_the_signed_in_users_store(
    monkeypatch: pytest.MonkeyPatch, iap_user: dict[str, str]
) -> None:
    from app.auth.credential_store import set_credential_store
    from tests.fakes import InMemoryCredentialStore

    monkeypatch.setenv("K_SERVICE", "adk-voice-agent")
    monkeypatch.setenv("APP_URL", "https://adk-voice-agent-prod.a.run.app")
    store = InMemoryCredentialStore()
    set_credential_store(store)
    for user in ("user@example.com", "other@example.com"):
        store.set_many(user, {"GITHUB_ACCESS_TOKEN": f"ghu-{user}"})
    client = TestClient(app, headers=iap_user)

    assert client.post("/api/v1/auth/github/disconnect").status_code == 200
    assert store.get("user@example.com", "GITHUB_ACCESS_TOKEN") == ""
    assert store.get("other@example.com", "GITHUB_ACCESS_TOKEN") == (
        "ghu-other@example.com"
    )
    assert "GITHUB_ACCESS_TOKEN" not in os.environ


def test_config_reports_whether_secrets_are_managed_remotely(
    monkeypatch: pytest.MonkeyPatch, iap_user: dict[str, str]
) -> None:
    local = client.get("/api/v1/config", headers=iap_user)
    assert local.json()["is_remote"] is False

    monkeypatch.setenv("K_SERVICE", "adk-voice-agent")
    monkeypatch.setenv("APP_URL", "https://adk-voice-agent-prod.a.run.app")

    config = client.get("/api/v1/config", headers=iap_user)
    assert config.json()["is_remote"] is True


def test_config_names_the_local_dev_user_with_no_sign_out() -> None:
    account = client.get("/api/v1/config").json()["account"]
    assert account["auth_mode"] == "dev" and account["user_id"]
    assert account["sign_out_url"] is None


def test_config_names_the_iap_user_and_how_to_sign_out(
    monkeypatch: pytest.MonkeyPatch, iap_user: dict[str, str]
) -> None:
    monkeypatch.setenv("K_SERVICE", "adk-voice-agent")
    monkeypatch.setenv("APP_URL", "https://adk-voice-agent-prod.a.run.app")
    account = client.get("/api/v1/config", headers=iap_user).json()["account"]
    assert account == {
        "user_id": "user@example.com",
        "auth_mode": "iap",
        "sign_out_url": "/_gcp_iap/clear_login_cookie",
    }


def test_cross_origin_requests_are_refused() -> None:
    res = client.get(
        "/api/v1/state", headers={"Origin": "https://evil.example"}
    )
    assert res.status_code == 403
    assert "access-control-allow-origin" not in res.headers


def test_web_ui_is_served_without_shadowing_api_routes(web_dist: Path) -> None:
    page_load = {"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}
    for res in (
        client.get("/"),
        client.get("/threads/thread-1234", headers=page_load),
    ):
        assert res.status_code == 200
        assert "voice-agent-ui" in res.text

    assert client.get("/assets/app.js").text == "console.log('voice_agent')"
    assert client.get("/assets/missing.js").status_code == 404
    traversal = client.get("/%2e%2e/outside.txt", headers=page_load)
    assert "outside-secret" not in traversal.text

    for headers in ({}, page_load):
        state = client.get("/api/v1/state", headers=headers)
        assert state.headers["content-type"].startswith("application/json")
        assert "threads" in state.json()
    unknown_api = client.get("/api/v1/does-not-exist")
    assert unknown_api.status_code == 404
    assert unknown_api.headers["content-type"].startswith("application/json")
    assert (
        client.get("/list-apps")
        .headers["content-type"]
        .startswith("application/json")
    )


def test_no_ui_without_a_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VOICE_AGENT_WEB_DIST", str(tmp_path / "missing"))

    assert client.get("/").status_code == 404
    assert client.get("/dev-ui/").status_code == 404


def test_rebound_host_names_are_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A page on another site that rebinds its name to this server sends its own
    # name as Host, with a matching Origin, so the origin check alone lets it in.
    rebound = {
        "Host": "evil.example:8080",
        "Origin": "http://evil.example:8080",
    }
    res = client.get("/api/v1/state", headers=rebound)
    assert res.status_code == 400
    assert "VOICE_AGENT_ALLOWED_HOSTS" in res.text

    assert client.get(
        "/api/v1/state", headers={"Host": "localhost:8080"}
    ).is_success
    assert client.get(
        "/api/v1/state", headers={"Host": "127.0.0.1:8000"}
    ).is_success
    assert (
        client.get(
            "/api/v1/state", headers={"Host": "host.docker.internal:8000"}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/v1/mcp", headers={"Host": "host.docker.internal:8000"}
        ).status_code
        != 400
    )

    monkeypatch.setenv("VOICE_AGENT_ALLOWED_HOSTS", "testserver,192.168.1.20")
    lan = {"Host": "192.168.1.20:3000"}
    assert client.get("/api/v1/state", headers=lan).is_success


def test_deployed_services_leave_host_checks_to_iap(
    monkeypatch: pytest.MonkeyPatch, iap_user: dict[str, str]
) -> None:
    monkeypatch.setenv("K_SERVICE", "adk-voice-agent")
    monkeypatch.setenv("APP_URL", "https://adk-voice-agent-prod.a.run.app")
    res = client.get(
        "/api/v1/state",
        headers={"Host": "adk-voice-agent-prod.a.run.app", **iap_user},
    )
    assert res.status_code == 200


def test_host_header_parsing() -> None:
    from app.app_utils.host_guard import host_name

    assert host_name("LocalHost:8080") == "localhost"
    assert host_name("[::1]:8000") == "[::1]"
    assert host_name("example.com") == "example.com"


def test_deployed_cors_allows_only_the_public_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.fast_api_app import default_allow_origins

    assert "http://localhost:3000" in default_allow_origins()
    monkeypatch.setenv("K_SERVICE", "adk-voice-agent")
    monkeypatch.setenv("APP_URL", "https://adk-voice-agent-prod.a.run.app/")
    assert default_allow_origins() == "https://adk-voice-agent-prod.a.run.app"


def test_approve_endpoint_requires_permission_key_and_rejects_stale_key() -> (
    None
):
    from app.threads.model import (
        PermissionKind,
        PermissionRequest,
        ThreadHandle,
        ThreadState,
    )
    from app.threads.registry import ThreadRegistry, set_thread_registry

    reg = ThreadRegistry()
    set_thread_registry(reg)
    try:
        handle = ThreadHandle(
            thread_id="thread-sec00001",
            goal="Send report",
            project="scratch/report",
            harness="claude",
            branch="",
            state=ThreadState.AWAITING_PERMISSION,
            permission=PermissionRequest(
                action_class="gmail.send*",
                command="gmail__send_message {}",
                key="expected-key-1234",
                kind=PermissionKind.TOOL,
            ),
        )
        reg._threads[handle.thread_id] = handle

        missing = client.post(f"/api/v1/threads/{handle.thread_id}/approve")
        assert missing.status_code == 400
        assert "permission_key" in missing.json()["detail"]

        blank = client.post(
            f"/api/v1/threads/{handle.thread_id}/approve",
            json={"permission_key": "   "},
        )
        assert blank.status_code == 400

        stale = client.post(
            f"/api/v1/threads/{handle.thread_id}/approve",
            json={"permission_key": "stale-key"},
        )
        assert stale.status_code == 409
        assert handle.state is ThreadState.AWAITING_PERMISSION
    finally:
        set_thread_registry(None)
