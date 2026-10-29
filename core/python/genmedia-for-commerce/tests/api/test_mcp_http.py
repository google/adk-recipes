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

"""The Studio MCP App over stateless streamable HTTP at ``/mcp``."""

import json

import pytest
from fastapi.testclient import TestClient

from genmedia4commerce.mcp import studio

HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}
MODEL_TOOLS = {"studio_agent"}  # the only tool Gemini Enterprise's model sees
APP_TOOLS = {
    "studio_event",
    "studio_state",
    "studio_preview",
    "studio_gallery",
    "studio_upload",
    "studio_catalog_search",
    "studio_job_start",
    "studio_job_status",
}


@pytest.fixture
def client(monkeypatch):
    """A fresh app with its lifespan (the MCP session manager) running."""
    from genmedia4commerce.api import app as app_module

    with TestClient(app_module.create_app()) as test_client:
        yield test_client


def _rpc(client: TestClient, path: str, method: str, params: dict) -> dict:
    """One JSON-RPC call; the body is JSON or one SSE message."""
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    # No redirects: /mcp itself must answer (hosts do not follow a 307).
    response = client.post(
        path, json=body, headers=HEADERS, follow_redirects=False
    )
    assert response.status_code == 200, response.text
    if response.headers["content-type"].startswith("text/event-stream"):
        data = [
            line.removeprefix("data:").strip()
            for line in response.text.splitlines()
            if line.startswith("data:")
        ]
        message = json.loads(data[-1])
    else:
        message = response.json()
    assert "error" not in message, message
    return message["result"]


def _initialize(client: TestClient, path: str) -> None:
    result = _rpc(
        client,
        path,
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    )
    assert result["serverInfo"]["name"] == "GenMedia Studio"


@pytest.mark.parametrize("path", ["/mcp", "/mcp/"])
def test_tools_are_listed_with_their_visibility(client, path):
    _initialize(client, path)
    tools = _rpc(client, path, "tools/list", {})["tools"]
    visibility = {t["name"]: t["_meta"]["ui"]["visibility"] for t in tools}
    assert set(visibility) == MODEL_TOOLS | APP_TOOLS
    assert len(tools) == 9
    for name, seen in visibility.items():
        assert seen == (["model", "app"] if name in MODEL_TOOLS else ["app"])
    for tool in tools:
        assert tool["_meta"]["ui"]["resourceUri"] == studio.STUDIO_URI
        assert tool["annotations"]["readOnlyHint"] is True
    # The app sends actions, never its whole state.
    schema = {t["name"]: t["inputSchema"]["properties"] for t in tools}
    assert set(schema["studio_event"]) == {"studio_id", "event"}
    assert set(schema["studio_agent"]) == {"message", "studio_id"}


@pytest.mark.parametrize("path", ["/mcp", "/mcp/"])
def test_the_studio_resource_is_served(client, path):
    _initialize(client, path)
    result = _rpc(client, path, "resources/read", {"uri": studio.STUDIO_URI})
    (content,) = result["contents"]
    assert content["mimeType"] == "text/html;profile=mcp-app"
    assert content["text"] == studio._HTML.read_text(encoding="utf-8")


def test_mcp_responses_have_no_web_app_headers(client):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}

    response = client.post("/mcp", json=body, headers=HEADERS)

    assert response.status_code == 200
    assert "content-security-policy" not in response.headers
    assert "x-frame-options" not in response.headers


def test_browser_calls_from_other_sites_are_refused(client):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    headers = {**HEADERS, "Origin": "https://evil.example"}

    assert client.post("/mcp", json=body, headers=headers).status_code == 403
