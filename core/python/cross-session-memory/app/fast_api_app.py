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

import os

import google.auth
import vertexai
from fastapi import FastAPI
from google.adk.cli.fast_api import get_fast_api_app
from google.cloud import logging as google_cloud_logging
from vertexai._genai.types import (
    AgentEngineConfig,
    ReasoningEngineContextSpec,
)

from app.app_utils.memory_config import memory_bank_config
from app.app_utils.telemetry import setup_telemetry
from app.app_utils.typing import Feedback

setup_telemetry()
try:
    _, project_id = google.auth.default()
except Exception:
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT")

if os.environ.get("INTEGRATION_TEST"):
    import logging as _std_logging

    _cloud_logger = None
    _std_logger = _std_logging.getLogger(__name__)
else:
    try:
        logging_client = google_cloud_logging.Client()
        _cloud_logger = logging_client.logger(__name__)
        _std_logger = None
    except Exception:
        import logging as _std_logging

        _cloud_logger = None
        _std_logger = _std_logging.getLogger(__name__)


def _log_struct(data: dict) -> None:
    if _cloud_logger:
        _cloud_logger.log_struct(data, severity="INFO")
    else:
        import logging as _lg

        _lg.info("Struct log: %s", data)


allow_origins = (
    os.getenv("ALLOW_ORIGINS", "").split(",")
    if os.getenv("ALLOW_ORIGINS")
    else None
)

# Artifact bucket for ADK (passed via env var)
logs_bucket_name = os.environ.get("LOGS_BUCKET_NAME")

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Agent Engine session and memory configuration
# Check if we should use in-memory session for local development (set USE_IN_MEMORY_SESSION=true)
use_in_memory_session = os.environ.get("USE_IN_MEMORY_SESSION", "").lower() in (
    "true",
    "1",
    "yes",
)

if use_in_memory_session:
    # Use in-memory session/memory for local development
    session_service_uri = None
    memory_service_uri = None
else:
    # Use environment variable for agent name, default to project name
    default_agent_name = "memory-bank-sample"
    agent_name = os.environ.get("AGENT_ENGINE_SESSION_NAME", default_agent_name)

    # --- Memory Bank ---
    # Use vertexai.Client API (instead of the module-level agent_engines API)
    # so we can pass AgentEngineConfig with context_spec containing the
    # memory_bank_config when creating a new Agent Engine instance.
    agent_engine_location = os.environ.get("AGENT_ENGINE_LOCATION", "us-west1")
    client = vertexai.Client(project=project_id, location=agent_engine_location)

    existing_agents = list(client.agent_engines.list())
    matching_agents = [
        a for a in existing_agents if a.api_resource.display_name == agent_name
    ]

    if matching_agents:
        # Use the existing agent
        agent_engine = matching_agents[0]
    else:
        # --- Memory Bank ---
        # Create Agent Engine with memory_bank_config via context_spec so the
        # instance has Memory Bank enabled from the start.
        context_spec = ReasoningEngineContextSpec(
            memory_bank_config=memory_bank_config,
        )
        agent_engine = client.agent_engines.create(
            config=AgentEngineConfig(
                display_name=agent_name,
                context_spec=context_spec,
            ),
        )

    session_service_uri = f"agentengine://{agent_engine.api_resource.name}"
    # --- Memory Bank ---
    # The memory service uses the same Agent Engine instance as the session
    # service. Setting memory_service_uri tells ADK to use
    # VertexAiMemoryBankService, which works with the PreloadMemoryTool and
    # generate_memories_callback defined in agent.py.
    memory_service_uri = session_service_uri

artifact_service_uri = (
    f"gs://{logs_bucket_name}"
    if logs_bucket_name and not use_in_memory_session
    else None
)

app: FastAPI = get_fast_api_app(
    agents_dir=AGENT_DIR,
    web=True,
    artifact_service_uri=artifact_service_uri,
    allow_origins=allow_origins,
    session_service_uri=session_service_uri,
    # --- Memory Bank ---
    memory_service_uri=memory_service_uri,
    otel_to_cloud=not use_in_memory_session,
)
app.title = "memory-bank-sample"
app.description = "API for interacting with the Memory Bank recipe agent"


@app.post("/feedback")
def collect_feedback(feedback: Feedback) -> dict[str, str]:
    """Collect and log feedback.

    Args:
        feedback: The feedback data to log

    Returns:
        Success message
    """
    _log_struct(feedback.model_dump())
    return {"status": "success"}


# Main execution
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
