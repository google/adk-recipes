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

from app.environment.process import BackendGoneError
from app.environment.sandbox import SandboxEnvironment


def _env(minter, fetcher, *, interval=0.0):
    return SandboxEnvironment(
        client=object(),
        sandbox_name="sbx-1",
        lb_host="lb.example",
        routing_token="rt0",
        sandbox_token="jwt0",
        owner="u1",
        token_minter=minter,
        routing_fetcher=fetcher,
        routing_refresh_interval=interval,
    )


@pytest.mark.asyncio
async def test_refresh_applies_tokens_and_stays_alive():
    env = _env(lambda: "jwt1", lambda: "rt1")
    assert await env.refresh_auth() is True
    assert env._http.headers["Authorization"] == "Bearer jwt1"
    assert env._http.headers["X-Sandbox-Routing-Token"] == "rt1"


@pytest.mark.asyncio
async def test_backend_gone_evicts():
    def gone():
        raise BackendGoneError()

    assert await _env(lambda: "jwt1", gone).refresh_auth() is False


@pytest.mark.asyncio
async def test_transient_mint_error_keeps_cached_token_alive():
    def boom():
        raise RuntimeError("vertex hiccup")

    env = _env(boom, lambda: "rt1")
    assert await env.refresh_auth() is True
    assert env._http.headers["Authorization"] == "Bearer jwt0"


@pytest.mark.asyncio
async def test_routing_throttled_within_interval():
    calls = []
    env = _env(lambda: "jwt1", lambda: calls.append(1) or "rt1", interval=3600)
    await env.refresh_auth()
    assert calls == []


@pytest.mark.asyncio
async def test_no_minter_is_noop_alive():
    env = SandboxEnvironment(
        client=object(),
        sandbox_name="s",
        lb_host="lb",
        routing_token="rt",
        sandbox_token="jwt",
        owner="u",
    )
    assert await env.refresh_auth() is True
