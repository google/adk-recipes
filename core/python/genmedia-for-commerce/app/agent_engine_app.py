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

"""Agent Engine application module."""

import logging
import os
from typing import Any

import vertexai
from dotenv import load_dotenv
from google.adk.artifacts import GcsArtifactService, InMemoryArtifactService
from google.cloud import logging as google_cloud_logging
from vertexai.agent_engines.templates.adk import AdkApp

from app.agent import app as adk_app
from app.app_utils.telemetry import setup_telemetry
from app.app_utils.typing import Feedback

# Load environment variables from .env file at runtime
load_dotenv()

logger = logging.getLogger(__name__)


class AgentEngineApp(AdkApp):
    """Agent Engine application wrapping the ADK app."""

    def set_up(self) -> None:
        """Initialize the agent engine app with logging and telemetry."""
        vertexai.init()
        setup_telemetry()
        super().set_up()
        # Workaround for Gotcha #5
        self._tmpl_attrs["app_name"] = adk_app.name
        logging.basicConfig(level=logging.INFO)
        logging_client = google_cloud_logging.Client()
        self.logger = logging_client.logger(__name__)
        if gemini_location:
            os.environ["GOOGLE_CLOUD_LOCATION"] = gemini_location

    def register_feedback(self, feedback: dict[str, Any]) -> None:
        """Collect and log feedback."""
        feedback_obj = Feedback.model_validate(feedback)
        self.logger.log_struct(feedback_obj.model_dump(), severity="INFO")

    def register_operations(self) -> dict[str, list[str]]:
        """Register the operations of the Agent."""
        operations = super().register_operations()
        operations[""] = [*operations.get("", []), "register_feedback"]
        return operations


gemini_location = os.environ.get("GOOGLE_CLOUD_LOCATION")
logs_bucket_name = os.environ.get("LOGS_BUCKET_NAME")
try:
    agent_engine = AgentEngineApp(
        app=adk_app,
        artifact_service_builder=lambda: (
            GcsArtifactService(bucket_name=logs_bucket_name)
            if logs_bucket_name
            else InMemoryArtifactService()
        ),
    )
except Exception as e:
    # vertexai's AdkApp.__init__ always calls google.auth.default() for
    # credentials; this fails in environments without ADC (e.g. unit-test
    # runners without service-account keys).  Guard so the module stays
    # importable — callers that need a real instance (integration tests,
    # Cloud Run entrypoints) should check for None and obtain credentials first.
    logger.warning(
        f"Could not instantiate AgentEngineApp at import time: {e}. "
        "GCP credentials or project configuration may be missing."
    )
    agent_engine = None
