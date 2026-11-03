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

"""Attach A2A (Agent2Agent) endpoints to the FastAPI app.

func:`attach_a2a_routes` registers the dynamic
agent-card endpoint and the JSON-RPC endpoint so the same app serves A2A
alongside the adk_api routes, reachable by A2A clients and Gemini Enterprise A2A
registration.
"""

from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import (
    add_a2a_routes_to_fastapi,
    create_agent_card_routes,
    create_jsonrpc_routes,
    create_rest_routes,
)
from a2a.server.routes.common import DefaultServerCallContextBuilder
from a2a.server.tasks import TaskStore
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentExtension,
    AgentInterface,
)
from a2a.utils.constants import (
    AGENT_CARD_WELL_KNOWN_PATH,
    PROTOCOL_VERSION_0_3,
    PROTOCOL_VERSION_1_0,
    TransportProtocol,
)
from google.adk.a2a.executor.a2a_agent_executor import A2aAgentExecutor
from google.adk.a2a.utils.agent_card_builder import AgentCardBuilder
from starlette.requests import Request
from starlette.routing import BaseRoute, Mount, Route


class _A2AServerCallContextBuilder(DefaultServerCallContextBuilder):
    """Context builder that ensures A2A-Version defaults correctly when missing.

    Proxy infrastructure (e.g. Google Cloud API Gateways) can strip custom HTTP headers
    like 'A2A-Version'. This builder attempts to infer A2A-version from the method name
    when the header is missing.
    """

    def build(self, request):
        context = super().build(request)
        headers = context.state.setdefault("headers", {})
        existing_version = (
            headers.get("A2A-Version")
            or headers.get("a2a-version")
            or headers.get("x-a2a-version")
            or headers.get("X-A2A-Version")
        )
        if existing_version:
            headers["A2A-Version"] = existing_version
            return context

        # 0.3 uses method names that include a '/' like "message/send"
        # 1.0 uses PascalCase like "SendMessage"
        json_body = getattr(request, "_json", {}) or {}
        method = (
            json_body.get("method") if isinstance(json_body, dict) else None
        )

        # The v0.3 HTTP+JSON routes live under /v1/ (e.g. /v1/message:send).
        path = str(getattr(getattr(request, "url", None), "path", ""))
        if (method and "/" in str(method)) or "/v1/" in path:
            headers["A2A-Version"] = "0.3"
        else:
            headers["A2A-Version"] = "1.0"

        return context


if TYPE_CHECKING:
    from fastapi import FastAPI
    from google.adk.agents import BaseAgent
    from google.adk.runners import Runner

# URI advertised on the agent card describing the executor extension shipped
# by ADK. Kept as a module-level constant so callers can override or extend
# the capabilities list when needed.
_ADK_AGENT_EXECUTOR_EXTENSION_URI = (
    "https://google.github.io/adk-docs/a2a/a2a-extension/"
)


def _blocking_by_default(
    endpoint: Callable[[Request], Awaitable[Any]],
) -> Callable[[Request], Awaitable[Any]]:
    """Makes a v0.3 HTTP+JSON message:send wait for the answer by default.

    In the v0.3 REST schema ``configuration.blocking`` is a proto bool that
    defaults to false, so a request without a configuration (the form shown
    in the Gemini agent / Agent Registry docs) would get back a just-submitted
    task with no answer. JSON-RPC 0.3 and the 1.0 bindings already block by
    default; this aligns the v0.3 REST route with them. An explicit
    ``"blocking": false`` from the client is respected.
    """

    async def wrapper(request: Request) -> Any:
        body = await request.body()
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            return await endpoint(request)  # let the handler report the error
        if isinstance(data, dict):
            config = data.setdefault("configuration", {})
            if isinstance(config, dict):
                config.setdefault("blocking", True)
            # Starlette caches the body on the request; the handler reads it
            # again with request.body().
            request._body = json.dumps(data).encode()
        return await endpoint(request)

    return wrapper


def _with_blocking_v0_3_send(routes: list[BaseRoute]) -> list[BaseRoute]:
    out: list[BaseRoute] = []
    for route in routes:
        if isinstance(route, Route) and route.path.endswith("/v1/message:send"):
            out.append(
                Route(
                    route.path,
                    endpoint=_blocking_by_default(route.endpoint),
                    methods=list(route.methods or []),
                )
            )
        else:
            out.append(route)
    return out


