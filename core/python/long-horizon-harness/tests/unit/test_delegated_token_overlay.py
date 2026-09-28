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

"""The GE-forwarded Google token overlays CLOUDSDK_AUTH_ACCESS_TOKEN per turn."""

from __future__ import annotations

import pytest

from app.environment import LocalEnvironment
from app.environment_context import set_active_environment
from app.secrets import secret_env, set_secret_store
from app.secrets.inject import (
    reset_delegated_google_token,
    set_delegated_google_token,
)
from app.secrets.store import SecretManagerStore
from tests.unit.test_secret_store import FakeSecretClient


@pytest.fixture()
def wired_store():
    store = SecretManagerStore(
        client=FakeSecretClient(), project_id="proj", ttl_s=60.0
    )
    set_secret_store(store)
    yield store
    set_secret_store(None)


@pytest.fixture()
def owned_env(tmp_path):
    class OwnedEnv(LocalEnvironment):
        @property
        def owner(self):  # type: ignore[override]
            return "alice@x"

    set_active_environment(OwnedEnv(working_dir=tmp_path))
    return "alice@x"


@pytest.mark.asyncio
async def test_delegated_token_overlays_cloudsdk(wired_store, owned_env):
    tok = set_delegated_google_token("ya29.ge-user")
    try:
        result = await secret_env()
    finally:
        reset_delegated_google_token(tok)
    assert result["CLOUDSDK_AUTH_ACCESS_TOKEN"] == "ya29.ge-user"


@pytest.mark.asyncio
async def test_no_overlay_without_token(wired_store, owned_env):
    assert "CLOUDSDK_AUTH_ACCESS_TOKEN" not in await secret_env()
