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

"""Every engine client factory carries the usage analytics product token.

Clients are built on anonymous credentials and never send a request.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import google.auth
import pytest
from ambient_quality_agent import clients
from ambient_quality_shared import __version__, usage_headers
from ambient_quality_shared.usage_headers import ENGINE_ID_ENV
from google.auth.credentials import AnonymousCredentials
from google.genai import types

_TOKEN = f"google-agents-cli-aqua/{__version__}+local"


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch, usage_analytics_on) -> None:
    """Pins the engine suffix to `+local` and hands out anonymous
    credentials."""
    monkeypatch.delenv(ENGINE_ID_ENV, raising=False)
    monkeypatch.setattr(
        google.auth,
        "default",
        lambda *a, **k: (AnonymousCredentials(), "test-project"),
    )


def test_bigquery_client_sends_the_token() -> None:
    client = clients.create_bigquery_client("test-project", "US")

    assert client.location == "US"
    assert client._connection.user_agent.startswith(f"{_TOKEN} ")
    assert "gccl/" in client._connection.user_agent


def test_storage_client_sends_the_token() -> None:
    client = clients.create_storage_client("test-project")

    assert client.project == "test-project"
    assert client._connection.user_agent.startswith(f"{_TOKEN} ")


def test_genai_client_sends_the_token_and_keeps_its_http_options() -> None:
    retry = types.HttpRetryOptions(attempts=4)

    client = clients.create_genai_client(
        project="test-project",
        location="us-central1",
        retry_options=retry,
        timeout_ms=60_000,
    )

    options = client._api_client._http_options
    assert options.retry_options == retry
    assert options.timeout == 60_000
    assert options.headers["user-agent"].endswith(_TOKEN)
    assert options.headers["x-goog-api-client"].endswith(_TOKEN)


def test_agentplatform_client_sends_the_token() -> None:
    client = clients.create_agentplatform_client(
        project="test-project", location="us-central1"
    )

    headers = client._api_client._http_options.headers
    assert headers["user-agent"].endswith(_TOKEN)
    assert headers["x-goog-api-client"].endswith(_TOKEN)


class _Recorder:
    """Stands in for a gRPC client class, recording its keyword arguments."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


def test_cloud_tasks_client_keeps_the_library_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from google.cloud import tasks_v2
    from google.cloud.tasks_v2.services.cloud_tasks.transports.base import (
        DEFAULT_CLIENT_INFO,
    )

    monkeypatch.setattr(tasks_v2, "CloudTasksClient", _Recorder)

    client_info = clients.create_cloud_tasks_client().kwargs["client_info"]

    assert client_info.user_agent == _TOKEN
    assert client_info.gapic_version == DEFAULT_CLIENT_INFO.gapic_version
    assert DEFAULT_CLIENT_INFO.user_agent is None


def test_logging_client_keeps_the_library_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from google.cloud.logging_v2.services import logging_service_v2
    from google.cloud.logging_v2.services.logging_service_v2.transports.base import (
        DEFAULT_CLIENT_INFO,
    )

    monkeypatch.setattr(logging_service_v2, "LoggingServiceV2Client", _Recorder)

    client_info = clients.create_logging_client().kwargs["client_info"]

    assert client_info.user_agent == _TOKEN
    assert client_info.gapic_version == DEFAULT_CLIENT_INFO.gapic_version
    assert DEFAULT_CLIENT_INFO.user_agent is None


