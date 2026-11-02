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

"""Built-in Workspace MCP servers: stdio protocol, `.mcp.json` wiring, and `mcp_gates` annotations."""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from typing import Any

import httpx
import pytest

from app.api import mcp_gateway
from app.auth.providers import all_providers
from workspace_mcp import SERVERS
from workspace_mcp._google import ACCESS_TOKEN_ENV, GoogleApi
from workspace_mcp._server import Server

_SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "count": {"type": "integer"}},
    "required": ["name"],
}


def _demo_server() -> Server:
    server = Server("demo")

    @server.tool("greet", "Say hello.", _SCHEMA, read_only=True)
    async def greet(api: GoogleApi, args: dict[str, Any]) -> dict[str, Any]:
        return await api.get(f"https://example.test/hello/{args['name']}")

    return server


def _api(status: int = 200, body: Any = None) -> GoogleApi:
    def reply(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer fake-token"
        payload = body if body is not None else {"hello": request.url.path}
        return httpx.Response(status, json=payload)

    return GoogleApi("fake-token", transport=httpx.MockTransport(reply))


def _rpc(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 7, "method": method, "params": params or {}}


@pytest.mark.asyncio
async def test_initialize_ping_and_list() -> None:
    server = _demo_server()
    init = await server.handle(_rpc("initialize"))
    assert init is not None
    assert init["result"]["serverInfo"]["name"] == "demo"
    assert "tools" in init["result"]["capabilities"]
    assert (
        await server.handle(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        is None
    )
    assert (await server.handle(_rpc("ping")))["result"] == {}  # type: ignore[index]
    listed = await server.handle(_rpc("tools/list"))
    (tool,) = listed["result"]["tools"]  # type: ignore[index]
    assert tool["name"] == "greet"
    assert tool["inputSchema"] == _SCHEMA
    assert tool["annotations"] == {
        "readOnlyHint": True,
        "destructiveHint": False,
        "openWorldHint": False,
    }


@pytest.mark.asyncio
async def test_a_call_returns_structured_content_and_json_text() -> None:
    reply = await _demo_server().handle(
        _rpc("tools/call", {"name": "greet", "arguments": {"name": "ada"}}),
        api=_api(),
    )
    result = reply["result"]  # type: ignore[index]
    assert result["isError"] is False
    assert result["structuredContent"] == {"hello": "/hello/ada"}
    assert json.loads(result["content"][0]["text"]) == {"hello": "/hello/ada"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({}, "missing required argument(s): name"),
        ({"name": "ada", "count": "3"}, "`count` should be a integer"),
        ({"name": "ada", "count": True}, "`count` should be a integer"),
    ],
)
async def test_bad_arguments_are_an_error_result(
    arguments: dict[str, Any], expected: str
) -> None:
    result = await _demo_server().call_tool("greet", arguments, api=_api())
    assert result["isError"] is True
    assert expected in result["content"][0]["text"]
    assert "structuredContent" not in result


@pytest.mark.asyncio
async def test_a_google_error_tells_the_agent_what_to_do() -> None:
    result = await _demo_server().call_tool(
        "greet", {"name": "ada"}, api=_api(401, {"error": {"message": "x"}})
    )
    assert result["isError"] is True
    assert "reconnect Google in Settings → Apps" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_no_token_in_the_environment_is_an_error_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ACCESS_TOKEN_ENV, raising=False)
    result = await _demo_server().call_tool("greet", {"name": "ada"})
    assert result["isError"] is True
    assert "reconnect Google" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_unknown_tools_and_methods() -> None:
    server = _demo_server()
    result = await server.call_tool("nope", {}, api=_api())
    assert result["isError"] is True
    reply = await server.handle(_rpc("resources/list"))
    assert reply["error"]["code"] == -32601  # type: ignore[index]


def test_mcp_json_runs_each_builtin_server_with_the_venv_python() -> None:
    upstreams = mcp_gateway.load_upstreams()
    for name, module in SERVERS.items():
        assert upstreams[name].command == "python", name
        assert upstreams[name].args == ("-m", module), name


@pytest.mark.parametrize(("name", "module"), sorted(SERVERS.items()))
def test_each_module_serves_its_mcp_json_name(name: str, module: str) -> None:
    assert importlib.import_module(module).server.name == name


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "module"), sorted(SERVERS.items()))
async def test_the_gateway_spawns_each_server_over_stdio(
    name: str, module: str
) -> None:
    upstream = mcp_gateway.Upstream(
        name=name, command=sys.executable, args=("-m", module)
    )
    creds = mcp_gateway.Credentials(env={ACCESS_TOKEN_ENV: "fake-token"})
    async with mcp_gateway.open_session(upstream, creds) as session:
        listed = await session.request("tools/list", {})
    expected = importlib.import_module(module).server.tools
    assert [t["name"] for t in listed["tools"]] == list(expected)


def test_the_servers_never_import_the_app() -> None:
    code = (
        "import sys, importlib\n"
        f"for m in {sorted(SERVERS.values())!r}: importlib.import_module(m)\n"
        "print(sorted(m for m in sys.modules if m == 'app' or m.startswith('app.')))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == "[]"


@pytest.mark.parametrize(("name", "module"), sorted(SERVERS.items()))
def test_tools_that_reach_people_or_destroy_things_are_gated(
    name: str, module: str
) -> None:
    for tool in importlib.import_module(module).server.tools.values():
        hints = tool.annotations
        risky = hints["openWorldHint"] or hints["destructiveHint"]
        gate = mcp_gateway.gate_for(name, tool.name)
        if risky:
            assert gate, f"{name}.{tool.name} needs an mcp_gates rule"
        if hints["readOnlyHint"]:
            assert not gate, f"read-only {name}.{tool.name} is gated by {gate}"
            assert not risky, f"{name}.{tool.name}: read-only yet risky"


def test_the_token_env_is_the_one_the_gateway_sets() -> None:
    google = all_providers()["google_workspace"]
    assert ACCESS_TOKEN_ENV == google.access_token_env
    assert set(SERVERS) <= set(google.mcp_servers)


def test_system_2_evals_keep_the_hosted_urls_their_fakes_answer() -> None:
    from tests.eval.system2_agent.fake_apps import HOSTS
    from tests.eval.system2_agent.runner import HOSTED_MCP_JSON

    hosted = mcp_gateway.load_upstreams(HOSTED_MCP_JSON)
    assert set(hosted) == set(mcp_gateway.load_upstreams())
    for name, upstream in hosted.items():
        assert HOSTS.get(httpx.URL(upstream.url).host) == name, name
