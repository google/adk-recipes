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

from app.conversation.system_prompt import _available_secrets_line
from app.secrets import set_secret_store
from app.secrets.store import SecretManagerStore
from tests.unit.test_secret_store import FakeSecretClient


@pytest.mark.asyncio
async def test_line_lists_names_not_values():
    store = SecretManagerStore(client=FakeSecretClient(), project_id="proj")
    set_secret_store(store)
    try:
        await store.set_secret("alice@x", "OPENAI_API_KEY", "sk-secret")
        line = await _available_secrets_line("alice@x")
        assert "OPENAI_API_KEY" in line
        assert "sk-secret" not in line
    finally:
        set_secret_store(None)


@pytest.mark.asyncio
async def test_line_empty_when_no_secrets():
    store = SecretManagerStore(client=FakeSecretClient(), project_id="proj")
    set_secret_store(store)
    try:
        assert await _available_secrets_line("alice@x") == ""
    finally:
        set_secret_store(None)
