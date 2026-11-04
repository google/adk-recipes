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

"""Per-user connected-app credentials: the stores, isolation between users, and policy."""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from app.app_utils.http_client import set_default_http_transport
from app.auth.credential_store import (
    MAX_STALE_S,
    EnvCredentialStore,
    SecretManagerCredentialStore,
    get_credential_store,
    set_credential_store,
    user_secret_id,
)
from app.auth.env_store import (
    dotenv_path,
    load_dotenv_defaults,
    persist_env_vars,
    read_dotenv,
)
from app.auth.github.sandbox_token import sandbox_github_token
from app.auth.identity import run_as
from app.auth.oauth import ensure_fresh_access_token
from app.auth.providers import has_provider_credentials
from app.auth.sandbox_env import get_sandbox_gcp_env
from app.fast_api_app import app
from app.workers.workspace import git_env
from tests.fake_integrations import iap_headers, integrations_transport
from tests.fakes import (
    FakeSecretManagerClient,
    InMemoryCredentialStore,
    configure_apps,
)

ALICE, BOB = "alice@example.com", "bob@example.com"
APP_URL = "https://adk-voice-agent-prod.a.run.app"


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "APP_URL",
        "K_SERVICE",
        "CLOUD_RUN_JOB",
        "GOOGLE_WORKSPACE_REDIRECT_URI",
    ):
        monkeypatch.delenv(key, raising=False)
    configure_apps(monkeypatch)


@pytest.fixture
def deployed(monkeypatch: pytest.MonkeyPatch, iap_user: dict[str, str]) -> None:
    monkeypatch.setenv("K_SERVICE", "adk-voice-agent")
    monkeypatch.setenv("APP_URL", APP_URL)


@pytest.fixture
def workspace_token_endpoint() -> Iterator[list[dict[str, list[str]]]]:
    grants: list[dict[str, list[str]]] = []
    fallback = integrations_transport()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            grants.append(parse_qs(request.content.decode()))
            n = len(grants)
            return httpx.Response(
                200,
                json={
                    "access_token": f"gw-access-{n}",
                    "refresh_token": f"gw-refresh-{n}",
                    "expires_in": 3600,
                },
            )
        return await fallback.handle_async_request(request)

    set_default_http_transport(httpx.MockTransport(handler))
    yield grants


def test_local_mode_keeps_tokens_in_dotenv_and_the_environment(
    workspace_token_endpoint: list[dict[str, list[str]]],
) -> None:
    assert isinstance(get_credential_store(), EnvCredentialStore)
    client = TestClient(app)

    started = client.post("/api/v1/auth/google_workspace/authorize")
    assert started.status_code == 200, started.text
    state = parse_qs(urlparse(started.json()["authorize_url"]).query)["state"][
        0
    ]
    done = client.get(
        "/api/v1/auth/google_workspace/callback",
        params={"code": "local-code", "state": state},
    )

    assert "Connected to" in done.text
    assert os.environ["GOOGLE_WORKSPACE_ACCESS_TOKEN"] == "gw-access-1"
    assert read_dotenv()["GOOGLE_WORKSPACE_REFRESH_TOKEN"] == "gw-refresh-1"
    assert read_dotenv()["GOOGLE_WORKSPACE_ACCOUNT"]
    assert has_provider_credentials("google_workspace")

    assert (
        client.post("/api/v1/auth/google_workspace/disconnect").status_code
        == 200
    )
    assert "GOOGLE_WORKSPACE_ACCESS_TOKEN" not in os.environ
    assert "GOOGLE_WORKSPACE_ACCESS_TOKEN" not in read_dotenv()
    assert "GOOGLE_WORKSPACE_ACCOUNT" not in read_dotenv()


