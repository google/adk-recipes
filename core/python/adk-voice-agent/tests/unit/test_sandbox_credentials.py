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

"""Worker credential boundary and GitHub git token forwarding."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator

import pytest

from app.auth.github.provider import reset_github_caches
from app.auth.github.sandbox_token import (
    NOT_CONNECTED_NOTICE,
    sandbox_github_token,
)
from app.auth.sandbox_env import get_sandbox_gcp_env, sandbox_location
from app.workers import workspace
from app.workers.environment import SandboxEnvironment
from app.workers.factory import sandbox_project_and_location


@pytest.fixture(autouse=True)
def _fresh_caches() -> Iterator[None]:
    reset_github_caches()
    yield
    reset_github_caches()


def test_sandbox_env_carries_no_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SANDBOX_GCP_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj")
    env = SandboxEnvironment(
        name="sandboxes/s", url="http://h", access_token="a"
    ).credential_env()
    assert "test-token" not in json.dumps(env)
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in env
    assert not {"GH_TOKEN", "GITHUB_ACCESS_TOKEN"} & set(env)
    assert "GCE_METADATA_HOST" not in env
    assert env["GOOGLE_CLOUD_PROJECT"] == "proj"


def test_the_computer_calls_models_in_its_own_location(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    monkeypatch.delenv("SANDBOX_GCP_LOCATION", raising=False)
    assert sandbox_location() == "global"
    assert sandbox_project_and_location()[1] == "global"
    assert get_sandbox_gcp_env()["GOOGLE_CLOUD_LOCATION"] == "global"

    monkeypatch.setenv("SANDBOX_GCP_LOCATION", "europe-west4")
    assert sandbox_project_and_location()[1] == "europe-west4"
    assert get_sandbox_gcp_env()["GOOGLE_CLOUD_LOCATION"] == "europe-west4"


def _manifest_repo(name: str, url: str) -> None:
    with open(os.environ["VOICE_AGENT_WORKSPACES_LOCAL_YAML"], "a") as f:
        f.write(f"repositories:\n  - name: {name}\n    git_url: {url}\n")


@pytest.mark.asyncio
async def test_a_connected_users_token_is_forwarded_to_git(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _manifest_repo("app", "https://github.com/me/app.git")
    monkeypatch.setenv("GITHUB_ACCESS_TOKEN", "ghu_mine")
    token = await sandbox_github_token("app")
    assert (token.token, token.notice) == ("ghu_mine", "")


@pytest.mark.asyncio
async def test_without_a_connection_git_gets_no_token_and_a_notice() -> None:
    _manifest_repo("app", "https://github.com/me/app.git")
    token = await sandbox_github_token("app")
    assert (token.token, token.notice) == ("", NOT_CONNECTED_NOTICE)


@pytest.mark.asyncio
async def test_seed_repos_need_no_github_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_ACCESS_TOKEN", "ghu_mine")
    token = await sandbox_github_token("voice-agent-fixture")
    assert (token.token, token.notice) == ("", "")


def test_git_env_injects_the_thread_token_in_memory() -> None:
    without_token = workspace.git_env()
    assert without_token["GIT_CONFIG_COUNT"] == "4"
    assert "Authorization:" not in " ".join(without_token.values())
    git = workspace.git_env("ghs_x")
    assert git["GIT_CONFIG_COUNT"] == "5"
    assert git["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert git["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic ")
