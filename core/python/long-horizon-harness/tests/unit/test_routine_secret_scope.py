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

from app.environment import LocalEnvironment
from app.environment_context import set_active_environment
from app.secrets import (
    reset_routine_secret_scope,
    secret_env,
    set_routine_secret_scope,
    set_secret_store,
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
async def test_secret_env_unscoped_by_default(wired_store, owned_env):
    await wired_store.set_secret("alice@x", "STRIPE_KEY", "sk_x")
    await wired_store.set_secret("alice@x", "OTHER", "o")
    assert await secret_env() == {"STRIPE_KEY": "sk_x", "OTHER": "o"}


@pytest.mark.asyncio
async def test_secret_env_respects_active_scope(wired_store, owned_env):
    await wired_store.set_secret("alice@x", "STRIPE_KEY", "sk_x")
    await wired_store.set_secret("alice@x", "OTHER", "o")
    token = set_routine_secret_scope(["STRIPE_KEY"])
    try:
        assert await secret_env() == {"STRIPE_KEY": "sk_x"}
    finally:
        reset_routine_secret_scope(token)
    # scope reset → unscoped again
    assert await secret_env() == {"STRIPE_KEY": "sk_x", "OTHER": "o"}


@pytest.mark.asyncio
async def test_empty_scope_returns_nothing(wired_store, owned_env):
    await wired_store.set_secret("alice@x", "STRIPE_KEY", "sk_x")
    token = set_routine_secret_scope([])
    try:
        assert await secret_env() == {}
    finally:
        reset_routine_secret_scope(token)