def test_secret_manager_store_keeps_one_hashed_secret_per_user() -> None:
    client = FakeSecretManagerClient()
    store = SecretManagerCredentialStore(client=client, project_id="p")

    store.set_many(
        ALICE, {"GITHUB_ACCESS_TOKEN": "a-1", "GITHUB_ACCOUNT": "@alice"}
    )
    store.set_many(BOB, {"GITHUB_ACCESS_TOKEN": "b-1"})
    store.set_many(
        ALICE, {"GITHUB_ACCESS_TOKEN": "a-2", "GITHUB_ACCOUNT": None}
    )

    alice_secret = f"projects/p/secrets/{user_secret_id(ALICE)}"
    assert user_secret_id(ALICE).startswith("voice-agent-user-")
    assert "alice" not in user_secret_id(ALICE)
    assert client.blob(alice_secret) == {"GITHUB_ACCESS_TOKEN": "a-2"}
    assert store.get_all(ALICE) == {"GITHUB_ACCESS_TOKEN": "a-2"}
    assert store.get(BOB, "GITHUB_ACCESS_TOKEN") == "b-1"
    assert store.get("carol@example.com", "GITHUB_ACCESS_TOKEN") == ""
    versions = client.secrets[alice_secret]["versions"]
    assert [bool(data) for data in versions.values()] == [False, True]
    assert client.secrets[alice_secret]["labels"]["kind"] == "user-credentials"


def test_secret_manager_reads_are_cached_until_a_write_or_the_ttl() -> None:
    now = [0.0]
    client = FakeSecretManagerClient()
    store = SecretManagerCredentialStore(
        client=client, project_id="p", ttl_s=30.0, clock=lambda: now[0]
    )
    store.set_many(ALICE, {"GITHUB_ACCESS_TOKEN": "ghu-1"})
    client.calls.clear()

    for _ in range(3):
        assert store.get(ALICE, "GITHUB_ACCESS_TOKEN") == "ghu-1"
    assert client.calls == []

    other_instance = SecretManagerCredentialStore(client=client, project_id="p")
    other_instance.set_many(ALICE, {"GITHUB_ACCESS_TOKEN": "ghu-2"})
    client.calls.clear()
    now[0] = 31.0
    assert store.get(ALICE, "GITHUB_ACCESS_TOKEN") == "ghu-1"
    deadline = time.monotonic() + 5
    while store.get(ALICE, "GITHUB_ACCESS_TOKEN") != "ghu-2":
        assert time.monotonic() < deadline, (
            "the background refresh never landed"
        )
        time.sleep(0.01)
    assert client.calls.count("access_secret_version") == 1

    now[0] = 31.0 + 30.0 + MAX_STALE_S + 1
    other_instance.set_many(ALICE, {"GITHUB_ACCESS_TOKEN": "ghu-3"})
    assert store.get(ALICE, "GITHUB_ACCESS_TOKEN") == "ghu-3"


def test_invalidate_forces_a_fresh_read() -> None:
    client = FakeSecretManagerClient()
    store = SecretManagerCredentialStore(client=client, project_id="p")
    store.set_many(ALICE, {"GITHUB_ACCESS_TOKEN": "a-1"})
    SecretManagerCredentialStore(client=client, project_id="p").set_many(
        ALICE, {"GITHUB_ACCESS_TOKEN": "a-2"}
    )
    assert store.get(ALICE, "GITHUB_ACCESS_TOKEN") == "a-1"

    store.invalidate(ALICE)

    assert store.get(ALICE, "GITHUB_ACCESS_TOKEN") == "a-2"


def test_deployed_service_uses_secret_manager_without_a_request_user(
    deployed: None,
) -> None:
    store = get_credential_store()
    assert isinstance(store, SecretManagerCredentialStore)
    with pytest.raises(RuntimeError):
        has_provider_credentials("github")


