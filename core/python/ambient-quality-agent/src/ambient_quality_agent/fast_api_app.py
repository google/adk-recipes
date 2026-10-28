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

"""ASGI entrypoint for the AQA container image (see the repository Dockerfile).

Wiring::

    ADK app builder ──► app ◄── routers from core.ambient.routes, core.command_routes
        │ registers the unary and streaming Agent Runtime query routes
        │ lifespan
        ▼
    _lifespan()
        ├──► backends.configure_providers()
        ├──► check_investigations_schema(), check_insights_schema() ──► BigQuery
        ├──► attach_a2a_routes() onto app, at the chat agent's A2A path
        │      ├ agent: chat_agent.build_chat_agent()
        │      ├ runner: Runner ◄── services.get_session_service(),
        │      │                    services.get_artifact_service()
        │      └ task store: _build_task_store() ──► GcsTaskStore ──► GCS
        │                                            or an in-memory task store
        └──► telemetry.setup.shutdown_spans() on exit

    global tracer provider ──► telemetry.setup.attach_gcs_exporter()

Agent Runtime builds the Dockerfile and runs its CMD; it injects no serving
code, so the container must implement the runtime contract itself:

    "If you choose to deploy your agent using a custom container or a
    Dockerfile, your container must adhere to the runtime contract to
    successfully serve queries. [...] the container must listen for HTTP
    requests on 0.0.0.0 on port 8080."

    https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/runtime/runtime-contract

That contract is two routes -- `/api/reasoning_engine` (unary) and
`/api/stream_reasoning_engine` (streaming) -- each receiving a JSON body of
`{class_method, input}`. They back `:query`, `:streamQuery` and, critically for
AQA, `:asyncQuery`: durable investigations execute as async query jobs, so a
container without these routes deploys and chats but can never finish an
investigation.

ADK already implements both. `get_fast_api_app` registers them, but only when
`gemini_enterprise_app_name` is passed. Opting in that way is why this file is
small -- everything below is configuration, not a reimplementation.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from a2a.server.tasks import InMemoryTaskStore, TaskStore
from ambient_quality_agent import backends
from ambient_quality_agent import config as config_module
from ambient_quality_agent.app_utils import services
from ambient_quality_agent.app_utils.a2a import attach_a2a_routes
from ambient_quality_agent.app_utils.task_store import GcsTaskStore
from ambient_quality_agent.core import chat_agent
from ambient_quality_agent.core.ambient.routes import router as ambient_router
from ambient_quality_agent.core.command_routes import router as commands_router
from ambient_quality_agent.startup_checks import (
    check_insights_schema,
    check_investigations_schema,
)
from ambient_quality_agent.telemetry import setup as telemetry_setup
from fastapi import FastAPI
from google.adk.apps import App
from google.adk.cli.fast_api import get_fast_api_app
from google.adk.runners import Runner
from opentelemetry import trace

_package_logger = logging.getLogger(__package__)
if not _package_logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _package_logger.addHandler(_handler)
    _package_logger.setLevel(logging.INFO)

AGENT_DIR = services.AGENT_DIR
APP_NAME = "ambient_quality_agent"

# Where the chat agent answers. The UI proxy forwards `/a2a` here, and the card
# is served at `<this>/.well-known/agent-card.json`.
A2A_RPC_PATH = f"/a2a/{chat_agent.AGENT_NAME}"


def _use_lf_sse_frames() -> None:
    """Make the A2A SDK end SSE frames with LF rather than CRLF.

    Agent Runtime's SSE relay dispatches a frame only on a blank **LF** line,
    and `sse_starlette` writes CRLF. Without this the browser receives exactly
    one frame and the chat hangs -- not an error, just silence. Both classes
    hold the separator as a class attribute, so one assignment covers every
    response the SDK builds.

    Tracked in b/556488800; remove when that is fixed. Note there is no local
    reproduction: curl, uvicorn and the browser all parse CRLF frames happily,
    so this only ever shows up through a deployed Agent Runtime agent.
    """
    from sse_starlette.event import ServerSentEvent
    from sse_starlette.sse import EventSourceResponse

    ServerSentEvent.DEFAULT_SEPARATOR = "\n"
    EventSourceResponse.DEFAULT_SEPARATOR = "\n"


_use_lf_sse_frames()


def _build_task_store() -> TaskStore:
    """Builds a task store for conversation persistence.

    Uses GCS when a jobs bucket is configured, otherwise falls back to an
    in-memory task store.

    Returns:
        TaskStore instance (GcsTaskStore or InMemoryTaskStore).
    """
    bucket = config_module.load().jobs_gcs_bucket
    if not bucket:
        _package_logger.warning(
            "No AQA_JOBS_GCS_BUCKET; chat transcripts last only for this process."
        )
        return InMemoryTaskStore()
    return GcsTaskStore(bucket)


@asynccontextmanager
async def _lifespan(fastapi_app: FastAPI) -> AsyncIterator[None]:
    """Manages application startup checks and A2A service attachment.

    Initializes storage providers, runs schema validation, constructs the chat
    agent, and registers A2A routes.

    Args:
        fastapi_app: FastAPI application instance.

    Yields:
        None while the application serves requests.
    """
    # Storage first: the checks below are the first thing to reach a seam. In
    # the lifespan rather than at import, so importing this module rewires
    # nothing.
    backends.configure_providers()
    # Both checks read a live BigQuery table's schema. Standalone has no
    # dataset, and an in-process store has no schema to fall behind the models.
    if not config_module.load().standalone:
        await check_investigations_schema()
        await check_insights_schema()
    agent = chat_agent.build_chat_agent()
    # One agent, one runner. `attach_a2a_routes` uses `agent` only to build the
    # card and executes whatever `runner` wraps, so a runner over a *different*
    # agent publishes a card that does not describe what is served.
    #
    # The chat agent and not `root_agent`: the orchestrator can rewrite config,
    # and this surface must not reach that.
    await attach_a2a_routes(
        fastapi_app,
        agent=agent,
        runner=Runner(
            app=App(name=agent.name, root_agent=agent),
            session_service=services.get_session_service(),
            artifact_service=services.get_artifact_service(),
            auto_create_session=True,
        ),
        task_store=_build_task_store(),
        rpc_path=A2A_RPC_PATH,
    )
    try:
        yield
    finally:
        # Best-effort shutdown: containers may terminate abruptly during scale-to-zero
        # without running ASGI lifespan cleanup.
        telemetry_setup.shutdown_spans()


app: FastAPI = get_fast_api_app(
    agents_dir=AGENT_DIR,
    web=True,
    # `shared://` so ADK's web routes and every surface attached in `_lifespan`
    # resolve one service each, and a session created on one is visible to the
    # others. `get_fast_api_app` takes URIs, not objects, so the registry in
    # `app_utils.services` is the only seam for that.
    session_service_uri=services.SESSION_SERVICE_URI,
    artifact_service_uri=services.ARTIFACT_SERVICE_URI,
    # Registers /api/reasoning_engine and /api/stream_reasoning_engine. Without
    # it ADK serves only its own routes and `:asyncQuery` has nothing to call.
    gemini_enterprise_app_name=APP_NAME,
    lifespan=_lifespan,
)

# Attached after get_fast_api_app() initializes and installs the global tracer provider.
telemetry_setup.attach_gcs_exporter(trace.get_tracer_provider())

app.title = "ambient-quality-agent"
app.description = "Ambient Quality Agent (AQA) orchestrator"

app.include_router(ambient_router)
app.include_router(commands_router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))  # noqa: S104 - direct runs mirror the container CMD's all-interfaces bind
