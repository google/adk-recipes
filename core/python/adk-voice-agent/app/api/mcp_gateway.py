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

"""Proxies `.mcp.json` upstreams as `<server>__<tool>` with server-held user tokens and `mcp_gates` approval checks."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import os
import shutil
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx

from app.api.mcp_outputs import gateway_output
from app.api.mcp_permissions import matching_rule, request_permission
from app.app_utils.http_client import get_http_client
from app.auth.provider_hooks import provider_hooks
from app.auth.providers import app_for_server, bearer_headers
from app.threads.registry import ThreadNotFound, get_thread_registry
from app.workers.policy import load_policy

logger = logging.getLogger(__name__)

MCP_JSON_PATH = Path(__file__).resolve().parents[2] / ".mcp.json"
TOOL_SEPARATOR = "__"
TOOLS_CACHE_TTL_S = 300.0
UPSTREAM_TIMEOUT_S = 30.0
_PROTOCOL_VERSION = "2025-03-26"
_CLIENT_INFO = {"name": "adk-voice-agent-gateway", "version": "1.0.0"}
# Only pass safe base vars to spawned stdio upstreams alongside credentials.
_STDIO_BASE_ENV = ("PATH", "HOME", "LANG", "TMPDIR")


class CallToolResult(dict[str, Any]):
    pass


class UpstreamError(RuntimeError):
    pass


@dataclass(frozen=True)
class Upstream:
    name: str
    url: str = ""
    command: str = ""
    args: tuple[str, ...] = ()

    def unavailable_reason(self) -> str | None:
        if self.url:
            return None
        if not self.command:
            return "it has neither a url nor a command"
        if shutil.which(self.command) is None:
            return f"`{self.command}` is not installed on this server"
        return None


@dataclass(frozen=True)
class Credentials:
    headers: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)


def load_upstreams(path: Path | None = None) -> dict[str, Upstream]:
    path = path or MCP_JSON_PATH
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    servers = raw.get("mcpServers") if isinstance(raw, dict) else None
    if not isinstance(servers, dict):
        return {}
    return {
        name: Upstream(
            name=name,
            url=str(spec.get("url") or "").strip(),
            command=str(spec.get("command") or "").strip(),
            args=tuple(str(a) for a in spec.get("args") or ()),
        )
        for name, spec in servers.items()
        if isinstance(spec, dict) and TOOL_SEPARATOR not in name
    }


async def upstream_credentials(upstream: Upstream) -> Credentials | None:
    app = app_for_server(upstream.name)
    if app is None:
        return None
    token = await provider_hooks(app.name).access_token(app.name)
    if not token:
        return None
    return Credentials(
        headers=bearer_headers(token), env={app.access_token_env: token}
    )


class UpstreamSession(Protocol):
    async def request(self, method: str, params: dict[str, Any]) -> Any: ...

    async def notify(self, method: str) -> None: ...


def _result_of(message: dict[str, Any]) -> Any:
    if error := message.get("error"):
        detail = error.get("message") if isinstance(error, dict) else error
        raise UpstreamError(str(detail))
    return message.get("result")


class _HttpSession:
    def __init__(self, client: httpx.AsyncClient, url: str) -> None:
        self._client, self._url = client, url
        self._ids = itertools.count(1)
        self._session_id = ""

    async def _post(self, payload: dict[str, Any]) -> httpx.Response:
        headers = {"Accept": "application/json, text/event-stream"}
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        try:
            response = await self._client.post(
                self._url, json=payload, headers=headers
            )
        except httpx.HTTPError as exc:
            raise UpstreamError(f"could not reach {self._url}: {exc}") from exc
        # Google's MCP servers send some JSON-RPC replies with an HTTP error status.
        if response.status_code >= 400 and not _carries_jsonrpc(response):
            raise UpstreamError(
                f"{self._url} answered HTTP {response.status_code}"
            )
        self._session_id = (
            response.headers.get("mcp-session-id") or self._session_id
        )
        return response

    async def request(self, method: str, params: dict[str, Any]) -> Any:
        req_id = next(self._ids)
        response = await self._post(
            {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        )
        for message in _messages(response):
            if message.get("id") == req_id:
                return _result_of(message)
        raise UpstreamError(f"{self._url} sent no reply to {method}")

    async def notify(self, method: str) -> None:
        await self._post({"jsonrpc": "2.0", "method": method})


def _messages(response: httpx.Response) -> list[dict[str, Any]]:
    if "text/event-stream" in response.headers.get("content-type", ""):
        found = []
        for line in response.text.splitlines():
            if line.startswith("data:"):
                with contextlib.suppress(ValueError):
                    found.append(json.loads(line[5:].strip()))
    else:
        try:
            body = response.json()
        except ValueError as exc:
            raise UpstreamError("the reply was not JSON") from exc
        found = body if isinstance(body, list) else [body]
    return [m for m in found if isinstance(m, dict)]


def _carries_jsonrpc(response: httpx.Response) -> bool:
    try:
        return any("jsonrpc" in m for m in _messages(response))
    except UpstreamError:
        return False


class _StdioSession:
    def __init__(self, proc: asyncio.subprocess.Process, name: str) -> None:
        self._proc, self._name = proc, name
        self._ids = itertools.count(1)

    async def _send(self, payload: dict[str, Any]) -> None:
        if self._proc.stdin is None:
            raise UpstreamError(f"{self._name} has no stdin")
        self._proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
        await self._proc.stdin.drain()

    async def request(self, method: str, params: dict[str, Any]) -> Any:
        req_id = next(self._ids)
        await self._send(
            {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        )
        if self._proc.stdout is None:
            raise UpstreamError(f"{self._name} has no stdout")
        while line := await self._proc.stdout.readline():
            try:
                message = json.loads(line)
            except ValueError:
                continue  # Ignore non-JSON-RPC log lines on stdout.
            if isinstance(message, dict) and message.get("id") == req_id:
                return _result_of(message)
        raise UpstreamError(f"{self._name} exited before replying to {method}")

    async def notify(self, method: str) -> None:
        await self._send({"jsonrpc": "2.0", "method": method})


@contextlib.asynccontextmanager
async def _transport(
    upstream: Upstream, creds: Credentials
) -> AsyncIterator[UpstreamSession]:
    if upstream.url:
        async with get_http_client(
            timeout=UPSTREAM_TIMEOUT_S, headers=creds.headers
        ) as client:
            yield _HttpSession(client, upstream.url)
        return
    env = {k: v for k in _STDIO_BASE_ENV if (v := os.environ.get(k))}
    proc = await asyncio.create_subprocess_exec(
        upstream.command,
        *upstream.args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env={**env, **creds.env},
    )
    try:
        yield _StdioSession(proc, upstream.name)
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()


@contextlib.asynccontextmanager
async def open_session(
    upstream: Upstream, creds: Credentials
) -> AsyncIterator[UpstreamSession]:
    async with _transport(upstream, creds) as session:
        await asyncio.wait_for(
            session.request(
                "initialize",
                {
                    "protocolVersion": _PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": _CLIENT_INFO,
                },
            ),
            UPSTREAM_TIMEOUT_S,
        )
        await session.notify("notifications/initialized")
        yield session


_tools_cache: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}


def clear_tools_cache() -> None:
    _tools_cache.clear()


async def _usable(upstream: Upstream) -> Credentials | None:
    if reason := upstream.unavailable_reason():
        logger.info("MCP gateway: omitting %s: %s", upstream.name, reason)
        return None
    return await upstream_credentials(upstream)


async def _upstream_tools(
    user_id: str, upstream: Upstream, creds: Credentials
) -> list[dict[str, Any]]:
    key = (user_id, upstream.name)
    cached = _tools_cache.get(key)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    async with open_session(upstream, creds) as session:
        result = await asyncio.wait_for(
            session.request("tools/list", {}), UPSTREAM_TIMEOUT_S
        )
    tools = [
        t
        for t in (result or {}).get("tools") or []
        if isinstance(t, dict) and isinstance(t.get("name"), str)
    ]
    _tools_cache[key] = (time.monotonic() + TOOLS_CACHE_TTL_S, tools)
    return tools


async def list_gateway_tools(user_id: str) -> list[dict[str, Any]]:
    listed: list[dict[str, Any]] = []
    for upstream in load_upstreams().values():
        creds = await _usable(upstream)
        if creds is None:
            continue
        try:
            tools = await _upstream_tools(user_id, upstream, creds)
        except (UpstreamError, TimeoutError, OSError) as exc:
            logger.warning(
                "MCP gateway: could not list %s: %s", upstream.name, exc
            )
            continue
        listed.extend(
            {
                "name": f"{upstream.name}{TOOL_SEPARATOR}{tool['name']}",
                "description": f"[{upstream.name}] {tool.get('description') or ''}".strip(),
                "inputSchema": tool.get("inputSchema")
                or {"type": "object", "properties": {}},
            }
            for tool in tools
        )
    return listed


def split_gateway_tool(name: str) -> tuple[Upstream, str] | None:
    server, sep, tool = name.partition(TOOL_SEPARATOR)
    upstream = load_upstreams().get(server) if sep and tool else None
    return (upstream, tool) if upstream else None


def gate_for(server: str, tool: str) -> str | None:
    return matching_rule(f"{server}.{tool}", load_policy().mcp_gates)


def _error(text: str) -> CallToolResult:
    return CallToolResult(
        content=[{"type": "text", "text": text}], isError=True
    )


async def call_gateway_tool(
    thread_id: str, name: str, arguments: dict[str, Any]
) -> CallToolResult:
    split = split_gateway_tool(name)
    if split is None:
        raise KeyError(f"Unknown MCP tool: {name}")
    upstream, tool = split
    creds = await _usable(upstream)
    if creds is None:
        return _error(
            f"{upstream.name} is not available: the user hasn't connected its app. "
            "They can connect it in Settings → Apps."
        )
    if rule := gate_for(upstream.name, tool):
        try:
            decision = await request_permission(
                thread_id, name, arguments, rule=rule
            )
        except ThreadNotFound:
            return _error(
                f"{name} needs the user's approval, and no thread can ask for it."
            )
        if not decision.allowed:
            return _error(decision.message)
    try:
        async with open_session(upstream, creds) as session:
            result = await asyncio.wait_for(
                session.request(
                    "tools/call", {"name": tool, "arguments": arguments}
                ),
                UPSTREAM_TIMEOUT_S,
            )
    except (UpstreamError, TimeoutError, OSError) as exc:
        logger.warning("MCP gateway: %s failed: %s", name, exc)
        return _error(f"{name} failed: {exc}")
    if not isinstance(result, dict):
        return CallToolResult(
            content=[{"type": "text", "text": json.dumps(result)}],
            isError=False,
        )
    passed = CallToolResult(
        (k, result[k])
        for k in ("content", "structuredContent", "isError")
        if k in result
    )
    passed.setdefault("content", [])
    passed.setdefault("isError", False)
    if output := gateway_output(upstream.name, tool, arguments, passed):
        await _record_output(thread_id, output)
    return passed


async def _record_output(thread_id: str, output: dict[str, str]) -> None:
    try:
        await get_thread_registry().record_output(thread_id, output)
    except Exception:
        logger.warning(
            "MCP gateway: could not record %s on %s",
            output,
            thread_id,
            exc_info=True,
        )