def test_two_users_never_see_each_others_tokens(deployed: None) -> None:
    client = TestClient(app)
    alice, bob = iap_headers(ALICE), iap_headers(BOB)
    store = get_credential_store()
    store.set_many(
        ALICE, {"GITHUB_ACCESS_TOKEN": "ghu-alice", "GITHUB_ACCOUNT": "@alice"}
    )

    def github(headers: dict[str, str]) -> dict[str, object]:
        config = client.get("/api/v1/config", headers=headers).json()
        (app_,) = [a for a in config["apps"] if a["name"] == "github"]
        return app_

    assert github(alice)["connected"] is True
    assert github(alice)["account_label"] == "@alice"
    assert github(bob)["connected"] is False
    assert github(bob)["account_label"] == ""
    assert "GITHUB_ACCESS_TOKEN" not in os.environ

    store.set_many(BOB, {"GITHUB_ACCESS_TOKEN": "ghu-bob"})
    client.post("/api/v1/auth/github/disconnect", headers=alice)
    assert github(alice)["connected"] is False
    assert github(bob)["connected"] is True
    with run_as(BOB):
        assert (
            get_credential_store().get(BOB, "GITHUB_ACCESS_TOKEN") == "ghu-bob"
        )


def test_oauth_sign_in_started_by_one_user_cannot_be_completed_by_another(
    deployed: None, workspace_token_endpoint: list[dict[str, list[str]]]
) -> None:
    store = InMemoryCredentialStore()
    set_credential_store(store)
    client = TestClient(app)

    started = client.post(
        "/api/v1/auth/google_workspace/authorize",
        headers=iap_headers(ALICE),
    )
    assert started.status_code == 200, started.text
    state = parse_qs(urlparse(started.json()["authorize_url"]).query)["state"][
        0
    ]

    hijack = client.get(
        "/api/v1/auth/google_workspace/callback",
        params={"code": "alice-code", "state": state},
        headers=iap_headers(BOB),
    )
    assert "different user" in hijack.text
    assert workspace_token_endpoint == []  # the code was never exchanged
    assert store.users == {}

    # The state is spent; Alice starts again and completes her own sign-in.
    started = client.post(
        "/api/v1/auth/google_workspace/authorize",
        headers=iap_headers(ALICE),
    )
    state = parse_qs(urlparse(started.json()["authorize_url"]).query)["state"][
        0
    ]
    done = client.get(
        "/api/v1/auth/google_workspace/callback",
        params={"code": "alice-code", "state": state},
        headers=iap_headers(ALICE),
    )
    assert "Connected to" in done.text
    assert store.get(ALICE, "GOOGLE_WORKSPACE_REFRESH_TOKEN") == "gw-refresh-1"
    assert store.get(ALICE, "GOOGLE_WORKSPACE_ACCESS_TOKEN") == "gw-access-1"
    assert BOB not in store.users


@pytest.mark.asyncio
async def test_rotated_refresh_tokens_are_saved_to_the_users_store(
    deployed: None, workspace_token_endpoint: list[dict[str, list[str]]]
) -> None:
    store = InMemoryCredentialStore()
    store.set_many(
        ALICE, {"GOOGLE_WORKSPACE_REFRESH_TOKEN": "alice-old-refresh"}
    )
    store.set_many(BOB, {"GOOGLE_WORKSPACE_REFRESH_TOKEN": "bob-refresh"})
    set_credential_store(store)

    with run_as(ALICE):
        token = await ensure_fresh_access_token("google_workspace")

    (grant,) = workspace_token_endpoint
    assert grant["refresh_token"] == ["alice-old-refresh"]
    assert token == "gw-access-1"
    assert store.get_all(ALICE) == {
        "GOOGLE_WORKSPACE_ACCESS_TOKEN": "gw-access-1",
        "GOOGLE_WORKSPACE_REFRESH_TOKEN": "gw-refresh-1",
    }
    assert store.get_all(BOB) == {
        "GOOGLE_WORKSPACE_REFRESH_TOKEN": "bob-refresh"
    }


