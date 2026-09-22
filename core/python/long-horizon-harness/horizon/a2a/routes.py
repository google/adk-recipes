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

"""Attach Agent2Agent (A2A) JSON-RPC routes to an existing FastAPI app (a2a 1.x).

The 1.x SDK replaced ``A2AFastAPIApplication`` with route-builder functions and
made ``DefaultRequestHandler`` require the agent card. The empty-SSE-on-error
hang the old ``ResilientRequestHandler`` worked around is fixed upstream: the
v0.3 ``JSONRPCAdapter`` now turns a mid-stream raise into an SSE error frame, so
the stock handler is used directly.
"""

from __future__ import annotations

from a2a.server.agent_execution import AgentExecutor
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import (
    add_a2a_routes_to_fastapi,
    create_agent_card_routes,
    create_jsonrpc_routes,
)
from a2a.server.tasks import TaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentInterface
from fastapi import FastAPI
from google.adk.a2a.utils.agent_card_builder import AgentCardBuilder
from google.adk.agents import BaseAgent

from horizon.infrastructure.env import env_str

A2A_RPC_PATH = "/a2a"


async def _add_v0_3_compat_interface(card: AgentCard) -> AgentCard:
    """Advertise a v0.3 JSON-RPC interface so the served card stays consumable by
    v0.3 A2A clients — notably Gemini Enterprise registration, whose validator
    still requires the 0.3 card shape (top-level ``url``/``protocolVersion``).
    a2a-sdk emits those top-level fields once a 0.3 interface is present."""
    # card_modifier runs per GET on the shared card object; guard so the 0.3
    # interface isn't appended again on every fetch (unbounded card growth).
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


async def attach_a2a_routes(
    app: FastAPI,
    *,
    agent: BaseAgent,
    agent_executor: AgentExecutor,
    task_store: TaskStore,
) -> DefaultRequestHandler:
    """Build the agent card + request handler, mount the A2A routes, and return
    the handler (the scheduler drives it directly for routine turns)."""
    agent_card = await AgentCardBuilder(
        agent=agent,
        capabilities=AgentCapabilities(streaming=True),
        # Deployments set APP_URL; the fallback is the documented `make dev`
        # backend, and must stay dialable (0.0.0.0 is not).
        rpc_url=f"{env_str('APP_URL', 'http://127.0.0.1:8001')}{A2A_RPC_PATH}",
        agent_version=env_str("AGENT_VERSION", "0.1.0"),
    ).build()
    request_handler = DefaultRequestHandler(
        agent_executor=agent_executor,
        task_store=task_store,
        agent_card=agent_card,
    )
    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(
            agent_card, card_modifier=_add_v0_3_compat_interface
        ),
        jsonrpc_routes=create_jsonrpc_routes(
            request_handler, rpc_url=A2A_RPC_PATH, enable_v0_3_compat=True
        ),
    )
    return request_handler
