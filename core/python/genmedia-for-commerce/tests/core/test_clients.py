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

import threading
import time
from typing import ClassVar

import pytest

from genmedia4commerce.core import clients

# Captured at collection time, before tests/conftest.py replaces
# clients.get_client with a guard that forbids real clients.
from genmedia4commerce.core.clients import RETRY_OPTIONS, reset_clients
from genmedia4commerce.core.clients import get_client as real_get_client
from genmedia4commerce.core.settings import get_settings


class RecordingClient:
    created: ClassVar[list[dict]] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        RecordingClient.created.append(kwargs)


@pytest.fixture
def stub_client(monkeypatch):
    RecordingClient.created = []
    monkeypatch.setattr(clients.genai, "Client", RecordingClient)
    monkeypatch.setenv("PROJECT_ID", "proj")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GLOBAL_REGION", raising=False)
    get_settings.cache_clear()
    reset_clients()
    yield RecordingClient
    reset_clients()


def test_retry_options_policy():
    assert RETRY_OPTIONS.attempts == 5
    assert RETRY_OPTIONS.initial_delay == 1.0
    assert RETRY_OPTIONS.exp_base == 2.0
    assert RETRY_OPTIONS.max_delay == 30.0
    assert RETRY_OPTIONS.http_status_codes == [408, 429, 500, 502, 503, 504]


def test_vertex_branch_default_location(stub_client):
    c = real_get_client()
    kw = c.kwargs
    assert kw["vertexai"] is True
    assert kw["project"] == "proj"
    assert kw["location"] == "global"
    assert kw["http_options"].retry_options == RETRY_OPTIONS
    assert kw["http_options"].headers == clients.tracking_headers()


def test_location_from_settings(stub_client, monkeypatch):
    monkeypatch.setenv("GLOBAL_REGION", "europe-west1")
    get_settings.cache_clear()
    assert real_get_client().kwargs["location"] == "europe-west1"


def test_explicit_location(stub_client):
    assert real_get_client(location="us-central1").kwargs["location"] == (
        "us-central1"
    )


def test_api_key_branch(stub_client, monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "secret")
    get_settings.cache_clear()
    kw = real_get_client().kwargs
    assert kw["api_key"] == "secret"
    assert "vertexai" not in kw
    assert kw["http_options"].retry_options == RETRY_OPTIONS
    assert kw["http_options"].headers == clients.tracking_headers()


def test_api_revision_header(stub_client):
    kw = real_get_client(api_revision="2026-05-20").kwargs
    assert kw["http_options"].headers == {
        **clients.tracking_headers(),
        "Api-Revision": "2026-05-20",
    }
    assert kw["http_options"].retry_options == RETRY_OPTIONS


def test_cached_per_key(stub_client):
    a = real_get_client()
    assert real_get_client() is a
    assert real_get_client(location="global") is a
    b = real_get_client(location="us-central1")
    c = real_get_client(api_revision="r1")
    assert b is not a and c is not a and c is not b
    assert real_get_client(api_revision="r1") is c
    assert len(stub_client.created) == 3


def test_concurrent_first_calls_share_one_client(stub_client, monkeypatch):
    # Without the lock both threads build a client and one is dropped (and
    # closed by genai.Client.__del__) while its caller still uses it.
    def slow_client(**kwargs):
        time.sleep(0.05)
        return RecordingClient(**kwargs)

    monkeypatch.setattr(clients.genai, "Client", slow_client)
    got = []
    threads = [
        threading.Thread(target=lambda: got.append(real_get_client()))
        for _ in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(stub_client.created) == 1
    assert all(c is got[0] for c in got)


def test_reset_clients(stub_client):
    a = real_get_client()
    reset_clients()
    assert real_get_client() is not a
    assert len(stub_client.created) == 2


def test_guard_blocks_real_client_in_unit_tests():
    with pytest.raises(AssertionError, match="real client in unit test"):
        clients.get_client()


def test_the_tracking_headers_name_the_recipe(monkeypatch):
    """The identifier the recipe has always sent, for usage counting."""
    monkeypatch.setattr(clients, "package_version", lambda name: "1.2.3")
    agent = "1.2.3-genmedia-for-commerce/1.2.3-genmedia-for-commerce"
    headers = clients.tracking_headers()
    assert headers["user-agent"] == agent
    python, _, rest = (
        headers["x-goog-api-client"].removeprefix(agent + " ").partition(" ")
    )
    assert python.startswith("gl-python/3.")
    assert rest == f"gccl/{agent}"


def test_the_tracking_headers_without_package_metadata(monkeypatch):
    def missing(name):
        raise clients.PackageNotFoundError(name)

    monkeypatch.setattr(clients, "package_version", missing)
    assert clients.tracking_headers()["user-agent"] == (
        "0.0.0-genmedia-for-commerce/0.0.0-genmedia-for-commerce"
    )


def test_the_sdk_keeps_the_tracking_headers_and_adds_its_label():
    """google-genai merges client headers and puts its own label in front,
    so every call carries the recipe's identifier and the SDK's."""
    from google.genai import _api_client, version

    headers = clients.tracking_headers()
    _api_client.append_library_version_headers(headers)
    sdk = f"google-genai-sdk/{version.__version__}"
    for name in ("user-agent", "x-goog-api-client"):
        assert headers[name].startswith(sdk)
        assert "genmedia-for-commerce" in headers[name]
