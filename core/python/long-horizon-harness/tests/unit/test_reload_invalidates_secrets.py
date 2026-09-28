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

from types import SimpleNamespace

import pytest

from app.commands import _reload
from app.secrets import set_secret_store
from app.secrets.store import SecretManagerStore
from tests.unit.test_secret_store import FakeSecretClient


@pytest.mark.asyncio
async def test_reload_invalidates_user_secret_cache(monkeypatch):
    store = SecretManagerStore(client=FakeSecretClient(), project_id="proj")
    set_secret_store(store)
    try:
        await store.set_secret("alice@x", "K", "v")
        await store.get_all("alice@x")  # populate cache
        assert "alice@x" in store._cache

        async def _noop_reload(tool_context=None):
            return {"skills_refreshed": False, "reason": "no toolset"}

        monkeypatch.setattr("app.commands.reload", _noop_reload)

        ctx = SimpleNamespace(
            _invocation_context=SimpleNamespace(user_id="alice@x")
        )
        await _reload("", ctx)
        assert "alice@x" not in store._cache
    finally:
        set_secret_store(None)