@pytest.mark.asyncio
async def test_sandbox_and_git_get_only_the_acting_users_github_token(
    deployed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_ACCESS_TOKEN", "ghu-deployer")
    monkeypatch.setenv("SANDBOX_GCP_ACCESS_TOKEN", "gcp-token")
    local_yaml = os.environ["VOICE_AGENT_WORKSPACES_LOCAL_YAML"]
    with open(local_yaml, "a") as f:
        f.write(
            "repositories:\n  - name: app\n"
            "    git_url: https://github.com/alice/app.git\n"
        )
    store = InMemoryCredentialStore()
    store.set_many(ALICE, {"GITHUB_ACCESS_TOKEN": "ghu-alice"})
    store.set_many(BOB, {"GITHUB_ACCESS_TOKEN": "ghu-bob"})
    set_credential_store(store)

    with run_as(ALICE):
        assert (await sandbox_github_token("app")).token == "ghu-alice"
    with run_as(BOB):
        assert (await sandbox_github_token("app")).token == "ghu-bob"
    with run_as("carol@example.com"):
        assert (await sandbox_github_token("app")).token == ""
    with run_as(ALICE):
        assert not {"GH_TOKEN", "GITHUB_ACCESS_TOKEN"} & set(
            get_sandbox_gcp_env()
        )
    assert "Authorization:" not in " ".join(git_env().values())


@pytest.mark.parametrize(
    "key", ["lower", "1ABC", "A-B", "PATH\nEVIL", "", "A B"]
)
def test_rejects_malformed_keys_without_touching_the_environment(
    key: str,
) -> None:
    with pytest.raises(ValueError, match="Invalid environment variable name"):
        persist_env_vars({key: "x", "VOICE_AGENT_TEST_OK": "y"})
    assert "VOICE_AGENT_TEST_OK" not in os.environ
    assert not dotenv_path().exists()


def test_values_with_quotes_and_newlines_stay_on_one_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VOICE_AGENT_TEST_SECRET", "placeholder")
    value = 'a"b\\c\nEVIL=1\r\n# not a comment'

    persist_env_vars({"VOICE_AGENT_TEST_SECRET": value})

    assert dotenv_path().read_text().count("\n") == 1
    assert read_dotenv() == {"VOICE_AGENT_TEST_SECRET": value}
    assert os.environ["VOICE_AGENT_TEST_SECRET"] == value


def test_updates_replace_and_remove_existing_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VOICE_AGENT_TEST_A", "placeholder")
    monkeypatch.setenv("VOICE_AGENT_TEST_B", "placeholder")
    dotenv_path().write_text(
        '# keep me\nVOICE_AGENT_TEST_A="old"\nVOICE_AGENT_TEST_B="gone"\n'
    )

    persist_env_vars({"VOICE_AGENT_TEST_A": "new", "VOICE_AGENT_TEST_B": None})

    assert dotenv_path().read_text() == '# keep me\nVOICE_AGENT_TEST_A="new"\n'
    assert "VOICE_AGENT_TEST_B" not in os.environ


def test_dotenv_is_written_owner_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VOICE_AGENT_TEST_TOKEN", "placeholder")
    dotenv_path().write_text("# existing\n")
    dotenv_path().chmod(0o644)

    assert persist_env_vars({"VOICE_AGENT_TEST_TOKEN": "t"}) is True

    assert dotenv_path().stat().st_mode & 0o777 == 0o600
    assert read_dotenv() == {"VOICE_AGENT_TEST_TOKEN": "t"}
    assert not list(dotenv_path().parent.glob(".*.tmp"))


def test_unwritable_dotenv_keeps_the_value_in_process(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("VOICE_AGENT_TEST_RO", "placeholder")
    monkeypatch.setenv("VOICE_AGENT_DOTENV_PATH", "/nonexistent-dir/.env")

    assert persist_env_vars({"VOICE_AGENT_TEST_RO": "kept"}) is False

    assert os.environ["VOICE_AGENT_TEST_RO"] == "kept"
    assert "lasts until restart" in caplog.text


def test_load_dotenv_defaults_honors_the_load_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VOICE_AGENT_TEST_LOADED", "placeholder")
    monkeypatch.delenv("VOICE_AGENT_TEST_LOADED")
    dotenv_path().write_text('VOICE_AGENT_TEST_LOADED="from-file"\n')

    load_dotenv_defaults()
    assert "VOICE_AGENT_TEST_LOADED" not in os.environ

    monkeypatch.setenv("VOICE_AGENT_LOAD_DOTENV", "1")
    load_dotenv_defaults()
    assert os.environ["VOICE_AGENT_TEST_LOADED"] == "from-file"