async def _add_v0_3_compat_interface(card: AgentCard) -> AgentCard:
    """Advertise a v0.3 JSON-RPC interface so the served card stays consumable by
    v0.3 A2A clients — notably Gemini Enterprise registration, whose validator
    still requires the 0.3 card shape (top-level ``url``/``protocolVersion``)."""
    # The card modifier runs on every card request: only add the interface once.
    if card.supported_interfaces and not any(
        i.protocol_binding == "JSONRPC" and i.protocol_version == "0.3"
        for i in card.supported_interfaces
    ):
        card.supported_interfaces.append(
            AgentInterface(
                protocol_binding="JSONRPC",
                protocol_version="0.3",
                url=card.supported_interfaces[0].url,
            )
        )
    return card


def _default_capabilities() -> AgentCapabilities:
    """Returns the default A2A capabilities used by scaffolded projects."""
    return AgentCapabilities(
        streaming=True,
        # Required for GET /v1/card on the HTTP+JSON binding, which serves
        # the (here: public) extended card.
        extended_agent_card=True,
        extensions=[
            AgentExtension(
                uri=_ADK_AGENT_EXECUTOR_EXTENSION_URI,
                description=(
                    "Ability to use the new agent executor implementation"
                ),
            ),
        ],
    )


async def attach_a2a_routes(
    app: FastAPI,
    *,
    agent: BaseAgent,
    runner: Runner,
    task_store: TaskStore,
    rpc_path: str,
    capabilities: AgentCapabilities | None = None,
    agent_version: str | None = None,
    app_url: str | None = None,
) -> None:
    """Register A2A routes (JSON-RPC, HTTP+JSON and agent card) under ``rpc_path``.

    Builds a dynamic agent card from ``agent`` and mounts the routes on ``app``.
    The ``runner`` should share the session/artifact/memory services with the
    standard ADK path. ``capabilities``, ``agent_version``, and ``app_url``
    override their defaults (streaming + ADK extension, ``AGENT_VERSION``,
    ``APP_URL``). Call once per app — typically in a FastAPI ``lifespan``, since
    the card is built asynchronously; repeated calls register duplicate routes.
    """
    resolved_app_url = app_url or os.getenv("APP_URL")
    resolved_agent_version = agent_version or os.getenv("AGENT_VERSION")
    resolved_capabilities = capabilities or _default_capabilities()

    agent_card = await AgentCardBuilder(
        agent=agent,
        capabilities=resolved_capabilities,
        rpc_url=f"{resolved_app_url}{rpc_path}",
        agent_version=resolved_agent_version,
    ).build()

    # Also serve the HTTP+JSON (REST) binding under the same base URL. The
    # Gemini agent reaches A2A agents through Agent Registry over HTTP+JSON
    # (GET /v1/card, POST /v1/message:send, POST /v1/message:stream), which is
    # the v0.3 REST surface; the 1.0 REST routes (/message:send, ...) come
    # with it.
    rest_url = f"{resolved_app_url}{rpc_path}"
    for version in (PROTOCOL_VERSION_1_0, PROTOCOL_VERSION_0_3):
        agent_card.supported_interfaces.append(
            AgentInterface(
                protocol_binding=TransportProtocol.HTTP_JSON.value,
                protocol_version=version,
                url=rest_url,
            )
        )

    request_handler = DefaultRequestHandler(
        agent_executor=A2aAgentExecutor(runner=runner),
        task_store=task_store,
        agent_card=agent_card,
        # GET /v1/card serves the "extended" card; the agent has no
        # authenticated-only details, so it is the public card.
        extended_agent_card=agent_card,
    )
    rest_routes = _with_blocking_v0_3_send(
        [
            route
            for route in create_rest_routes(
                request_handler,
                context_builder=_A2AServerCallContextBuilder(),
                enable_v0_3_compat=True,
                path_prefix=rpc_path,
            )
            # Drop the multi-tenant `/{tenant}/...` mount: it would match any
            # two-segment path on this app.
            if not isinstance(route, Mount)
        ]
    )

    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(
            agent_card,
            card_modifier=_add_v0_3_compat_interface,
            card_url=f"{rpc_path}{AGENT_CARD_WELL_KNOWN_PATH}",
        ),
        jsonrpc_routes=create_jsonrpc_routes(
            request_handler,
            rpc_url=rpc_path,
            context_builder=_A2AServerCallContextBuilder(),
            enable_v0_3_compat=True,
        ),
        rest_routes=rest_routes,
    )
