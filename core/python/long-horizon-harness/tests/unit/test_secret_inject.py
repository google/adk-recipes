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

import pytest

from app.auth.oauth import (
    GCP_EXPIRES_KEY,
    GCP_TOKEN_KEY,
    GWS_EXPIRES_KEY,
    GWS_META_KEY,
    GWS_TOKEN_KEY,
)
from app.environment import LocalEnvironment
from app.environment_context import set_active_environment
from app.secrets import secret_env, set_secret_store
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


@pytest.mark.asyncio
async def test_secret_env_uses_environment_owner(
    wired_store, tmp_path, monkeypatch
):
    await wired_store.set_secret("alice@x", "K", "v")

    class OwnedEnv(LocalEnvironment):
        @property
        def owner(self):  # type: ignore[override]
            return "alice@x"

    set_active_environment(OwnedEnv(working_dir=tmp_path))
    assert await secret_env() == {"K": "v"}


@pytest.mark.asyncio
async def test_secret_env_falls_back_to_context_user(wired_store, tmp_path):
    await wired_store.set_secret("bob@x", "K", "v")
    set_active_environment(LocalEnvironment(working_dir=tmp_path))
    from app.auth.identity import _user_id_var

    token = _user_id_var.set("bob@x")
    try:
        assert await secret_env() == {"K": "v"}
    finally:
        _user_id_var.reset(token)


@pytest.mark.asyncio
async def test_secret_env_empty_without_user(wired_store, tmp_path):
    set_active_environment(LocalEnvironment(working_dir=tmp_path))
    assert await secret_env() == {}


async def _seed_mixed(store, owner):
    await store.set_secret(owner, "STRIPE_KEY", "sk_x")
    await store.set_secret(owner, GCP_TOKEN_KEY, "ya29.gcp")
    await store.set_secret(owner, GCP_EXPIRES_KEY, "1700000000")
    await store.set_secret(owner, GWS_TOKEN_KEY, "ya29.gws")
    await store.set_secret(owner, GWS_EXPIRES_KEY, "1700000001")
    await store.set_secret(owner, GWS_META_KEY, '{"surfaces":["drive"]}')


@pytest.fixture()
def owned_env(tmp_path):
    class OwnedEnv(LocalEnvironment):
        @property
        def owner(self):  # type: ignore[override]
            return "alice@x"

    set_active_environment(OwnedEnv(working_dir=tmp_path))
    return "alice@x"


@pytest.mark.asyncio
async def test_secret_env_includes_user_and_oauth(wired_store, owned_env):
    await _seed_mixed(wired_store, owned_env)
    result = await secret_env()
    assert result == {
        "STRIPE_KEY": "sk_x",
        GCP_TOKEN_KEY: "ya29.gcp",
        GCP_EXPIRES_KEY: "1700000000",
        GWS_TOKEN_KEY: "ya29.gws",
        GWS_EXPIRES_KEY: "1700000001",
        GWS_META_KEY: '{"surfaces":["drive"]}',
    }


@pytest.mark.asyncio
async def test_secret_env_fail_open_on_store_error(owned_env):
    class BoomStore:
        async def get_all(self, owner):
            raise RuntimeError("secret manager down")

    set_secret_store(BoomStore())
    try:
        assert await secret_env() == {}
    finally:
        set_secret_store(None)
