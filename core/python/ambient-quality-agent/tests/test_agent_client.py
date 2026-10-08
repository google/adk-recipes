# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


"""Hermetic tests for the shared AQA wire client (``ambient_quality_shared.agent_client``).

These pin the wire contract without any network:

* URL / resource-id resolution,
* backend selection and the A2A base URL, and
* the exact command-route calls each backend makes (via a fake
  ``httpx.AsyncClient``).
"""

from __future__ import annotations

import asyncio

import pytest
from ambient_quality_shared import agent_client as ac


# --------------------------------------------------------------------------- #
# Async helper + a fake httpx.AsyncClient that records requests
# --------------------------------------------------------------------------- #
def _run(coro):
    return asyncio.run(coro)


class _FakeResp:
    def __init__(self, *, status_code=200, json_data=None):
        self.status_code = status_code
        self._json = {} if json_data is None else json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json


class _FakeAsyncClient:
    """Returns a canned post response and appends every call to ``calls``."""

    def __init__(self, calls, post_resp):
        self._calls = calls
        self._post = post_resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        self._calls.append(
            {"verb": "post", "url": url, "json": json, "headers": headers}
        )
        return self._post


def _install_fake_httpx(monkeypatch, calls, post_resp):
    def factory(*_a, **_k):
        return _FakeAsyncClient(calls, post_resp)

    monkeypatch.setattr(ac.httpx, "AsyncClient", factory)


# --------------------------------------------------------------------------- #
# AgentRuntimeClient construction
# --------------------------------------------------------------------------- #
def test_agent_engine_from_resource_id():
    c = ac.AgentRuntimeClient(
        resource_id="projects/p/locations/us-central1/reasoningEngines/9"
    )
    assert c.container_base == (
        "https://us-central1-aiplatform.googleapis.com/reasoningEngines/v1/"
        "projects/p/locations/us-central1/reasoningEngines/9"
    )


def test_agent_engine_from_url_strips_verb():
    url = (
        "https://us-central1-aiplatform.googleapis.com/v1/projects/p/locations/"
        "us-central1/reasoningEngines/9:streamQuery"
    )
    c = ac.AgentRuntimeClient(url=url)
    assert (
        c.resource_id == "projects/p/locations/us-central1/reasoningEngines/9"
    )
    assert c.container_base.endswith("/reasoningEngines/9")


@pytest.mark.parametrize(
    "url",
    [
        "https://aiplatform.example.test/other",
        "https://us-east1-aiplatform.googleapis.com/v1/foo",
        "https://us-east1-aiplatform.googleapis.com/v1/projects/p/locations/"
        "us-east1/reasoningEngines/",
        "https://us-east1-aiplatform.googleapis.com/reasoningEngines/v1/foo/api",
    ],
    ids=["no-version", "v1-garbage", "no-engine-id", "passthrough-garbage"],
)
def test_agent_engine_from_url_without_a_resource_path_is_rejected(url):
    with pytest.raises(ValueError, match="containing projects/P"):
        ac.AgentRuntimeClient(url=url)


@pytest.mark.parametrize(
    "url",
    [
        "projects/p/locations/us-east1/reasoningEngines/1",
        "us-east1-aiplatform.googleapis.com/v1/projects/p/locations/us-east1/"
        "reasoningEngines/1",
    ],
    ids=["resource-path", "no-scheme"],
)
def test_agent_engine_from_a_relative_url_is_rejected(url):
    with pytest.raises(ValueError, match="absolute engine URL"):
        ac.AgentRuntimeClient(url=url)


@pytest.mark.parametrize(
    "resource_id",
    [
        "123",
        "projects/p/foo/bar/reasoningEngines/1",
        "https://us-east1-aiplatform.googleapis.com/v1/projects/p/locations/"
        "us-east1/reasoningEngines/1",
    ],
    ids=["bare-id", "wrong-shape", "full-url"],
)
def test_agent_engine_from_a_partial_resource_id_is_rejected(resource_id):
    with pytest.raises(ValueError, match="engine resource like"):
        ac.AgentRuntimeClient(resource_id=resource_id)


_US_EAST1_ENGINE = "projects/p/locations/us-east1/reasoningEngines/123"
_US_EAST1_CONTAINER_BASE = (
    "https://us-east1-aiplatform.googleapis.com/reasoningEngines/v1/"
    f"{_US_EAST1_ENGINE}"
)


@pytest.mark.parametrize(
    "url",
    [
        f"https://us-east1-aiplatform.googleapis.com/v1/{_US_EAST1_ENGINE}",
        f"https://us-east1-aiplatform.googleapis.com/v1beta1/{_US_EAST1_ENGINE}",
        f"https://us-east1-aiplatform.googleapis.com/v1/{_US_EAST1_ENGINE}:query",
        f"https://us-east1-aiplatform.googleapis.com/v1/{_US_EAST1_ENGINE}:streamQuery",
        _US_EAST1_CONTAINER_BASE,
        f"{_US_EAST1_CONTAINER_BASE}/api",
        f"{_US_EAST1_CONTAINER_BASE}/api/",
    ],
    ids=[
        "v1",
        "v1beta1",
        "v1-query",
        "v1-stream-query",
        "passthrough",
        "passthrough-api",
        "api-slash",
    ],
)
def test_container_base_from_any_engine_url_form(url):
    c = ac.AgentRuntimeClient(url=url)

    assert c.resource_id == _US_EAST1_ENGINE
    assert c.container_base == _US_EAST1_CONTAINER_BASE


def test_container_base_from_resource_id():
    c = ac.AgentRuntimeClient(resource_id=_US_EAST1_ENGINE)

    assert c.container_base == _US_EAST1_CONTAINER_BASE


