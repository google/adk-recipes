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

"""FastAPI entrypoint for the smart-tool-router ADK agent."""

import contextlib
import os
from collections.abc import AsyncIterator

import google.auth
from a2a.server.tasks import InMemoryTaskStore
from fastapi import FastAPI
from google.adk.cli.fast_api import get_fast_api_app
from google.adk.runners import Runner

from app.agent import app as adk_app
from app.agent import root_agent, router_plugin
from app.app_utils import services
from app.app_utils.a2a import attach_a2a_routes
from app.app_utils.reasoning_engine_adapter import (
    attach_reasoning_engine_routes,
)
from app.config import config

_TRUTHY_ENV_VALUES: frozenset[str] = frozenset({"1", "true", "yes"})
_DEFAULT_HOST = "127.0.0.1"

try:
    _, project_id = google.auth.default()
except Exception:
    project_id = None

allow_origins = (
    config.ALLOW_ORIGINS.split(",") if config.ALLOW_ORIGINS else None
)

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@contextlib.asynccontextmanager
async def lifespan(fastapi_app: FastAPI) -> AsyncIterator[None]:
    """Warm up the smart router when active and initialize the ADK runner."""
    if config.SMART_ROUTER_MODE == "active":
        await router_plugin.warmup(adk_app)
    runner = Runner(
        app=adk_app,
        session_service=services.get_session_service(),
        artifact_service=services.get_artifact_service(),
        auto_create_session=True,
    )
    fastapi_app.state.runner = runner
    fastapi_app.state.agent_app_name = adk_app.name
    yield


app: FastAPI = get_fast_api_app(
    agents_dir=AGENT_DIR,
    web=True,
    artifact_service_uri=services.ARTIFACT_SERVICE_URI,
    allow_origins=allow_origins,
    session_service_uri=services.SESSION_SERVICE_URI,
    otel_to_cloud=project_id is not None
    and config.INTEGRATION_TEST.lower() not in _TRUTHY_ENV_VALUES,
    lifespan=lifespan,
)
app.title = "smart-tool-router"
app.description = "API for interacting with the smart-tool-router ADK agent"

attach_a2a_routes(
    app,
    agent=root_agent,
    runner=lambda: app.state.runner,
    task_store=InMemoryTaskStore(),
    rpc_path=f"/a2a/{adk_app.name}",
)

attach_reasoning_engine_routes(app)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=_DEFAULT_HOST, port=config.PORT)