def test_adk_model_calls_send_the_token_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADK sets per-request headers through `_merge_tracking_headers`, and
    they replace the client's, so the token must survive that merge. Checked
    on the wire, which also catches ADK renaming either private hook."""
    import asyncio

    import httpx
    from ambient_quality_agent.core.models import LocatedGemini
    from google.adk.models.llm_request import LlmRequest
    from google.genai._api_client import BaseApiClient
    from google.oauth2.credentials import Credentials

    from .conftest import make_config

    monkeypatch.setattr("ambient_quality_agent.config.config", make_config())
    # The SDK switches to aiohttp on hosts with a client certificate; pin it
    # to httpx so the patched `send` below sees every request.
    monkeypatch.setattr(BaseApiClient, "_use_aiohttp", lambda self: False)
    monkeypatch.setattr(
        google.auth,
        "default",
        lambda *a, **k: (Credentials(token="fake-token"), "test-project"),
    )
    sent: list[httpx.Request] = []

    async def _send(self: httpx.AsyncClient, request: httpx.Request, **_: Any):
        sent.append(request)
        reply = {
            "candidates": [
                {"content": {"role": "model", "parts": [{"text": "ok"}]}}
            ]
        }
        return httpx.Response(200, json=reply, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", _send)
    model = LocatedGemini(model="gemini-3.5-flash", location="us-central1")
    request = LlmRequest(
        model="gemini-3.5-flash",
        contents=[types.Content(role="user", parts=[types.Part(text="hi")])],
        config=types.GenerateContentConfig(),
    )

    async def _call() -> None:
        async for _ in model.generate_content_async(request):
            pass

    asyncio.run(_call())

    assert len(sent) == 1
    for name in ("user-agent", "x-goog-api-client"):
        tokens = sent[0].headers[name].split(" ")
        assert tokens.count(_TOKEN) == 1, sent[0].headers[name]
        assert any(token.startswith("google-adk/") for token in tokens)


def test_the_switch_off_leaves_every_client_as_the_library_builds_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ambient_quality_agent.core.models import LocatedGemini
    from google.adk.models import Gemini
    from google.cloud import bigquery, storage, tasks_v2
    from google.cloud.logging_v2.services import logging_service_v2

    monkeypatch.setattr(usage_headers, "USAGE_ANALYTICS_ENABLED", False)
    monkeypatch.setattr(tasks_v2, "CloudTasksClient", _Recorder)
    monkeypatch.setattr(logging_service_v2, "LoggingServiceV2Client", _Recorder)

    assert clients.build_client_info().user_agent is None
    assert (
        clients.create_bigquery_client(
            "test-project", "US"
        )._connection.user_agent
        == bigquery.Client(project="test-project")._connection.user_agent
    )
    assert (
        clients.create_storage_client("test-project")._connection.user_agent
        == storage.Client(project="test-project")._connection.user_agent
    )
    for client in (
        clients.create_genai_client(
            project="test-project", location="us-central1"
        ),
        clients.create_agentplatform_client(
            project="test-project", location="us-central1"
        ),
    ):
        headers = client._api_client._http_options.headers
        for name in ("user-agent", "x-goog-api-client"):
            assert usage_headers.PRODUCT_NAME not in headers[name]
    for client in (
        clients.create_cloud_tasks_client(),
        clients.create_logging_client(),
    ):
        assert client.kwargs["client_info"].user_agent is None
    model = LocatedGemini(model="gemini-3.5-flash", location="us-central1")
    adk = Gemini(model="gemini-3.5-flash")
    assert model._tracking_headers() == adk._tracking_headers()
    assert model._merge_tracking_headers({}) == adk._merge_tracking_headers({})


# --- guard: no client is constructed outside a factory ------------------------ #

_REPO = Path(__file__).resolve().parent.parent
_GUARDED_ROOTS = ("src", "ui", "extension")
_CLIENT_MODULES = frozenset(
    {"bigquery", "storage", "genai", "agentplatform", "logging_v2"}
)
_CLIENT_CLASSES = frozenset(
    {
        "CloudTasksClient",
        "LoggingServiceV2Client",
        "GcsArtifactService",
        "VertexAiSessionService",
    }
)
_FACTORY_MODULES = frozenset(
    {
        "src/ambient_quality_agent/clients.py",
        "src/ambient_quality_agent/core/models.py",
        "src/ambient_quality_agent/app_utils/services.py",
        "ui/task_reader.py",
    }
)


def _list_direct_constructions(source: str) -> list[int]:
    """Lists the lines that construct a guarded Google Cloud client.

    Args:
        source: Python source text.

    Returns:
        Line numbers of each call to a class in `_CLIENT_CLASSES`, and of each
        `Client` of a module in `_CLIENT_MODULES`, called as `bigquery.Client(`
        or as `Client(` after `from google.cloud.bigquery import Client`.
    """
    tree = ast.parse(source)
    imported_clients = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and _CLIENT_MODULES.intersection((node.module or "").split("."))
        for alias in node.names
        if alias.name == "Client"
    }
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else ""
        name = func.id if isinstance(func, ast.Name) else name
        owner = func.value if isinstance(func, ast.Attribute) else None
        if (
            name in _CLIENT_CLASSES
            or (isinstance(func, ast.Name) and name in imported_clients)
            or (
                name == "Client"
                and isinstance(owner, ast.Name)
                and owner.id in _CLIENT_MODULES
            )
        ):
            lines.append(node.lineno)
    return lines


def test_the_guard_recognizes_every_client() -> None:
    source = (
        "bigquery.Client()\nstorage.Client(project='p')\ngenai.Client()\n"
        "agentplatform.Client()\nlogging_v2.Client()\n"
        "tasks_v2.CloudTasksClient()\n"
        "LoggingServiceV2Client()\nGcsArtifactService(bucket_name='b')\n"
        "VertexAiSessionService()\nhttpx.Client()\n"
        "clients.create_storage_client()\n"
    )

    assert _list_direct_constructions(source) == [1, 2, 3, 4, 5, 6, 7, 8, 9]


def test_the_guard_recognizes_a_client_imported_by_name() -> None:
    source = (
        "from google.genai import Client, types\n"
        "from google.cloud.storage import Client as GcsClient\n"
        "from httpx import Client as HttpClient\n"
        "Client()\nGcsClient()\nHttpClient()\nhttpx.Client()\n"
    )

    assert _list_direct_constructions(source) == [4, 5]


def test_clients_are_built_only_by_the_factories() -> None:
    """A client built anywhere else would send no usage analytics token."""
    offenders = []
    for root in _GUARDED_ROOTS:
        for path in sorted((_REPO / root).rglob("*.py")):
            relative = path.relative_to(_REPO).as_posix()
            if relative in _FACTORY_MODULES or "node_modules" in path.parts:
                continue
            source = path.read_text(encoding="utf-8")
            offenders += [
                f"{relative}:{line}"
                for line in _list_direct_constructions(source)
            ]

    assert offenders == [], (
        "Build these through a factory module: " + ", ".join(offenders)
    )
