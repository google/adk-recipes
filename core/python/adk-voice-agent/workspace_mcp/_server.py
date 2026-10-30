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

"""Minimal stdio MCP JSON-RPC server for built-in Workspace modules."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from workspace_mcp._google import GoogleApi, GoogleApiError

logger = logging.getLogger("workspace_mcp")

PROTOCOL_VERSION = "2025-03-26"
_JSON_TYPES: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}

Handler = Callable[[GoogleApi, dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Handler
    annotations: dict[str, bool] = field(default_factory=dict)

    def listing(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": self.annotations,
        }

    def check(self, arguments: dict[str, Any]) -> None:
        properties = self.input_schema.get("properties") or {}
        missing = [
            k
            for k in self.input_schema.get("required") or ()
            if arguments.get(k) in (None, "")
        ]
        if missing:
            raise ValueError(
                f"missing required argument(s): {', '.join(missing)}"
            )
        for key, value in arguments.items():
            expected = _JSON_TYPES.get(
                (properties.get(key) or {}).get("type", "")
            )
            if value is None or expected is None:
                continue
            if isinstance(value, bool) and expected is not bool:
                ok = False
            else:
                ok = isinstance(value, expected)
            if not ok:
                raise ValueError(
                    f"`{key}` should be a {properties[key]['type']}, "
                    f"not {type(value).__name__}"
                )


def text_result(data: Any, *, is_error: bool = False) -> dict[str, Any]:
    text = (
        data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    )
    result: dict[str, Any] = {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
    }
    if isinstance(data, dict) and not is_error:
        result["structuredContent"] = data
    return result


class Server:
    def __init__(self, name: str, version: str = "0.1.0") -> None:
        self.name, self.version = name, version
        self.tools: dict[str, Tool] = {}

    def tool(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        *,
        read_only: bool = False,
        destructive: bool = False,
        open_world: bool = False,
    ) -> Callable[[Handler], Handler]:
        """Register a tool (`open_world` or `destructive` tools require an `mcp_gates` rule)."""

        def register(handler: Handler) -> Handler:
            self.tools[name] = Tool(
                name=name,
                description=description,
                input_schema=input_schema,
                handler=handler,
                annotations={
                    "readOnlyHint": read_only,
                    "destructiveHint": destructive,
                    "openWorldHint": open_world,
                },
            )
            return handler

        return register

    async def call_tool(
        self, name: str, arguments: dict[str, Any], api: GoogleApi | None = None
    ) -> dict[str, Any]:
        tool = self.tools.get(name)
        if tool is None:
            return text_result(f"unknown tool: {name}", is_error=True)
        try:
            tool.check(arguments)
            if api is not None:
                return text_result(await tool.handler(api, arguments))
            async with GoogleApi.from_env() as env_api:
                return text_result(await tool.handler(env_api, arguments))
        except (GoogleApiError, ValueError) as exc:
            return text_result(str(exc), is_error=True)
        except Exception as exc:
            logger.exception("%s.%s failed", self.name, name)
            return text_result(
                f"{name} failed unexpectedly ({type(exc).__name__}): {exc}",
                is_error=True,
            )

    async def handle(
        self, message: dict[str, Any], api: GoogleApi | None = None
    ) -> dict[str, Any] | None:
        method, req_id = message.get("method"), message.get("id")
        params = message.get("params") or {}
        if req_id is None:
            return None
        if method == "initialize":
            result: Any = {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": self.name, "version": self.version},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [t.listing() for t in self.tools.values()]}
        elif method == "tools/call":
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                arguments = {}
            result = await self.call_tool(
                str(params.get("name")), arguments, api
            )
        else:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32601,
                    "message": f"method not found: {method}",
                },
            }
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    async def _serve(self) -> None:
        loop = asyncio.get_running_loop()
        stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
        while line := await loop.run_in_executor(None, stdin.readline):
            try:
                message = json.loads(line)
            except ValueError:
                reply: dict[str, Any] | None = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "parse error"},
                }
            else:
                reply = (
                    await self.handle(message)
                    if isinstance(message, dict)
                    else None
                )
            if reply is not None:
                stdout.write((json.dumps(reply) + "\n").encode("utf-8"))
                stdout.flush()

    def run(self) -> None:
        logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
        asyncio.run(self._serve())
