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

"""DNS-rebinding guard for local runs that bind to 0.0.0.0 rather than loopback."""

from __future__ import annotations

import os

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.auth.providers import is_remote_deployment

LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]"})
# Docker containers reach System 1 via `host.docker.internal`; allowed only on `/api/v1/mcp`.
DOCKER_MCP_HOST = "host.docker.internal"


def _is_mcp_path(path: str) -> bool:
    return path == "/api/v1/mcp" or path.startswith("/api/v1/mcp/")


def allowed_hosts() -> frozenset[str]:
    extra = {
        h.strip().lower()
        for h in os.getenv("VOICE_AGENT_ALLOWED_HOSTS", "").split(",")
        if h.strip()
    }
    return LOCAL_HOSTS | extra


def host_name(header: str) -> str:
    header = header.strip().lower()
    if header.startswith("["):
        return header.split("]", 1)[0] + "]"
    return header.rsplit(":", 1)[0] if ":" in header else header


class LocalHostGuard:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        if scope["type"] not in {"http", "websocket"} or is_remote_deployment():
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        host = host_name(headers.get(b"host", b"").decode("latin-1"))
        path = scope.get("path", "")
        if host in allowed_hosts() or (
            host == DOCKER_MCP_HOST and _is_mcp_path(path)
        ):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        response = PlainTextResponse(
            "Host not allowed. Add it to VOICE_AGENT_ALLOWED_HOSTS to serve this name.",
            status_code=400,
        )
        await response(scope, receive, send)
