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

import contextlib
import os
from collections.abc import AsyncIterator

import google.auth
from a2a.server.tasks import InMemoryTaskStore
from fastapi import FastAPI
from google.adk.cli.fast_api import get_fast_api_app
from google.adk.runners import Runner

from brand_search_optimization.app_utils import services
from brand_search_optimization.app_utils.a2a import attach_a2a_routes

# Cloud telemetry needs Application Default Credentials. Resolve them here
# rather than letting get_fast_api_app raise DefaultCredentialsError at import
# time: the container has no ADC in CI, so an unguarded otel_to_cloud=True
# makes the image unstartable and fails recipe-docker-build.
try:
    _, project_id = google.auth.default()
except Exception:
    project_id = None

allow_origins = (
    [
        origin.strip()
        for origin in os.getenv("ALLOW_ORIGINS").split(",")
        if origin.strip()
    ]
    if os.getenv("ALLOW_ORIGINS")
    else None
)

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Fallbacks for local execution; deployments set PORT (see .env.example).
DEFAULT_HOST = "0.0.0.0"  # noqa: S104 -- container entrypoint default host
DEFAULT_PORT = 8080


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    from brand_search_optimization.agent import app as adk_app
    from brand_search_optimization.agent import root_agent

    runner = Runner(
        app=adk_app,
        session_service=services.get_session_service(),
        artifact_service=services.get_artifact_service(),
        auto_create_session=True,
    )
    app.state.runner = runner
    app.state.agent_app_name = adk_app.name
    await attach_a2a_routes(
        app,
        agent=root_agent,
        runner=runner,
        task_store=InMemoryTaskStore(),
        rpc_path=f"/a2a/{adk_app.name}",
    )
    yield


app: FastAPI = get_fast_api_app(
    agents_dir=AGENT_DIR,
    web=True,
    artifact_service_uri=services.ARTIFACT_SERVICE_URI,
    allow_origins=allow_origins,
    session_service_uri=services.SESSION_SERVICE_URI,
    otel_to_cloud=project_id is not None and not os.getenv("INTEGRATION_TEST"),
    lifespan=lifespan,
)
app.title = "brand-search-optimization"
app.description = "API for interacting with the Agent brand-search-optimization"


# Main execution
if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT") or DEFAULT_PORT)
    uvicorn.run(app, host=DEFAULT_HOST, port=port)
