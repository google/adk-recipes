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

"""Attach A2A endpoints to the FastAPI app.

`attach_a2a_routes` registers two things under one path: the agent card, built
from the agent so it always describes what is actually served, and the JSON-RPC
endpoint the dashboard's chat client calls. They sit alongside ADK's own routes
and the reasoning-engine contract on the same app.

Every non-obvious argument is documented to explain its operational rationale
and prevent silent failures.
"""

from __future__ import annotations

import os
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
from google.adk.a2a.executor.config import A2aAgentExecutorConfig
from google.adk.a2a.executor.interceptors.include_artifacts_in_a2a_event import (
    include_artifacts_in_a2a_event_interceptor,
)
from google.adk.a2a.utils.agent_card_builder import AgentCardBuilder


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

        if method and "/" in str(method):
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


async def _add_v0_3_compat_interface(card: AgentCard) -> AgentCard:
    """Advertises a v0.3 JSON-RPC interface for compatibility with v0.3 A2A clients.

    Registering the agent in Gemini requires the 0.3 card shape
    (top-level ``url``/``protocolVersion``). The function is async because
    `create_agent_card_routes` expects a
    `Callable[[AgentCard], Awaitable[AgentCard]]`.

    Args:
        card: Agent card to update.

    Returns:
        Updated agent card containing the v0.3 interface.
    """
    if card.supported_interfaces:
        card.supported_interfaces.append(
            AgentInterface(
                protocol_binding="JSONRPC",
                protocol_version="0.3",
                url=card.supported_interfaces[0].url,
            )
        )
    return card


def _build_default_capabilities() -> AgentCapabilities:
    """Returns default A2A capabilities used by scaffolded projects.

    Returns:
        AgentCapabilities with streaming enabled and ADK executor extension.
    """
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


def _resolve_app_url(app_url: str | None) -> str:
    """Resolves the public base URL advertised inside the agent card.

    Fallback sequence:
    1. Explicit ``app_url`` argument.
    2. ``APP_URL`` environment variable.
    3. Agent Runtime ``/api`` passthrough built from runtime environment variables.
    4. Local default ("http://0.0.0.0:8000").

    Args:
        app_url: Explicit base URL override, if provided.

    Returns:
        Resolved base URL string.
    """
    if app_url:
        return app_url
    if env_url := os.getenv("APP_URL"):
        return env_url

    agent_engine_id = os.getenv("GOOGLE_CLOUD_AGENT_ENGINE_ID")
    project = os.getenv("GOOGLE_CLOUD_PROJECT")
    # Not GOOGLE_CLOUD_LOCATION: the agent pins it to "global", which would build
    # an invalid "global-aiplatform.googleapis.com" URL.
    location = os.getenv("GOOGLE_CLOUD_AGENT_ENGINE_LOCATION", "us-east1")
    if agent_engine_id and project and location:
        engine_id_part = agent_engine_id.split("/")[-1]
        return (
            f"https://{location}-aiplatform.googleapis.com/reasoningEngines/v1"
            f"/projects/{project}/locations/{location}"
            f"/reasoningEngines/{engine_id_part}/api"
        )

    return "http://0.0.0.0:8000"


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
    """Registers A2A routes (JSON-RPC + agent-card endpoints) under ``rpc_path``.

    Builds a dynamic agent card from ``agent`` and mounts the routes on ``app``.
    The ``runner`` should share the session/artifact/memory services with the
    standard ADK path. ``capabilities``, ``agent_version``, and ``app_url``
    override their defaults (streaming + ADK extension, ``AGENT_VERSION``,
    ``APP_URL``). Call once per app — typically in a FastAPI ``lifespan``, since
    the card is built asynchronously; repeated calls register duplicate routes.

    Args:
        app: FastAPI application on which to mount routes.
        agent: Base agent instance used to build the agent card.
        runner: Runner instance executing agent invocations.
        task_store: A2A task store implementation.
        rpc_path: Base path for JSON-RPC and agent card routes.
        capabilities: Optional custom agent capabilities override.
        agent_version: Optional version string override.
        app_url: Optional base URL override.
    """
    resolved_app_url = _resolve_app_url(app_url)
    resolved_agent_version = agent_version or os.getenv(
        "AGENT_VERSION", "0.1.0"
    )
    resolved_capabilities = capabilities or _build_default_capabilities()

    agent_card = await AgentCardBuilder(
        agent=agent,
        capabilities=resolved_capabilities,
        rpc_url=f"{resolved_app_url}{rpc_path}",
        agent_version=resolved_agent_version,
    ).build()

    request_handler = DefaultRequestHandler(
        # The legacy executor drops tool events: an agent that emits
        # function_call, function_response and text sends only the text. The
        # new implementation converts every part, so the dashboard can show
        # which tools ran. Forced rather than negotiated via the card
        # extension, so behavior does not depend on what the client asks for.
        agent_executor=A2aAgentExecutor(
            runner=runner,
            force_new_version=True,
            # Without this an artifact the agent saves stays in the artifact
            # service and never reaches the client, so `save_artifact` would
            # write a file nobody can open. The interceptor loads each
            # `artifact_delta` and attaches it to the A2A event.
            config=A2aAgentExecutorConfig(
                execute_interceptors=[
                    include_artifacts_in_a2a_event_interceptor
                ]
            ),
        ),
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