def test_container_base_keeps_a_non_regional_host():
    c = ac.AgentRuntimeClient(
        url=f"https://aiplatform.example.test/v1/{_US_EAST1_ENGINE}"
    )

    assert c.container_base == (
        f"https://aiplatform.example.test/reasoningEngines/v1/{_US_EAST1_ENGINE}"
    )


def test_agent_engine_requires_target():
    with pytest.raises(ValueError):
        ac.AgentRuntimeClient()


# --------------------------------------------------------------------------- #
# Backend selection
# --------------------------------------------------------------------------- #
@pytest.fixture
def _clean_env(monkeypatch):
    for key in (
        "AQA_BACKEND",
        "AGENT_ENGINE_RESOURCE_ID",
        "AGENT_ENGINE_URL",
        "AGENT_ADK_BASE_URL",
        "AGENT_A2A_BASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def test_client_from_env_agent_engine(_clean_env):
    _clean_env.setenv(
        "AGENT_ENGINE_RESOURCE_ID",
        "projects/p/locations/us-central1/reasoningEngines/1",
    )
    assert isinstance(ac.build_client_from_env(), ac.AgentRuntimeClient)


def test_client_from_env_local_adk(_clean_env):
    _clean_env.setenv("AGENT_ADK_BASE_URL", "http://localhost:8000")
    assert isinstance(ac.build_client_from_env(), ac.AdkHttpClient)


def test_client_from_env_a2a_rejected(_clean_env):
    _clean_env.setenv(
        "AGENT_ENGINE_RESOURCE_ID",
        "projects/p/locations/us-central1/reasoningEngines/1",
    )
    _clean_env.setenv("AQA_BACKEND", "a2a")
    with pytest.raises(RuntimeError, match="not valid targets"):
        ac.build_client_from_env()


def test_client_from_env_engine_url_alone(_clean_env):
    _clean_env.setenv(
        "AGENT_ENGINE_URL",
        "https://us-central1-aiplatform.googleapis.com/v1/projects/p/"
        "locations/us-central1/reasoningEngines/1",
    )
    assert isinstance(ac.build_client_from_env(), ac.AgentRuntimeClient)


def test_client_from_env_malformed_engine_url(_clean_env):
    _clean_env.setenv("AGENT_ENGINE_URL", "https://example.test/v1/foo")
    with pytest.raises(RuntimeError, match="AGENT_ENGINE_URL"):
        ac.build_client_from_env()


def test_client_from_env_a2a_base_url_rejected(_clean_env):
    _clean_env.setenv(
        "AGENT_ENGINE_RESOURCE_ID",
        "projects/p/locations/us-central1/reasoningEngines/1",
    )
    _clean_env.setenv("AGENT_A2A_BASE_URL", "http://localhost:9000")
    with pytest.raises(RuntimeError, match="not valid targets"):
        ac.build_client_from_env()


def test_resolve_a2a_base_url_for_a_local_server():
    client = ac.AdkHttpClient("http://localhost:8000/")

    assert ac.resolve_a2a_base_url(client) == ("http://localhost:8000", False)


def test_resolve_a2a_base_url_for_agent_runtime():
    client = ac.AgentRuntimeClient(
        resource_id="projects/p/locations/us-east1/reasoningEngines/1"
    )

    assert ac.resolve_a2a_base_url(client) == (
        "https://us-east1-aiplatform.googleapis.com/reasoningEngines/v1/"
        "projects/p/locations/us-east1/reasoningEngines/1/api",
        True,
    )


def test_resolve_a2a_base_url_rejects_other_clients():
    with pytest.raises(RuntimeError, match="No A2A route"):
        ac.resolve_a2a_base_url(object())


def test_client_from_env_unconfigured(_clean_env):
    with pytest.raises(RuntimeError):
        ac.build_client_from_env()


# --------------------------------------------------------------------------- #
# Wire contract: the agent's command routes
# --------------------------------------------------------------------------- #
def test_agent_engine_command_wire(monkeypatch):
    calls: list[dict] = []
    _install_fake_httpx(
        monkeypatch, calls, _FakeResp(json_data={"run_id": "r1"})
    )

    monkeypatch.setattr(ac, "get_adc_token", lambda: "tok")  # skip real ADC
    c = ac.AgentRuntimeClient(
        resource_id="projects/p/locations/us-central1/reasoningEngines/9"
    )

    result = _run(c.post_command("investigations/get", {"run_id": "r1"}))

    assert result == {"run_id": "r1"}
    # Container routes hang off the `reasoningEngines/v1` prefix and the
    # container's own `/api`. The standard `/v1` prefix answers 404.
    assert calls[0]["url"] == (
        "https://us-central1-aiplatform.googleapis.com/reasoningEngines/v1/"
        "projects/p/locations/us-central1/reasoningEngines/9/api/investigations/get"
    )
    assert calls[0]["json"] == {"run_id": "r1"}
    # The deployed route is behind the platform's own IAM check, so the call
    # carries the caller's bearer token like every other engine request.
    assert calls[0]["headers"]["Authorization"] == "Bearer tok"


def test_adk_http_command_wire(monkeypatch):
    calls: list[dict] = []
    _install_fake_httpx(monkeypatch, calls, _FakeResp(json_data={"config": {}}))

    c = ac.AdkHttpClient(base_url="http://localhost:8000")

    assert _run(c.post_command("config")) == {"config": {}}
    assert calls[0]["url"] == "http://localhost:8000/config"
    assert calls[0]["json"] == {}


def test_command_raises_on_a_non_2xx(monkeypatch):
    calls: list[dict] = []
    _install_fake_httpx(monkeypatch, calls, _FakeResp(status_code=404))

    c = ac.AdkHttpClient(base_url="http://localhost:8000")

    with pytest.raises(RuntimeError):
        _run(c.post_command("investigations/list"))
