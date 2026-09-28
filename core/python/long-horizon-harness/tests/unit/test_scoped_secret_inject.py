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
from app.secrets import set_secret_store
from app.secrets.inject import scoped_secret_env
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
async def test_scoped_returns_only_declared(wired_store, owned_env):
    await wired_store.set_secret("alice@x", "STRIPE_KEY", "sk_x")
    await wired_store.set_secret("alice@x", "OTHER", "o")
    assert await scoped_secret_env(["STRIPE_KEY"]) == {"STRIPE_KEY": "sk_x"}


@pytest.mark.asyncio
async def test_scoped_undeclared_name_omitted(wired_store, owned_env):
    await wired_store.set_secret("alice@x", "STRIPE_KEY", "sk_x")
    assert await scoped_secret_env(["MISSING"]) == {}


@pytest.mark.asyncio
async def test_scoped_empty_declared_returns_empty(wired_store, owned_env):
    await wired_store.set_secret("alice@x", "STRIPE_KEY", "sk_x")
    assert await scoped_secret_env([]) == {}
