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

"""Attach A2A (Agent2Agent) endpoints to the FastAPI app."""

from __future__ import annotations

import asyncio
import concurrent.futures
from typing import TYPE_CHECKING

from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import (
    add_a2a_routes_to_fastapi,
    create_agent_card_routes,
    create_jsonrpc_routes,
)
from a2a.server.routes.common import DefaultServerCallContextBuilder
from a2a.server.tasks import TaskStore
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentExtension,
    AgentInterface,
)
from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH
from google.adk.a2a.executor.a2a_agent_executor import A2aAgentExecutor
from google.adk.a2a.utils.agent_card_builder import AgentCardBuilder

from app.config import config


class _A2AServerCallContextBuilder(DefaultServerCallContextBuilder):
    """Context builder that ensures A2A-Version defaults correctly when missing."""

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

        json_body = getattr(request, "_json", {}) or {}
        method = (
            json_body.get("method") if isinstance(json_body, dict) else None
        )

        if method and "/" in str(method):
            headers["A2A-Version"] = "0.3"
        else:
            headers["A2A-Version"] = "1.0"

        return context


if TYPE_CHECKING:
    from fastapi import FastAPI
    from google.adk.agents import BaseAgent
    from google.adk.runners import Runner

_ADK_AGENT_EXECUTOR_EXTENSION_URI = (
    "https://google.github.io/adk-docs/a2a/a2a-extension/"
)


async def _add_v0_3_compat_interface(card: AgentCard) -> AgentCard:
    """Advertise a v0.3 JSON-RPC interface for v0.3 A2A clients."""
    if card.supported_interfaces and not any(
        i.protocol_version == "0.3" for i in card.supported_interfaces
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
    return AgentCapabilities(
        streaming=True,
        extensions=[
            AgentExtension(
                uri=_ADK_AGENT_EXECUTOR_EXTENSION_URI,
                description=(
                    "Ability to use the new agent executor implementation"
                ),
            ),
        ],
    )


def _build_card_sync(builder: AgentCardBuilder) -> AgentCard:
    """Build an AgentCard synchronously, supporting both sync and async callers."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(builder.build())
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, builder.build()).result()


def attach_a2a_routes(
    app: FastAPI,
    *,
    agent: BaseAgent,
    runner: Runner | object,
    task_store: TaskStore,
    rpc_path: str,
    capabilities: AgentCapabilities | None = None,
    agent_version: str | None = None,
    app_url: str | None = None,
) -> None:
    """Register A2A routes (JSON-RPC + agent-card endpoints) under ``rpc_path``."""
    resolved_app_url = app_url or config.APP_URL
    resolved_agent_version = agent_version or config.AGENT_VERSION
    resolved_capabilities = capabilities or _default_capabilities()

    agent_card = _build_card_sync(
        AgentCardBuilder(
            agent=agent,
            capabilities=resolved_capabilities,
            rpc_url=f"{resolved_app_url}{rpc_path}",
            agent_version=resolved_agent_version,
        )
    )

    request_handler = DefaultRequestHandler(
        agent_executor=A2aAgentExecutor(runner=runner),
        task_store=task_store,
        agent_card=agent_card,
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
    )
