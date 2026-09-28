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
from google.api_core.exceptions import AlreadyExists, NotFound

from app.secrets.store import SecretManagerStore, SecretTooLargeError


class FakeSecretClient:
    """In-memory stand-in for SecretManagerServiceAsyncClient."""

    def __init__(self) -> None:
        self.versions: dict[str, list[bytes]] = {}
        self.access_calls = 0

    @staticmethod
    def _sid(resource: str) -> str:
        return resource.split("/secrets/")[1].split("/versions/", maxsplit=1)[0]

    async def create_secret(self, request):
        sid = request["secret_id"]
        if sid in self.versions:
            raise AlreadyExists(sid)
        self.versions[sid] = []

    async def add_secret_version(self, request):
        sid = self._sid(request["parent"] + "/versions/x")
        self.versions.setdefault(sid, []).append(request["payload"]["data"])

    async def access_secret_version(self, request):
        self.access_calls += 1
        sid = self._sid(request["name"])
        chain = self.versions.get(sid)
        if not chain:
            raise NotFound(request["name"])
        return SimpleNamespace(payload=SimpleNamespace(data=chain[-1]))


@pytest.fixture()
def clock():
    return {"t": 0.0}


@pytest.fixture()
def store(clock):
    return SecretManagerStore(
        client=FakeSecretClient(),
        project_id="proj",
        ttl_s=60.0,
        clock=lambda: clock["t"],
        now_ms=lambda: 1000,
    )


@pytest.mark.asyncio
async def test_get_all_empty_when_no_secret(store):
    assert await store.get_all("alice@x") == {}


@pytest.mark.asyncio
async def test_set_then_get_roundtrip(store):
    await store.set_secret("alice@x", "OPENAI_API_KEY", "sk-1")
    assert await store.get_all("alice@x") == {"OPENAI_API_KEY": "sk-1"}


@pytest.mark.asyncio
async def test_list_names_returns_name_and_created_at(store):
    await store.set_secret("alice@x", "GITHUB_TOKEN", "ght-1")
    names = await store.list_names("alice@x")
    assert names == [{"name": "GITHUB_TOKEN", "created_at": 1000}]


@pytest.mark.asyncio
async def test_delete_removes_one_key(store):
    await store.set_secret("alice@x", "A", "1")
    await store.set_secret("alice@x", "B", "2")
    assert await store.delete_secret("alice@x", "A") is True
    assert await store.get_all("alice@x") == {"B": "2"}


@pytest.mark.asyncio
async def test_delete_missing_returns_false(store):
    assert await store.delete_secret("alice@x", "NOPE") is False


@pytest.mark.asyncio
async def test_users_are_isolated(store):
    await store.set_secret("alice@x", "K", "alice")
    await store.set_secret("bob@x", "K", "bob")
    assert await store.get_all("alice@x") == {"K": "alice"}
    assert await store.get_all("bob@x") == {"K": "bob"}


@pytest.mark.asyncio
async def test_cache_serves_within_ttl(store, clock):
    await store.set_secret("alice@x", "K", "v")
    await store.get_all("alice@x")
    before = store._client.access_calls
    await store.get_all("alice@x")
    assert store._client.access_calls == before


@pytest.mark.asyncio
async def test_cache_expires_after_ttl(store, clock):
    await store.set_secret("alice@x", "K", "v")
    await store.get_all("alice@x")
    before = store._client.access_calls
    clock["t"] = 61.0
    await store.get_all("alice@x")
    assert store._client.access_calls == before + 1


@pytest.mark.asyncio
async def test_blob_size_guard(store):
    big = "x" * 70_000
    with pytest.raises(SecretTooLargeError):
        await store.set_secret("alice@x", "BIG", big)


def test_secret_id_is_deterministic_and_valid(store):
    sid_a = store._secret_id("alice@example.com")
    sid_b = store._secret_id("alice@example.com")
    assert sid_a == sid_b
    assert sid_a.startswith("lha-secret-")
    assert all(c.isalnum() or c in "-_" for c in sid_a)
    assert store._secret_id("bob@example.com") != sid_a


@pytest.mark.asyncio
async def test_set_many_writes_one_version(store):
    await store.set_many("alice@x", {"A": "1", "B": "2", "C": "3"})
    sid = store._secret_id("alice@x")
    assert len(store._client.versions[sid]) == 1
    assert await store.get_all("alice@x") == {"A": "1", "B": "2", "C": "3"}


@pytest.mark.asyncio
async def test_set_many_merges_and_overwrites(store):
    await store.set_secret("alice@x", "A", "old")
    await store.set_secret("alice@x", "KEEP", "untouched")
    await store.set_many("alice@x", {"A": "new", "B": "2"})
    assert await store.get_all("alice@x") == {
        "A": "new",
        "B": "2",
        "KEEP": "untouched",
    }


@pytest.mark.asyncio
async def test_set_many_empty_writes_nothing(store):
    await store.set_secret("alice@x", "A", "1")
    versions_before = len(store._client.versions[store._secret_id("alice@x")])
    await store.set_many("alice@x", {})
    versions_after = len(store._client.versions[store._secret_id("alice@x")])
    assert versions_before == versions_after


@pytest.mark.asyncio
async def test_set_many_oversize_raises(store):
    big = {f"K{i}": "x" * 1000 for i in range(100)}
    with pytest.raises(SecretTooLargeError):
        await store.set_many("alice@x", big)
